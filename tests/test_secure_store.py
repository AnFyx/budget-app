"""Tests du coffre chiffré : aller-retour, falsification, échec fermé."""

from __future__ import annotations

from pathlib import Path

import pytest

from budget_poc.secure_store import SecureStore, SecureStoreError

_PASSPHRASE_ENV = "BUDGET_MASTER_PASSPHRASE"
_SAMPLE = {"session": {"session_id": "test-session", "accounts": []}}


@pytest.fixture
def vault_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Coffre dans un répertoire temporaire, avec une passphrase de test."""
    monkeypatch.setenv(_PASSPHRASE_ENV, "correct horse battery staple")
    return tmp_path / "data" / "store.enc"


def test_roundtrip_returns_saved_data(vault_path: Path) -> None:
    store = SecureStore(vault_path)
    store.save(_SAMPLE)
    assert store.load() == _SAMPLE


def test_file_does_not_contain_plaintext(vault_path: Path) -> None:
    SecureStore(vault_path).save(_SAMPLE)
    assert b"test-session" not in vault_path.read_bytes()


def test_each_save_uses_fresh_salt_and_nonce(vault_path: Path) -> None:
    store = SecureStore(vault_path)
    store.save(_SAMPLE)
    first = vault_path.read_bytes()
    store.save(_SAMPLE)
    second = vault_path.read_bytes()
    # Sel (16 octets) puis nonce (12 octets) : aucun des deux ne doit se répéter.
    assert first[:16] != second[:16]
    assert first[16:28] != second[16:28]


def test_wrong_passphrase_is_rejected(
    vault_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    SecureStore(vault_path).save(_SAMPLE)
    monkeypatch.setenv(_PASSPHRASE_ENV, "wrong passphrase")
    with pytest.raises(SecureStoreError):
        SecureStore(vault_path).load()


def test_tampered_ciphertext_is_rejected(vault_path: Path) -> None:
    store = SecureStore(vault_path)
    store.save(_SAMPLE)
    data = bytearray(vault_path.read_bytes())
    data[-1] ^= 0x01  # un seul bit modifié dans le tag d'authentification
    vault_path.write_bytes(bytes(data))
    with pytest.raises(SecureStoreError):
        store.load()


def test_missing_passphrase_fails_closed(
    vault_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_PASSPHRASE_ENV)
    with pytest.raises(SecureStoreError):
        SecureStore(vault_path).save(_SAMPLE)
