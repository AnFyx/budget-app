"""Configuration du PoC, chargée depuis l'environnement.

Principe de sécurité : *fail-closed*. Si un paramètre sensible manque, on
refuse de démarrer plutôt que de retomber sur une valeur par défaut permissive.
Aucun secret n'est écrit en dur ici : tout vient de variables d'environnement
(typiquement injectées via un fichier `.env` non versionné).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# --- Constantes non sensibles (pas des secrets, donc acceptables en clair) ---

# Base de l'API Enable Banking (production). Voir leur API reference.
ENABLE_BANKING_BASE_URL = "https://api.enablebanking.com"

# Émetteur / audience attendus dans le JWT d'authentification applicative.
# Source : quick-start Enable Banking. Si l'auth renvoie 401, c'est le premier
# point à revérifier dans la doc « jwtAuthentication ».
JWT_ISSUER = "enablebanking.com"
JWT_AUDIENCE = "api.enablebanking.com"

# Durée de vie du JWT d'auth (court, on le régénère à chaque besoin).
JWT_TTL_SECONDS = 600

# Timeout réseau (connexion, lecture) en secondes, appliqué à tout appel HTTP.
HTTP_TIMEOUT_SECONDS = 30.0

# Adresse du petit serveur local qui capte le code OAuth au retour de la banque.
# 127.0.0.1 uniquement : jamais exposé sur le réseau.
# HTTPS imposé : Enable Banking refuse un redirect en http, même en loopback.
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8765
CALLBACK_PATH = "/callback"
CALLBACK_SCHEME = "https"


class ConfigError(RuntimeError):
    """Levée quand un paramètre obligatoire est absent ou invalide."""


def _require_env(name: str) -> str:
    """Retourne la variable d'environnement `name` ou échoue fermé."""
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"Variable d'environnement obligatoire absente : {name}. "
            f"Voir .env.example."
        )
    return value


@dataclass(frozen=True)
class Settings:
    """Paramètres résolus du PoC. Immutable une fois construit."""

    application_id: str  # ID d'application Enable Banking (= `kid` du JWT)
    private_key_path: Path  # chemin du .pem RSA téléchargé à l'enregistrement
    aspsp_name: str  # nom exact de la banque (ex. « BNP Paribas »)
    aspsp_country: str  # code pays ISO 3166-2 (ex. « FR »)
    data_dir: Path  # répertoire de stockage chiffré (hors dépôt git)
    access_validity_days: int  # durée de consentement demandée (la banque peut réduire)

    @property
    def redirect_url(self) -> str:
        """URL de callback locale, à enregistrer dans le control panel Enable Banking."""
        return (
            f"{CALLBACK_SCHEME}://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
        )

    @property
    def callback_cert_path(self) -> Path:
        """Chemin du certificat auto-signé du callback HTTPS."""
        return self.data_dir / "callback_cert.pem"

    @property
    def callback_key_path(self) -> Path:
        """Chemin de la clé privée du certificat de callback."""
        return self.data_dir / "callback_key.pem"

    @property
    def db_path(self) -> Path:
        """Chemin de la base SQLite locale des transactions (non chiffrée)."""
        return self.data_dir / "transactions.db"

    @property
    def rules_path(self) -> Path:
        """Chemin du fichier de règles de catégorisation (éditable)."""
        return self.data_dir / "rules.json"


def load_settings() -> Settings:
    """Construit les paramètres depuis l'environnement, en échouant fermé."""
    private_key_path = Path(_require_env("EB_PRIVATE_KEY_PATH")).expanduser()
    if not private_key_path.is_file():
        raise ConfigError(f"Clé privée introuvable : {private_key_path}")

    data_dir = Path(os.environ.get("EB_DATA_DIR", "~/.bnp-budget-data")).expanduser()

    try:
        validity_days = int(os.environ.get("EB_ACCESS_VALIDITY_DAYS", "90"))
    except ValueError as exc:
        raise ConfigError("EB_ACCESS_VALIDITY_DAYS doit être un entier.") from exc
    if not 1 <= validity_days <= 180:
        raise ConfigError("EB_ACCESS_VALIDITY_DAYS doit être entre 1 et 180.")

    return Settings(
        application_id=_require_env("EB_APPLICATION_ID"),
        private_key_path=private_key_path,
        aspsp_name=_require_env("EB_ASPSP_NAME"),
        aspsp_country=os.environ.get("EB_ASPSP_COUNTRY", "FR"),
        data_dir=data_dir,
        access_validity_days=validity_days,
    )
