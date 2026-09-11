"""Persistance locale des transactions en SQLite.

Stratégie de synchronisation : *idempotente*. À chaque rafraîchissement on
re-récupère une fenêtre de transactions qui recouvre l'existant, et on applique
un upsert sur une clé d'identité stable (`dedup_key`). Rejouer le même fetch dix
fois converge vers le même état, sans doublon ni perte. C'est ce qui gère
correctement le passage *pending → booked*, où une même opération réelle change
de date/statut entre deux récupérations.

Choix de la clé de dédup, par ordre de préférence :
1. `entry_reference` fourni par la banque (le plus fiable, stable dans le temps).
2. À défaut, un hash déterministe de (date de valeur, montant, devise, sens,
   libellé normalisé). Imparfait mais raisonnable ; à affiner selon les champs
   réellement renvoyés par BNP (cf. commande `show --raw`).

Les transactions sont stockées en clair (choix assumé : données non secrètes,
confidentialité au repos déléguée au chiffrement disque). Les secrets et la
session restent, eux, dans le coffre chiffré séparé. L'IBAN n'est jamais stocké
en entier ici : seuls ses derniers caractères, pour identifier le compte.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    uid         TEXT PRIMARY KEY,
    name        TEXT,
    iban_suffix TEXT,
    first_seen  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    dedup_key    TEXT PRIMARY KEY,
    account_uid  TEXT NOT NULL,
    value_date   TEXT,
    booking_date TEXT,
    amount       REAL NOT NULL,
    currency     TEXT NOT NULL,
    status       TEXT,
    label        TEXT,
    raw_json     TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    FOREIGN KEY (account_uid) REFERENCES accounts(uid)
);

CREATE INDEX IF NOT EXISTS idx_txn_account_date
    ON transactions(account_uid, value_date);

CREATE TABLE IF NOT EXISTS sync_meta (
    account_uid TEXT PRIMARY KEY,
    last_sync   TEXT NOT NULL,
    last_count  INTEGER
);

CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS overrides (
    dedup_key  TEXT PRIMARY KEY,
    category   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'manual',
    date_start   TEXT,
    date_end     TEXT,
    total_budget REAL
);

CREATE TABLE IF NOT EXISTS project_transactions (
    project_id INTEGER NOT NULL,
    dedup_key  TEXT NOT NULL,
    PRIMARY KEY (project_id, dedup_key),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_budgets (
    project_id INTEGER NOT NULL,
    category   TEXT NOT NULL,
    amount     REAL NOT NULL,
    PRIMARY KEY (project_id, category),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_exclusions (
    project_id INTEGER NOT NULL,
    dedup_key  TEXT NOT NULL,
    PRIMARY KEY (project_id, dedup_key),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
"""


def _now_iso() -> str:
    """Horodatage courant en ISO 8601 UTC."""
    return dt.datetime.now(tz=dt.timezone.utc).isoformat()


def _normalize_label(label: str) -> str:
    """Normalise un libellé pour la clé de dédup (casse + espaces)."""
    return " ".join(label.lower().split())


def _extract_label(raw: dict) -> str:
    """Extrait un libellé lisible d'une transaction, défensivement."""
    for key in ("remittance_information", "creditor_name", "debtor_name"):
        value = raw.get(key)
        if isinstance(value, list) and value:
            return str(value[0])
        if isinstance(value, str) and value:
            return value
    return ""


def _signed_amount(raw: dict) -> float:
    """Montant signé : négatif pour un débit, positif pour un crédit.

    Enable Banking renvoie un montant positif accompagné d'un indicateur de sens
    (`credit_debit_indicator` : CRDT/DBIT). On applique le signe nous-mêmes.
    """
    amount_obj = raw.get("transaction_amount", {})
    try:
        amount = float(amount_obj.get("amount"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Montant de transaction absent ou invalide.") from exc
    indicator = str(raw.get("credit_debit_indicator", "")).upper()
    return -amount if indicator == "DBIT" else amount


def _dedup_key(raw: dict) -> str:
    """Calcule la clé d'identité stable d'une transaction.

    Privilégie l'`entry_reference` de la banque (stable et unique). À défaut, un
    hash déterministe de champs stables.

    La clé ne dépend PAS de l'`uid` de compte attribué par la session : cet uid
    change à chaque reconnexion (nouveau consentement), ce qui ferait réinsérer
    toutes les transactions en double. Limite assumée : en multi-comptes, deux
    comptes dont les `entry_reference` se recouvriraient pourraient entrer en
    collision. À ce stade mono-compte, ce n'est pas un risque ; réintroduire
    l'IBAN (stable, contrairement à l'uid) comme discriminant le cas échéant.
    """
    ref = raw.get("entry_reference")
    if isinstance(ref, str) and ref.strip():
        return f"ref:{ref.strip()}"

    amount_obj = raw.get("transaction_amount", {})
    # On privilégie la date de valeur, plus stable que la date de comptabilisation
    # entre les états pending et booked.
    date = raw.get("value_date") or raw.get("transaction_date") or ""
    parts = [
        str(date),
        str(amount_obj.get("amount", "")),
        str(amount_obj.get("currency", "")),
        str(raw.get("credit_debit_indicator", "")),
        _normalize_label(_extract_label(raw)),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"hash:{digest}"


class TransactionStore:
    """Accès à la base SQLite locale des comptes et transactions."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Chaque accès passe par `with self._connect() as conn:` : transaction
        # validée en cas de succès, annulée en cas d'exception, connexion fermée.
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
        self._migrate()
        self._harden_permissions()

    def _migrate(self) -> None:
        """Ajoute les colonnes manquantes aux bases créées par une version antérieure.

        `CREATE TABLE IF NOT EXISTS` ne met pas à jour une table existante : on
        ajoute donc à la main les colonnes des projets budgétés si elles manquent.
        """
        additions = {
            "kind": "ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual'",
            "date_start": "ALTER TABLE projects ADD COLUMN date_start TEXT",
            "date_end": "ALTER TABLE projects ADD COLUMN date_end TEXT",
            "total_budget": "ALTER TABLE projects ADD COLUMN total_budget REAL",
        }
        with self._connect() as conn:
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(projects)").fetchall()
            }
            for column, statement in additions.items():
                if column not in existing:
                    conn.execute(statement)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Ouvre une connexion, valide ou annule la transaction, puis la ferme.

        `with sqlite3.connect(...)` seul valide la transaction mais ne ferme PAS la
        connexion : chaque appel laissait un descripteur ouvert (verrou de fichier
        possible sous Windows). Ce context manager garantit la fermeture.
        """
        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            # Intégrité référentielle activée explicitement (off par défaut en SQLite).
            conn.execute("PRAGMA foreign_keys = ON;")
            with conn:  # commit si succès, rollback si exception
                yield conn
        finally:
            conn.close()

    def _harden_permissions(self) -> None:
        """Restreint l'accès au fichier de base au seul propriétaire (POSIX)."""
        if os.name == "posix" and self._db_path.is_file():
            self._db_path.chmod(0o600)

    # --- Comptes -----------------------------------------------------------

    def upsert_account(self, uid: str, name: str, iban: str | None) -> None:
        """Enregistre ou met à jour un compte. L'IBAN est tronqué avant stockage."""
        iban_suffix = iban[-4:] if iban else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts (uid, name, iban_suffix, first_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET
                    name = excluded.name,
                    iban_suffix = excluded.iban_suffix
                """,
                (uid, name, iban_suffix, _now_iso()),
            )

    # --- Transactions ------------------------------------------------------

    def upsert_transactions(
        self, account_uid: str, raw_transactions: list[dict]
    ) -> tuple[int, int]:
        """Insère ou met à jour un lot de transactions brutes.

        Retourne (nb_insérées, nb_mises_à_jour). La distinction est calculée en
        comparant le nombre de lignes du compte avant et après l'opération.
        """
        now = _now_iso()
        with self._connect() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE account_uid = ?",
                (account_uid,),
            ).fetchone()[0]

            for raw in raw_transactions:
                self._upsert_one(conn, account_uid, raw, now)

            after = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE account_uid = ?",
                (account_uid,),
            ).fetchone()[0]

        inserted = after - before
        updated = len(raw_transactions) - inserted
        return inserted, max(updated, 0)

    def _upsert_one(
        self, conn: sqlite3.Connection, account_uid: str, raw: dict, now: str
    ) -> None:
        """Upsert d'une transaction unique (requête paramétrée)."""
        key = _dedup_key(raw)
        amount_obj = raw.get("transaction_amount", {})
        conn.execute(
            """
            INSERT INTO transactions (
                dedup_key, account_uid, value_date, booking_date,
                amount, currency, status, label, raw_json,
                first_seen, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedup_key) DO UPDATE SET
                value_date   = excluded.value_date,
                booking_date = excluded.booking_date,
                amount       = excluded.amount,
                currency     = excluded.currency,
                status       = excluded.status,
                label        = excluded.label,
                raw_json     = excluded.raw_json,
                last_updated = excluded.last_updated
            """,
            (
                key,
                account_uid,
                raw.get("value_date"),
                raw.get("booking_date"),
                _signed_amount(raw),
                str(amount_obj.get("currency", "")),
                raw.get("status"),
                _extract_label(raw),
                json.dumps(raw, ensure_ascii=False),
                now,
                now,
            ),
        )

    def get_transactions(
        self, account_uid: str | None = None, date_from: str | None = None
    ) -> list[dict[str, Any]]:
        """Lit les transactions depuis la base (jamais d'appel réseau).

        Filtrage optionnel par compte et par date de valeur minimale. Les
        paramètres sont passés à la requête de façon paramétrée (anti-injection).
        """
        clauses: list[str] = []
        params: list[Any] = []
        if account_uid:
            clauses.append("account_uid = ?")
            params.append(account_uid)
        if date_from:
            # COALESCE : certaines banques (dont BNP) ne renseignent pas value_date
            # mais booking_date. On filtre sur la première date disponible.
            clauses.append("COALESCE(value_date, booking_date) >= ?")
            params.append(date_from)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = (
            "SELECT dedup_key, account_uid, value_date, booking_date, amount, "
            f"currency, status, label FROM transactions {where} "
            "ORDER BY COALESCE(value_date, booking_date) DESC"
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_raw(self, account_uid: str | None, limit: int) -> list[str]:
        """Retourne le JSON brut des dernières transactions (pour inspection)."""
        clause = "WHERE account_uid = ?" if account_uid else ""
        params: list[Any] = [account_uid] if account_uid else []
        params.append(limit)
        query = (
            f"SELECT raw_json FROM transactions {clause} "
            "ORDER BY COALESCE(value_date, booking_date) DESC LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row["raw_json"] for row in rows]

    # --- Métadonnées de synchro -------------------------------------------

    def record_sync(self, account_uid: str, count: int) -> None:
        """Mémorise la date et le volume du dernier rafraîchissement d'un compte."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_meta (account_uid, last_sync, last_count)
                VALUES (?, ?, ?)
                ON CONFLICT(account_uid) DO UPDATE SET
                    last_sync = excluded.last_sync,
                    last_count = excluded.last_count
                """,
                (account_uid, _now_iso(), count),
            )

    def get_last_sync(self, account_uid: str) -> str | None:
        """Retourne l'horodatage ISO du dernier fetch d'un compte, ou None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_sync FROM sync_meta WHERE account_uid = ?",
                (account_uid,),
            ).fetchone()
        return row["last_sync"] if row else None

    # --- Métadonnées applicatives (clé-valeur non sensible) ----------------

    def set_meta(self, key: str, value: str) -> None:
        """Écrit une métadonnée applicative en clair (ex. expiration consentement)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        """Lit une métadonnée applicative, ou None si absente."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    # --- Corrections manuelles (overrides par transaction) -----------------

    def set_override(self, dedup_key: str, category: str) -> None:
        """Force une transaction précise dans une catégorie (prime sur les règles)."""
        if not dedup_key or not category:
            raise ValueError("dedup_key et category sont requis pour un override.")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO overrides (dedup_key, category, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(dedup_key) DO UPDATE SET
                    category = excluded.category,
                    created_at = excluded.created_at
                """,
                (dedup_key, category, _now_iso()),
            )

    def remove_override(self, dedup_key: str) -> None:
        """Supprime la correction manuelle d'une transaction (retour aux règles)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM overrides WHERE dedup_key = ?", (dedup_key,))

    def get_overrides(self) -> dict[str, str]:
        """Retourne tous les overrides sous forme {dedup_key: category}."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT dedup_key, category FROM overrides"
            ).fetchall()
        return {row["dedup_key"]: row["category"] for row in rows}

    # --- Projets (sélection manuelle de transactions) ----------------------

    def create_project(self, name: str) -> int:
        """Crée un projet et retourne son id. Lève si le nom existe déjà."""
        if not name.strip():
            raise ValueError("Le nom du projet ne peut pas être vide.")
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO projects (name, created_at) VALUES (?, ?)",
                (name.strip(), _now_iso()),
            )
            return int(cursor.lastrowid)

    def list_projects(self) -> list[dict]:
        """Liste les projets, du plus récent au plus ancien (avec leur type)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, kind FROM projects ORDER BY created_at DESC"
            ).fetchall()
        return [{"id": r["id"], "name": r["name"], "kind": r["kind"]} for r in rows]

    def get_project(self, project_id: int) -> dict | None:
        """Retourne les métadonnées complètes d'un projet, ou None s'il n'existe pas."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, name, kind, date_start, date_end, total_budget
                FROM projects WHERE id = ?
                """,
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_budget_project(
        self,
        name: str,
        date_start: str,
        date_end: str,
        total_budget: float,
    ) -> int:
        """Crée un projet budgété (période + enveloppe). Retourne son id."""
        if not name.strip():
            raise ValueError("Le nom du projet ne peut pas être vide.")
        if date_end < date_start:
            raise ValueError("La date de fin précède la date de début.")
        if total_budget < 0:
            raise ValueError("Le budget total ne peut pas être négatif.")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO projects
                    (name, created_at, kind, date_start, date_end, total_budget)
                VALUES (?, ?, 'budget', ?, ?, ?)
                """,
                (name.strip(), _now_iso(), date_start, date_end, total_budget),
            )
            return int(cursor.lastrowid)

    def update_budget_project(
        self, project_id: int, date_start: str, date_end: str, total_budget: float
    ) -> None:
        """Met à jour la période et l'enveloppe d'un projet budgété."""
        if date_end < date_start:
            raise ValueError("La date de fin précède la date de début.")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE projects SET date_start = ?, date_end = ?, total_budget = ?
                WHERE id = ?
                """,
                (date_start, date_end, total_budget, project_id),
            )

    def set_category_budgets(self, project_id: int, budgets: dict[str, float]) -> None:
        """Remplace l'ensemble des budgets par catégorie d'un projet."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM project_budgets WHERE project_id = ?", (project_id,)
            )
            conn.executemany(
                """
                INSERT INTO project_budgets (project_id, category, amount)
                VALUES (?, ?, ?)
                """,
                [
                    (project_id, category, float(amount))
                    for category, amount in budgets.items()
                    if str(category).strip() and float(amount) > 0
                ],
            )

    def get_category_budgets(self, project_id: int) -> dict[str, float]:
        """Retourne les budgets par catégorie d'un projet sous forme {cat: montant}."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT category, amount FROM project_budgets WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        return {r["category"]: r["amount"] for r in rows}

    def add_exclusion(self, project_id: int, dedup_key: str) -> None:
        """Exclut une transaction d'un projet budgété (opt-out)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO project_exclusions (project_id, dedup_key)
                VALUES (?, ?) ON CONFLICT DO NOTHING
                """,
                (project_id, dedup_key),
            )

    def remove_exclusion(self, project_id: int, dedup_key: str) -> None:
        """Réintègre une transaction précédemment exclue."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM project_exclusions WHERE project_id = ? AND dedup_key = ?",
                (project_id, dedup_key),
            )

    def get_project_exclusions(self, project_id: int) -> set[str]:
        """Retourne l'ensemble des dedup_key exclues d'un projet budgété."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT dedup_key FROM project_exclusions WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        return {r["dedup_key"] for r in rows}

    def delete_project(self, project_id: int) -> None:
        """Supprime un projet et ses associations (cascade)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def add_to_project(self, project_id: int, dedup_key: str) -> None:
        """Ajoute une transaction à un projet (idempotent)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO project_transactions (project_id, dedup_key)
                VALUES (?, ?) ON CONFLICT DO NOTHING
                """,
                (project_id, dedup_key),
            )

    def remove_from_project(self, project_id: int, dedup_key: str) -> None:
        """Retire une transaction d'un projet."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM project_transactions WHERE project_id = ? AND dedup_key = ?",
                (project_id, dedup_key),
            )

    def get_project_transactions(self, project_id: int) -> set[str]:
        """Retourne l'ensemble des dedup_key associées à un projet."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT dedup_key FROM project_transactions WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        return {row["dedup_key"] for row in rows}
