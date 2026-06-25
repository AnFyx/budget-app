"""Stockage local chiffré au repos.

Objectif : aucune donnée bancaire (tokens de session, comptes, transactions)
n'est écrite en clair sur le disque. Tout passe par un chiffrement authentifié
AES-256-GCM, avec une clé dérivée d'une passphrase maître via scrypt.

Modèle de menace couvert : vol/lecture du disque ou d'une sauvegarde. La
passphrase n'est jamais persistée ; elle est fournie au runtime (variable
d'environnement ou saisie interactive). Sans elle, le contenu est inexploitable.

Hors périmètre : un attaquant ayant un accès root au processus en cours
d'exécution (la clé dérivée est alors en mémoire). C'est une limite inhérente ;
le durcissement de l'hôte relève du déploiement, pas de ce module.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

logger = logging.getLogger(__name__)

# Paramètres scrypt (coût CPU/mémoire). N=2**15 est un compromis raisonnable
# pour un poste de travail ; augmenter si la machine cible est plus puissante.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32  # AES-256
_SALT_LEN = 16
_NONCE_LEN = 12  # taille recommandée pour AES-GCM
_PASSPHRASE_ENV = "BUDGET_MASTER_PASSPHRASE"


class SecureStoreError(RuntimeError):
    """Erreur générique du stockage chiffré (sans détail sensible)."""


def _get_passphrase() -> bytes:
    """Récupère la passphrase maître depuis l'environnement, ou échoue fermé.

    On lit une variable d'environnement plutôt qu'un argument pour éviter
    qu'elle apparaisse dans la ligne de commande (visible via `ps`).
    """
    passphrase = os.environ.get(_PASSPHRASE_ENV)
    if not passphrase:
        raise SecureStoreError(
            f"Passphrase maître absente ({_PASSPHRASE_ENV}). "
            f"Le stockage chiffré ne peut pas être ouvert."
        )
    return passphrase.encode("utf-8")


def _derive_key(passphrase: bytes, salt: bytes) -> bytes:
    """Dérive une clé AES-256 à partir de la passphrase et d'un sel."""
    kdf = Scrypt(salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return kdf.derive(passphrase)


class SecureStore:
    """Coffre clé-valeur chiffré, persisté dans un unique fichier.

    Format du fichier (binaire) : salt(16) || nonce(12) || ciphertext+tag.
    Le sel et le nonce ne sont pas secrets ; seul le contenu l'est.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Restreint le répertoire de données au seul propriétaire (rwx------).
        _harden_permissions(self._path.parent, is_dir=True)

    def load(self) -> dict:
        """Déchiffre et retourne le contenu, ou un dict vide si absent."""
        if not self._path.is_file():
            return {}
        raw = self._path.read_bytes()
        if len(raw) < _SALT_LEN + _NONCE_LEN:
            raise SecureStoreError("Fichier de stockage corrompu ou tronqué.")
        salt = raw[:_SALT_LEN]
        nonce = raw[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
        ciphertext = raw[_SALT_LEN + _NONCE_LEN :]
        key = _derive_key(_get_passphrase(), salt)
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            # Mauvaise passphrase OU altération du fichier : on ne distingue pas
            # (et on ne logge pas le détail) pour ne pas faciliter une attaque.
            raise SecureStoreError(
                "Déchiffrement impossible : passphrase incorrecte ou données altérées."
            ) from exc
        return json.loads(plaintext.decode("utf-8"))

    def save(self, data: dict) -> None:
        """Chiffre et écrit le contenu de façon atomique."""
        salt = os.urandom(_SALT_LEN)
        nonce = os.urandom(_NONCE_LEN)  # unique par écriture : jamais réutilisé
        key = _derive_key(_get_passphrase(), salt)
        plaintext = json.dumps(data).encode("utf-8")
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)

        # Écriture atomique : on écrit dans un fichier temporaire puis on remplace,
        # pour ne jamais laisser un fichier à moitié écrit en cas d'interruption.
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_bytes(salt + nonce + ciphertext)
        _harden_permissions(tmp_path, is_dir=False)
        os.replace(tmp_path, self._path)
        logger.debug("Stockage chiffré mis à jour (%d clés).", len(data))


def _harden_permissions(path: Path, *, is_dir: bool) -> None:
    """Restreint les permissions au seul propriétaire (no-op hors POSIX)."""
    if os.name != "posix":
        return
    mode = 0o700 if is_dir else 0o600
    try:
        path.chmod(mode)
    except OSError as exc:
        # On remonte : des permissions trop ouvertes sur un secret est un défaut
        # de sécurité, pas un détail qu'on avale en silence.
        raise SecureStoreError(
            f"Impossible de restreindre les permissions de {path}."
        ) from exc
