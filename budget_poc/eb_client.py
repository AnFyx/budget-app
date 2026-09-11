"""Client de l'API Enable Banking (périmètre AIS, lecture seule).

Ce module ne fait *que* lire : liste des banques, ouverture de session après
consentement, comptes, soldes, transactions. Il n'expose volontairement aucun
endpoint de paiement (PIS), pour qu'une erreur de code ne puisse pas initier de
virement. Le scope du consentement demandé est restreint à `balances` et
`transactions`.

Toutes les réponses réseau sont traitées comme des entrées non fiables :
parsées en JSON sûr et validées avant usage.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import jwt  # PyJWT
import requests
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from . import config

logger = logging.getLogger(__name__)

# Garde-fou de pagination : au-delà, on échoue fermé plutôt que de boucler.
_MAX_TRANSACTION_PAGES = 200


class EnableBankingError(RuntimeError):
    """Erreur d'interaction avec l'API Enable Banking."""


def _load_private_key(path: Path) -> Any:
    """Charge la clé privée RSA. Supporte une clé chiffrée par passphrase.

    Si la clé PEM est protégée, la passphrase est lue dans la variable
    d'environnement EB_PRIVATE_KEY_PASSPHRASE (recommandé). Une clé en clair
    sur le disque est tolérée mais déconseillée.
    """
    import os

    pem_data = path.read_bytes()
    key_passphrase = os.environ.get("EB_PRIVATE_KEY_PASSPHRASE")
    password = key_passphrase.encode("utf-8") if key_passphrase else None
    try:
        return load_pem_private_key(pem_data, password=password)
    except (ValueError, TypeError) as exc:
        # Mauvaise passphrase, format invalide, ou clé chiffrée sans passphrase
        # fournie. Message générique : pas de détail cryptographique en clair.
        raise EnableBankingError(
            "Chargement de la clé privée impossible "
            "(format invalide ou passphrase manquante/incorrecte)."
        ) from exc


def _path_segment(value: str) -> str:
    """Encode un identifiant pour l'insérer dans un chemin d'URL.

    Les identifiants viennent de réponses de l'API (donc non fiables) : un `/` ou
    un `..` ne doit pas pouvoir modifier la route appelée.
    """
    return quote(value, safe="")


class EnableBankingClient:
    """Client minimal et défensif pour l'API Account Information."""

    def __init__(self, settings: config.Settings) -> None:
        self._settings = settings
        self._private_key = _load_private_key(settings.private_key_path)
        self._session = requests.Session()

    # --- Authentification applicative -------------------------------------

    def _build_jwt(self) -> str:
        """Génère un JWT RS256 signé, valable quelques minutes.

        Le `kid` (header) identifie l'application auprès d'Enable Banking ;
        c'est l'ID obtenu à l'enregistrement. La clé privée correspond au
        certificat public uploadé à ce moment-là.
        """
        now = dt.datetime.now(tz=dt.timezone.utc)
        payload = {
            "iss": config.JWT_ISSUER,
            "aud": config.JWT_AUDIENCE,
            "iat": now,
            "exp": now + dt.timedelta(seconds=config.JWT_TTL_SECONDS),
        }
        return jwt.encode(
            payload,
            self._private_key,
            algorithm="RS256",
            headers={"kid": self._settings.application_id},
        )

    def _headers(self) -> dict[str, str]:
        """En-têtes communs : Bearer JWT régénéré à chaque appel."""
        return {
            "Authorization": f"Bearer {self._build_jwt()}",
            "Accept": "application/json",
        }

    # --- Appels HTTP bas niveau -------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, json_body=body)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        """Effectue un appel HTTP et retourne le JSON parsé, défensivement.

        - TLS vérifié (défaut de requests, jamais désactivé).
        - Timeout systématique (anti-blocage).
        - Erreurs HTTP remontées sans exposer le corps brut potentiellement
          sensible dans le message.
        """
        url = f"{config.ENABLE_BANKING_BASE_URL}{path}"
        try:
            response = self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=config.HTTP_TIMEOUT_SECONDS,
                # verify=True par défaut : la vérification de certificat reste active.
            )
        except requests.RequestException as exc:
            raise EnableBankingError(f"Échec réseau sur {method} {path}.") from exc

        if response.status_code >= 400:
            # On logge le code mais pas le corps (peut contenir des détails de compte).
            logger.warning("Réponse %s sur %s %s", response.status_code, method, path)
            raise EnableBankingError(
                f"Erreur API ({response.status_code}) sur {method} {path}."
            )

        try:
            parsed = response.json()
        except ValueError as exc:
            raise EnableBankingError("Réponse non-JSON inattendue.") from exc
        if not isinstance(parsed, dict):
            raise EnableBankingError("Structure de réponse inattendue (non-objet).")
        return parsed

    # --- Endpoints AIS (lecture seule) ------------------------------------

    def list_aspsps(self, country: str | None = None) -> list[dict]:
        """Liste les banques disponibles pour un pays (ISO 3166-2)."""
        country_code = country or self._settings.aspsp_country
        data = self._get("/aspsps", params={"country": country_code})
        aspsps = data.get("aspsps")
        if not isinstance(aspsps, list):
            raise EnableBankingError("Liste d'ASPSP absente ou malformée.")
        return aspsps

    def start_authorization(self, state: str) -> str:
        """Initie l'autorisation et retourne l'URL vers laquelle rediriger le PSU.

        Le scope est restreint à la lecture (balances + transactions). `state`
        est un anti-CSRF : il sera revérifié au retour du callback.
        """
        valid_until = (
            dt.datetime.now(tz=dt.timezone.utc)
            + dt.timedelta(days=self._settings.access_validity_days)
        ).isoformat()
        body = {
            "access": {
                "balances": True,
                "transactions": True,
                "valid_until": valid_until,
            },
            "aspsp": {
                "name": self._settings.aspsp_name,
                "country": self._settings.aspsp_country,
            },
            "psu_type": "personal",
            "redirect_url": self._settings.redirect_url,
            "state": state,
        }
        data = self._post("/auth", body)
        auth_url = data.get("url")
        if not isinstance(auth_url, str) or not auth_url.startswith("https://"):
            raise EnableBankingError("URL d'autorisation absente ou non sécurisée.")
        return auth_url

    def create_session(self, code: str) -> dict:
        """Échange le code reçu au callback contre une session + liste de comptes.

        Retourne le dict brut (session_id + accounts). Certains champs ne sont
        renvoyés qu'une fois : l'appelant doit persister immédiatement.
        """
        data = self._post("/sessions", {"code": code})
        if "session_id" not in data or "accounts" not in data:
            raise EnableBankingError("Réponse de session incomplète.")
        return data

    def get_transactions(
        self, account_uid: str, date_from: str | None = None
    ) -> list[dict]:
        """Récupère TOUTES les transactions d'un compte, page par page.

        L'API peut répartir le résultat sur plusieurs pages : tant que la réponse
        contient une `continuation_key` non nulle, on relance la requête avec
        cette clé. Une page peut être vide tout en annonçant une suite.

        `account_uid` provient de la création de session. `date_from` est au
        format ISO `AAAA-MM-JJ`.
        """
        if not account_uid:
            raise EnableBankingError("account_uid manquant.")
        path = f"/accounts/{_path_segment(account_uid)}/transactions"
        base_params: dict[str, str] = {"date_from": date_from} if date_from else {}

        transactions: list[dict] = []
        continuation_key: str | None = None
        for _ in range(_MAX_TRANSACTION_PAGES):
            params = dict(base_params)
            if continuation_key:
                params["continuation_key"] = continuation_key
            data = self._get(path, params=params or None)

            page = data.get("transactions")
            if not isinstance(page, list):
                raise EnableBankingError("Liste de transactions absente ou malformée.")
            transactions.extend(page)

            next_key = data.get("continuation_key")
            if not next_key:
                return transactions
            if not isinstance(next_key, str):
                raise EnableBankingError("Clé de continuation malformée.")
            if next_key == continuation_key:
                # La même clé renvoyée deux fois bouclerait indéfiniment.
                raise EnableBankingError("Pagination bloquée : clé de continuation répétée.")
            continuation_key = next_key

        raise EnableBankingError(
            f"Pagination interrompue : plus de {_MAX_TRANSACTION_PAGES} pages."
        )

    def revoke_session(self, session_id: str) -> None:
        """Ferme une session (et, si possible, le consentement côté banque)."""
        if not session_id:
            raise EnableBankingError("session_id manquant.")
        self._request("DELETE", f"/sessions/{_path_segment(session_id)}")
