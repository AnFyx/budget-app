"""Tests de la base locale : déduplication et gestion des connexions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from budget_poc import store as store_module
from budget_poc.store import TransactionStore, _dedup_key


def _transaction(**overrides: object) -> dict:
    """Transaction brute minimale, au format renvoyé par l'API."""
    raw: dict = {
        "entry_reference": "REF-001",
        "transaction_amount": {"amount": "12.50", "currency": "EUR"},
        "credit_debit_indicator": "DBIT",
        "status": "PDNG",
        "value_date": "2026-03-01",
        "booking_date": None,
        "remittance_information": ["FACTURE CARTE EXEMPLE"],
    }
    raw.update(overrides)
    return raw


def _count_rows(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    finally:
        conn.close()


def test_dedup_key_does_not_depend_on_account_uid(tmp_path: Path) -> None:
    """Régression : un nouvel uid de session ne doit pas dupliquer l'historique."""
    db_path = tmp_path / "transactions.db"
    db = TransactionStore(db_path)
    db.upsert_account("uid-before-reconnect", "Compte", None)
    db.upsert_account("uid-after-reconnect", "Compte", None)

    db.upsert_transactions("uid-before-reconnect", [_transaction()])
    inserted, _ = db.upsert_transactions("uid-after-reconnect", [_transaction()])

    assert inserted == 0
    assert _count_rows(db_path) == 1


def test_pending_to_booked_updates_in_place(tmp_path: Path) -> None:
    db_path = tmp_path / "transactions.db"
    db = TransactionStore(db_path)
    db.upsert_account("uid", "Compte", None)

    db.upsert_transactions("uid", [_transaction()])
    booked = _transaction(status="BOOK", booking_date="2026-03-02")
    inserted, updated = db.upsert_transactions("uid", [booked])

    assert (inserted, updated) == (0, 1)
    assert _count_rows(db_path) == 1
    assert db.get_transactions()[0]["status"] == "BOOK"


def test_fallback_key_is_deterministic_without_entry_reference() -> None:
    raw = _transaction(entry_reference=None)
    assert _dedup_key(raw) == _dedup_key(dict(raw))
    assert _dedup_key(raw).startswith("hash:")


def test_signed_amount_for_debit(tmp_path: Path) -> None:
    db = TransactionStore(tmp_path / "transactions.db")
    db.upsert_account("uid", "Compte", None)
    db.upsert_transactions("uid", [_transaction()])
    assert db.get_transactions()[0]["amount"] == pytest.approx(-12.50)


def test_every_connection_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chaque connexion ouverte par le store doit être fermée après usage."""
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(store_module.sqlite3, "connect", tracking_connect)
    db = TransactionStore(tmp_path / "transactions.db")
    db.upsert_account("uid", "Compte", None)
    db.upsert_transactions("uid", [_transaction()])
    db.get_transactions()

    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")  # lève si la connexion est fermée
