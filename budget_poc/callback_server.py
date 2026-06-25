"""Serveur de callback local pour récupérer le code OAuth après la SCA.

Au retour de la banque, le navigateur est redirigé vers notre `redirect_url`
(https://127.0.0.1:.../callback?code=...&state=...). Ce module lève un serveur
HTTPS éphémère, lié à la *loopback uniquement*, le temps de capter cette unique
requête, puis s'arrête.

Sécurité :
- Bind sur 127.0.0.1 exclusivement : jamais joignable depuis le réseau.
- TLS avec certificat auto-signé (Enable Banking impose un redirect HTTPS).
- Vérification du `state` en temps constant (anti-CSRF du flux OAuth).
- Le `code` n'est jamais journalisé.
- Timeout : on n'attend pas indéfiniment.
"""

from __future__ import annotations

import hmac
import logging
import ssl
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .tls import ensure_self_signed_cert

logger = logging.getLogger(__name__)

# Délai max d'attente du retour de l'utilisateur (secondes).
_CALLBACK_TIMEOUT_SECONDS = 300


class _CallbackResult:
    """Conteneur mutable pour transmettre le résultat hors du handler."""

    def __init__(self) -> None:
        self.code: str | None = None
        self.error: str | None = None


def _make_handler(expected_state: str, result: _CallbackResult):
    """Construit un handler lié à l'état attendu et au conteneur de résultat."""

    class CallbackHandler(BaseHTTPRequestHandler):
        # On neutralise le logging par défaut (qui écrirait l'URL, donc le code).
        def log_message(self, *args: object) -> None:  # noqa: D401
            return

        def do_GET(self) -> None:  # noqa: N802 (nom imposé par BaseHTTPRequestHandler)
            parsed = urlparse(self.path)
            if parsed.path != config.CALLBACK_PATH:
                self._respond(404, "Not found")
                return

            query = parse_qs(parsed.query)
            received_state = query.get("state", [""])[0]
            code = query.get("code", [""])[0]

            # Vérification anti-CSRF : comparaison en temps constant.
            if not hmac.compare_digest(received_state, expected_state):
                result.error = "state_mismatch"
                self._respond(400, "Etat invalide. Autorisation rejetee.")
                return

            if not code:
                result.error = query.get("error", ["missing_code"])[0]
                self._respond(400, "Autorisation refusee ou incomplete.")
                return

            result.code = code
            self._respond(200, "Autorisation recue. Vous pouvez fermer cet onglet.")

        def _respond(self, status: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # Pas de mise en cache d'une page liée à un flux d'auth.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return CallbackHandler


def wait_for_code(expected_state: str, cert_path: Path, key_path: Path) -> str:
    """Démarre le serveur HTTPS local, attend un callback valide, retourne le code.

    Génère un certificat auto-signé si nécessaire, puis enveloppe le socket
    d'écoute dans une couche TLS. Lève une exception si le state ne correspond
    pas, si le code est absent, ou si le délai d'attente expire.
    """
    ensure_self_signed_cert(cert_path, key_path)

    result = _CallbackResult()
    handler = _make_handler(expected_state, result)
    server = HTTPServer((config.CALLBACK_HOST, config.CALLBACK_PORT), handler)

    # Contexte TLS serveur : on charge notre certificat auto-signé.
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    server.socket = ssl_context.wrap_socket(server.socket, server_side=True)

    # Timeout court par itération : permet de re-vérifier le deadline global
    # régulièrement entre deux connexions.
    server.timeout = 1.0
    deadline = time.monotonic() + _CALLBACK_TIMEOUT_SECONDS

    try:
        # On boucle plutôt que de traiter une seule requête : avec un certificat
        # auto-signé, le navigateur ouvre des connexions parasites avant la vraie
        # requête /callback (handshake TLS abandonné le temps que l'utilisateur
        # accepte l'avertissement, favicon, preconnect...). Un seul handle_request
        # serait consommé par l'une d'elles. On continue jusqu'à capter le code,
        # rencontrer une erreur explicite (state invalide), ou expirer.
        while result.code is None and result.error is None:
            if time.monotonic() >= deadline:
                break
            # Les handshakes TLS échoués lèvent une OSError interceptée par la
            # stack http.server, qui rend la main sans planter : la boucle absorbe.
            server.handle_request()
    finally:
        server.server_close()

    if result.error == "state_mismatch":
        raise RuntimeError(
            "Le paramètre 'state' du callback ne correspond pas : "
            "tentative de CSRF possible, autorisation rejetée."
        )
    if result.code is None:
        raise RuntimeError(
            f"Aucun code d'autorisation reçu (raison : {result.error or 'timeout'})."
        )
    return result.code
