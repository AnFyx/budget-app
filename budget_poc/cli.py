"""Interface en ligne de commande du PoC.

Sous-commandes :
  list-banks  : affiche les ASPSP disponibles (pour trouver le nom exact de BNP).
  connect     : lance le flux de consentement et persiste la session chiffrée.
  fetch       : rafraîchit depuis l'API et écrit en base (le « bouton refresh »).
  show        : affiche les transactions depuis la base locale, sans appel réseau.
  revoke      : ferme la session et efface l'état local.

Séparation nette : `fetch` est le SEUL point qui touche l'API (à déclencher
manuellement, donc), `show` ne lit que la base. Les secrets et la session
restent dans le coffre chiffré ; les transactions vont en SQLite local.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import secrets
import sys
import webbrowser

from . import config, service
from .callback_server import wait_for_code
from .categorizer import Categorizer, CategorizerError, resolve_category
from .eb_client import EnableBankingClient, EnableBankingError
from .secure_store import SecureStore, SecureStoreError
from .store import TransactionStore

logger = logging.getLogger(__name__)

_SESSION_KEY = "session"  # clé du dict de stockage contenant la session courante


def _build_secure_store(settings: config.Settings) -> SecureStore:
    return SecureStore(settings.data_dir / "store.enc")


def _account_label(account: dict) -> str:
    """Construit un nom lisible de compte, défensivement."""
    name = account.get("name") or account.get("product") or "Compte"
    return str(name)


def _account_iban(account: dict) -> str | None:
    """Extrait l'IBAN d'un compte si présent, défensivement."""
    account_id = account.get("account_id")
    if isinstance(account_id, dict):
        iban = account_id.get("iban")
        if isinstance(iban, str) and iban:
            return iban
    return None


def cmd_list_banks(settings: config.Settings) -> int:
    """Affiche les banques disponibles pour le pays configuré."""
    client = EnableBankingClient(settings)
    aspsps = client.list_aspsps()
    print(f"ASPSP disponibles ({settings.aspsp_country}) :")
    for aspsp in aspsps:
        name = aspsp.get("name", "?")
        psu_types = ", ".join(aspsp.get("psu_types", []))
        print(f"  - {name}  [{psu_types}]")
    print(
        "\nReporte le nom EXACT de ta banque dans EB_ASPSP_NAME "
        "(.env), tel qu'affiché ci-dessus."
    )
    return 0


def cmd_connect(settings: config.Settings) -> int:
    """Lance le consentement, capte le code, crée et persiste la session."""
    client = EnableBankingClient(settings)
    secure = _build_secure_store(settings)

    # `state` aléatoire imprévisible : lien anti-CSRF entre /auth et le callback.
    state = secrets.token_urlsafe(32)
    auth_url = client.start_authorization(state)

    print("Ouverture de la page d'autorisation de la banque dans le navigateur…")
    print(f"Si rien ne s'ouvre, va manuellement à :\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print("En attente du retour après authentification forte (SCA)…")
    code = wait_for_code(
        state, settings.callback_cert_path, settings.callback_key_path
    )

    session_data = client.create_session(code)
    accounts = session_data.get("accounts", [])

    # Expiration estimée du consentement (la banque peut réduire). Stockée dans
    # le coffre ET, en clair, dans la base pour que le front puisse griser le
    # bouton de rafraîchissement sans demander la passphrase.
    expires_at = dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(
        days=settings.access_validity_days
    )

    # On persiste la session et les métadonnées de comptes (IBAN inclus) dans le
    # coffre chiffré, pas en clair.
    secure.save(
        {
            _SESSION_KEY: {
                "session_id": session_data["session_id"],
                "expires_at": expires_at.isoformat(),
                "accounts": [
                    {
                        "uid": a.get("uid"),
                        "name": _account_label(a),
                        "iban": _account_iban(a),
                    }
                    for a in accounts
                    if a.get("uid")
                ],
            }
        }
    )
    TransactionStore(settings.db_path)  # garantit l'existence du schéma
    service.record_consent_expiry(
        TransactionStore(settings.db_path), expires_at
    )
    print(f"Session établie. {len(accounts)} compte(s) connecté(s).")
    print("Lance `fetch` pour rapatrier les transactions en base.")
    return 0


def _load_session(secure: SecureStore) -> dict | None:
    """Charge la session courante depuis le coffre, ou None si absente."""
    return secure.load().get(_SESSION_KEY)


def cmd_fetch(settings: config.Settings, date_from: str | None) -> int:
    """Rafraîchit depuis l'API et écrit en base (idempotent). Le « bouton refresh ».

    C'est le seul point qui consomme l'API : à déclencher manuellement. Il
    re-récupère une fenêtre qui recouvre l'existant et fusionne par upsert.
    """
    try:
        summary = service.perform_fetch(settings, date_from)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    for account in summary["accounts"]:
        print(
            f"Compte {account['name']} : {account['received']} reçues, "
            f"{account['new']} nouvelles, {account['updated']} mises à jour."
        )
    print(
        f"\nRafraîchissement terminé : +{summary['total_new']} nouvelles, "
        f"{summary['total_updated']} mises à jour."
    )
    print("Utilise `show` pour consulter (lecture base, sans appel API).")
    return 0


def cmd_show(
    settings: config.Settings, date_from: str | None, raw: int
) -> int:
    """Affiche les transactions depuis la base locale (aucun appel réseau)."""
    db = TransactionStore(settings.db_path)

    if raw > 0:
        # Mode inspection : JSON brut, pour vérifier les champs renvoyés par BNP
        # (présence d'entry_reference, format des dates) et affiner la dédup.
        for raw_json in db.get_raw(None, raw):
            print(json.dumps(json.loads(raw_json), indent=2, ensure_ascii=False))
            print("-" * 60)
        return 0

    categorizer = Categorizer(settings.rules_path)
    overrides = db.get_overrides()
    transactions = db.get_transactions(date_from=date_from)
    if not transactions:
        print("Base vide. Lance `fetch` pour rapatrier les transactions.")
        return 0
    print(f"{len(transactions)} transaction(s) :")
    for txn in transactions:
        date = txn.get("value_date") or txn.get("booking_date") or "?"
        amount = txn.get("amount", 0.0)
        currency = txn.get("currency", "")
        label = txn.get("label") or "(sans libellé)"
        category = resolve_category(
            categorizer, overrides, txn.get("dedup_key", ""), label, amount
        )
        flag = "*" if txn.get("dedup_key") in overrides else " "
        print(f" {flag}{date}  {amount:>10.2f} {currency}  {category:22}  {label}")
    return 0


def cmd_summary(settings: config.Settings, date_from: str | None) -> int:
    """Agrège dépenses et recettes par catégorie (lecture base, sans réseau)."""
    db = TransactionStore(settings.db_path)
    categorizer = Categorizer(settings.rules_path)
    overrides = db.get_overrides()
    transactions = db.get_transactions(date_from=date_from)
    if not transactions:
        print("Base vide. Lance `fetch` d'abord.")
        return 0

    # Agrégation : par catégorie, on cumule débits (dépenses) et crédits.
    totals: dict[str, dict[str, float]] = {}
    for txn in transactions:
        amount = txn.get("amount", 0.0)
        category = resolve_category(
            categorizer, overrides, txn.get("dedup_key", ""), txn.get("label") or "", amount
        )
        bucket = totals.setdefault(category, {"debit": 0.0, "credit": 0.0, "count": 0})
        bucket["credit" if amount >= 0 else "debit"] += amount
        bucket["count"] += 1

    # Tri par dépense décroissante (le plus gros poste de dépense en tête).
    ordered = sorted(totals.items(), key=lambda kv: kv[1]["debit"])
    print(f"{'Catégorie':22} {'Dépenses':>12} {'Recettes':>12} {'Nb':>5}")
    print("-" * 54)
    total_debit = total_credit = 0.0
    for category, bucket in ordered:
        print(
            f"{category:22} {bucket['debit']:>12.2f} "
            f"{bucket['credit']:>12.2f} {bucket['count']:>5}"
        )
        total_debit += bucket["debit"]
        total_credit += bucket["credit"]
    print("-" * 54)
    print(f"{'TOTAL':22} {total_debit:>12.2f} {total_credit:>12.2f}")
    return 0


def cmd_unknown(settings: config.Settings, date_from: str | None) -> int:
    """Liste les libellés non catégorisés, dédupliqués, pour créer des règles."""
    db = TransactionStore(settings.db_path)
    categorizer = Categorizer(settings.rules_path)
    overrides = db.get_overrides()
    transactions = db.get_transactions(date_from=date_from)

    # Regroupe les libellés inconnus par occurrence et montant cumulé.
    unknown: dict[str, dict[str, float]] = {}
    for txn in transactions:
        label = txn.get("label") or "(sans libellé)"
        amount = txn.get("amount", 0.0)
        category = resolve_category(
            categorizer, overrides, txn.get("dedup_key", ""), label, amount
        )
        if category != "Inconnue":
            continue
        bucket = unknown.setdefault(label, {"count": 0, "total": 0.0})
        bucket["count"] += 1
        bucket["total"] += amount

    if not unknown:
        print("Aucune transaction non catégorisée. Tout est couvert par une règle.")
        return 0

    ordered = sorted(unknown.items(), key=lambda kv: kv[1]["count"], reverse=True)
    print(f"{len(ordered)} libellé(s) non catégorisé(s) (à transformer en règles) :")
    for label, bucket in ordered:
        print(f"  x{bucket['count']:<3} {bucket['total']:>10.2f}  {label}")
    return 0


def cmd_revoke(settings: config.Settings) -> int:
    """Ferme la session côté API et efface l'état local chiffré."""
    client = EnableBankingClient(settings)
    secure = _build_secure_store(settings)
    session = _load_session(secure)
    if session and session.get("session_id"):
        try:
            client.revoke_session(session["session_id"])
        except EnableBankingError:
            logger.warning("Révocation côté API échouée ; effacement local quand même.")
    secure.save({})  # écrase le contenu chiffré par un état vide
    print("Session révoquée et état local chiffré effacé.")
    print("Note : la base de transactions locale n'est PAS supprimée.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PoC budget BNP via Enable Banking (AIS).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-banks", help="Lister les banques disponibles.")
    sub.add_parser("connect", help="Lancer le consentement et créer la session.")
    fetch = sub.add_parser("fetch", help="Rafraîchir depuis l'API et écrire en base.")
    fetch.add_argument("--from", dest="date_from", help="Date de début AAAA-MM-JJ.")
    show = sub.add_parser("show", help="Afficher les transactions depuis la base.")
    show.add_argument("--from", dest="date_from", help="Date de début AAAA-MM-JJ.")
    show.add_argument(
        "--raw",
        type=int,
        default=0,
        metavar="N",
        help="Afficher le JSON brut des N dernières transactions (inspection).",
    )
    summary = sub.add_parser("summary", help="Totaux par catégorie.")
    summary.add_argument("--from", dest="date_from", help="Date de début AAAA-MM-JJ.")
    unknown = sub.add_parser(
        "unknown", help="Lister les libellés non catégorisés (pour créer des règles)."
    )
    unknown.add_argument("--from", dest="date_from", help="Date de début AAAA-MM-JJ.")
    sub.add_parser("revoke", help="Révoquer la session et purger l'état chiffré.")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)
    try:
        settings = config.load_settings()
    except config.ConfigError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "list-banks":
            return cmd_list_banks(settings)
        if args.command == "connect":
            return cmd_connect(settings)
        if args.command == "fetch":
            return cmd_fetch(settings, args.date_from)
        if args.command == "show":
            return cmd_show(settings, args.date_from, args.raw)
        if args.command == "summary":
            return cmd_summary(settings, args.date_from)
        if args.command == "unknown":
            return cmd_unknown(settings, args.date_from)
        if args.command == "revoke":
            return cmd_revoke(settings)
    except (EnableBankingError, SecureStoreError, CategorizerError) as exc:
        # Message générique côté utilisateur ; pas de détail sensible.
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
