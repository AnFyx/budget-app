r"""Migration : recalcule les `dedup_key` sans l'uid de session et fusionne les doublons.

À lancer UNE fois, après la mise à jour de store.py, quand une reconnexion a créé
des transactions en double (chaque transaction réapparaît avec un nouvel uid de
compte). Le script :
  1. sauvegarde la base (transactions.db.bak) ;
  2. recalcule la clé d'identité de chaque transaction avec la nouvelle règle
     (indépendante de l'uid) ;
  3. fusionne les doublons en gardant l'exemplaire le plus récent ;
  4. remappe les overrides et les associations de projets vers la clé conservée.

Idempotent : un second passage ne trouve plus de doublon et ne change rien.

Usage (PowerShell, depuis le dossier du projet) :
    .\.venv\Scripts\python.exe migrate_dedup.py

Si EB_DATA_DIR n'est pas la valeur par défaut :
    $env:EB_DATA_DIR = "C:\chemin\vers\data"
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from budget_poc.store import _dedup_key


def _db_path() -> Path:
    """Résout le chemin de la base comme la config de l'application."""
    data_dir = Path(os.environ.get("EB_DATA_DIR", "~/.bnp-budget-data")).expanduser()
    return data_dir / "transactions.db"


def migrate(db_path: Path) -> dict:
    """Recalcule les clés, fusionne les doublons, remappe les références.

    Retourne un dict de statistiques. Toutes les écritures se font dans une seule
    transaction SQL (tout réussit, ou rien).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT dedup_key, raw_json, first_seen FROM transactions"
        ).fetchall()

        # Ancienne clé -> nouvelle clé, et regroupement par nouvelle clé.
        old_to_new: dict[str, str] = {}
        groups: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            try:
                raw = json.loads(row["raw_json"])
            except (json.JSONDecodeError, TypeError):
                raw = {}
            new_key = _dedup_key(raw)
            old_to_new[row["dedup_key"]] = new_key
            groups.setdefault(new_key, []).append(
                (row["dedup_key"], row["first_seen"] or "")
            )

        # Survivant par groupe = first_seen le plus récent (= dernier fetch).
        survivors: dict[str, str] = {}
        losers: list[str] = []
        for new_key, members in groups.items():
            members.sort(key=lambda item: item[1], reverse=True)
            survivors[new_key] = members[0][0]
            losers.extend(old for old, _ in members[1:])

        # Remap des overrides (PK dedup_key) ; en cas de conflit, plus récent gagne.
        new_overrides: dict[str, tuple[str, str]] = {}
        for row in conn.execute(
            "SELECT dedup_key, category, created_at FROM overrides"
        ).fetchall():
            new_key = old_to_new.get(row["dedup_key"], row["dedup_key"])
            created = row["created_at"] or ""
            if new_key not in new_overrides or created > new_overrides[new_key][1]:
                new_overrides[new_key] = (row["category"], created)

        # Remap des associations de projets (ensembles dédupliqués).
        def _remap_pairs(table: str) -> set[tuple[int, str]]:
            query = f"SELECT project_id, dedup_key FROM {table}"  # noqa: S608
            return {
                (p["project_id"], old_to_new.get(p["dedup_key"], p["dedup_key"]))
                for p in conn.execute(query).fetchall()
            }

        new_project_txns = _remap_pairs("project_transactions")
        new_project_excl = _remap_pairs("project_exclusions")

        with conn:  # transaction atomique
            if losers:
                conn.executemany(
                    "DELETE FROM transactions WHERE dedup_key = ?",
                    [(key,) for key in losers],
                )
            for new_key, old_key in survivors.items():
                if old_key != new_key:
                    conn.execute(
                        "UPDATE transactions SET dedup_key = ? WHERE dedup_key = ?",
                        (new_key, old_key),
                    )
            conn.execute("DELETE FROM overrides")
            conn.executemany(
                "INSERT INTO overrides (dedup_key, category, created_at) "
                "VALUES (?, ?, ?)",
                [(k, cat, ts) for k, (cat, ts) in new_overrides.items()],
            )
            conn.execute("DELETE FROM project_transactions")
            conn.executemany(
                "INSERT INTO project_transactions (project_id, dedup_key) "
                "VALUES (?, ?)",
                list(new_project_txns),
            )
            conn.execute("DELETE FROM project_exclusions")
            conn.executemany(
                "INSERT INTO project_exclusions (project_id, dedup_key) "
                "VALUES (?, ?)",
                list(new_project_excl),
            )

        return {
            "avant": len(rows),
            "doublons_supprimes": len(losers),
            "apres": len(rows) - len(losers),
        }
    finally:
        conn.close()


def main() -> int:
    db_path = _db_path()
    if not db_path.is_file():
        print(f"Base introuvable : {db_path}")
        return 1

    backup = db_path.with_suffix(db_path.suffix + ".bak")
    shutil.copy2(db_path, backup)
    print(f"Sauvegarde : {backup}\n")

    stats = migrate(db_path)
    print(f"Transactions avant : {stats['avant']}")
    print(f"Doublons supprimes : {stats['doublons_supprimes']}")
    print(f"Transactions apres : {stats['apres']}\n")
    if stats["doublons_supprimes"] == 0:
        print("Aucun doublon : base deja saine (ou deja migree).")
    else:
        print("Doublons fusionnes. Overrides et projets remappes vers la cle conservee.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
