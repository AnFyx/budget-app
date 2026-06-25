"""Génération d'un certificat auto-signé pour le callback HTTPS local.

Enable Banking exige un redirect en HTTPS, même en local. On génère donc un
certificat auto-signé pour 127.0.0.1, utilisé uniquement par le serveur de
callback éphémère sur la loopback.

Ce certificat n'est pas un secret au sens des données bancaires : il sert juste
à chiffrer un aller-retour local. Le navigateur affichera un avertissement
(certificat non reconnu par une autorité) qu'il faut accepter manuellement —
c'est attendu et sans risque ici, le trafic ne quitte pas la machine.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

# Validité du certificat local (court : on le régénère sans douleur).
_CERT_VALIDITY_DAYS = 365
_KEY_SIZE = 2048
_LOOPBACK_IP = "127.0.0.1"


def ensure_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    """Génère le couple (certificat, clé) s'il n'existe pas déjà.

    Les fichiers sont créés avec des permissions restreintes au propriétaire.
    Si les deux existent déjà, ne fait rien (réutilisation).
    """
    if cert_path.is_file() and key_path.is_file():
        return

    cert_path.parent.mkdir(parents=True, exist_ok=True)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    now = dt.datetime.now(tz=dt.timezone.utc)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, _LOOPBACK_IP)]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=_CERT_VALIDITY_DAYS))
        .add_extension(
            # SAN sur l'IP loopback : requis pour que les clients TLS valident.
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(_LOOPBACK_IP))]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    _harden(key_path)
    _harden(cert_path)
    logger.info("Certificat auto-signé généré pour le callback local.")


def _harden(path: Path) -> None:
    """Restreint les permissions du fichier au propriétaire (POSIX)."""
    if os.name == "posix":
        path.chmod(0o600)
