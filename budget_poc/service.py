"""Logique métier partagée entre la CLI et le front Streamlit.

Centralise les opérations qui touchent à la fois l'API, le coffre chiffré et la
base, pour éviter toute duplication (et toute divergence de comportement) entre
les deux interfaces.
"""

from __future__ import annotations

import datetime as dt
import logging

from . import config
from .eb_client import EnableBankingClient
from .secure_store import SecureStore
from .store import TransactionStore

logger = logging.getLogger(__name__)

_SESSION_KEY = "session"
_CONSENT_EXPIRES_META = "consent_expires_at"


def secure_store(settings: config.Settings) -> SecureStore:
    """Construit le coffre chiffré (session, secrets)."""
    return SecureStore(settings.data_dir / "store.enc")


def session_accounts(session: dict) -> list[dict]:
    """Retourne les comptes de la session, en tolérant l'ancien format."""
    accounts = session.get("accounts")
    if accounts:
        return accounts
    legacy_uids = session.get("account_uids", [])
    return [{"uid": uid, "name": "Compte", "iban": None} for uid in legacy_uids]


def perform_fetch(
    settings: config.Settings, date_from: str | None = None
) -> dict:
    """Rafraîchit depuis l'API et écrit en base (idempotent).

    Retourne un récapitulatif {accounts: [...], total_new, total_updated}.
    Lève EnableBankingError / SecureStoreError en cas d'échec (gérées par l'appelant).
    """
    client = EnableBankingClient(settings)
    secure = secure_store(settings)
    db = TransactionStore(settings.db_path)

    session = secure.load().get(_SESSION_KEY)
    if not session:
        raise RuntimeError("Aucune session. Lance d'abord `connect`.")

    summary: dict = {"accounts": [], "total_new": 0, "total_updated": 0}
    for account in session_accounts(session):
        uid = account["uid"]
        db.upsert_account(uid, account.get("name", "Compte"), account.get("iban"))
        transactions = client.get_transactions(uid, date_from=date_from)
        inserted, updated = db.upsert_transactions(uid, transactions)
        db.record_sync(uid, len(transactions))
        summary["accounts"].append(
            {
                "name": account.get("name", uid),
                "received": len(transactions),
                "new": inserted,
                "updated": updated,
            }
        )
        summary["total_new"] += inserted
        summary["total_updated"] += updated
    return summary


def consent_expires_at(db: TransactionStore) -> dt.datetime | None:
    """Retourne la date d'expiration du consentement (clair), ou None si inconnue."""
    raw = db.get_meta(_CONSENT_EXPIRES_META)
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def record_consent_expiry(db: TransactionStore, expires_at: dt.datetime) -> None:
    """Mémorise l'expiration du consentement en clair (date non sensible)."""
    db.set_meta(_CONSENT_EXPIRES_META, expires_at.isoformat())
