"""Tests du client Enable Banking sans réseau : pagination et encodage d'URL."""

from __future__ import annotations

import pytest

from budget_poc.eb_client import EnableBankingClient, EnableBankingError


class _FakeClient(EnableBankingClient):
    """Client dont les appels HTTP sont remplacés par des réponses préparées."""

    def __init__(self, pages: list[dict]) -> None:
        # Pas d'appel au constructeur parent : ni clé privée, ni session HTTP.
        self._pages = list(pages)
        self.calls: list[tuple[str, dict | None]] = []

    def _get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append((path, params))
        return self._pages.pop(0)


def test_follows_continuation_keys_until_exhausted() -> None:
    client = _FakeClient(
        [
            {"transactions": [{"id": 1}], "continuation_key": "page-2"},
            {"transactions": [], "continuation_key": "page-3"},  # page vide mais suite annoncée
            {"transactions": [{"id": 2}], "continuation_key": None},
        ]
    )
    result = client.get_transactions("account-1", date_from="2026-01-01")

    assert result == [{"id": 1}, {"id": 2}]
    assert [params for _, params in client.calls] == [
        {"date_from": "2026-01-01"},
        {"date_from": "2026-01-01", "continuation_key": "page-2"},
        {"date_from": "2026-01-01", "continuation_key": "page-3"},
    ]


def test_repeated_continuation_key_fails_closed() -> None:
    client = _FakeClient(
        [
            {"transactions": [], "continuation_key": "same"},
            {"transactions": [], "continuation_key": "same"},
        ]
    )
    with pytest.raises(EnableBankingError):
        client.get_transactions("account-1")


def test_malformed_page_is_rejected() -> None:
    client = _FakeClient([{"transactions": "not-a-list"}])
    with pytest.raises(EnableBankingError):
        client.get_transactions("account-1")


def test_account_uid_cannot_alter_the_route() -> None:
    client = _FakeClient([{"transactions": [], "continuation_key": None}])
    client.get_transactions("../sessions/x")
    path, _ = client.calls[0]
    assert path == "/accounts/..%2Fsessions%2Fx/transactions"
