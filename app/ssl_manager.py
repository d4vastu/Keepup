"""SSL certificate management."""

import datetime
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_DATA_DIR = Path(os.getenv("DATA_PATH", "/app/data"))


def _ssl_dir() -> Path:
    return Path(os.getenv("DATA_PATH", "/app/data")) / "ssl"


def _cert_path() -> Path:
    return _ssl_dir() / "cert.pem"


def _key_path() -> Path:
    return _ssl_dir() / "key.pem"


def ssl_enabled() -> bool:
    return _cert_path().exists() and _key_path().exists()


def get_cert_info() -> dict | None:
    """Return basic info about current cert, or None if no cert installed."""
    if not _cert_path().exists():
        return None
    try:
        cert = x509.load_pem_x509_certificate(_cert_path().read_bytes())
        cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        cn = cn_attrs[0].value if cn_attrs else "unknown"
        # not_valid_after_utc is Python 3.10+; fall back to not_valid_after
        try:
            expires = cert.not_valid_after_utc.date().isoformat()
        except AttributeError:
            expires = cert.not_valid_after.date().isoformat()
        return {"cn": cn, "expires": expires}
    except Exception:
        return {"cn": "unknown", "expires": "unknown"}


def generate_self_signed_cert(hostname: str) -> tuple[str, str]:
    """Generate a 2-year self-signed cert for hostname (IP or DNS). Returns (cert_pem, key_pem)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Keepup"),
        ]
    )

    try:
        ip = ipaddress.ip_address(hostname)
        san_entries: list = [x509.IPAddress(ip)]
    except ValueError:
        san_entries = [x509.DNSName(hostname)]

    san_entries += [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=730))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


def save_ssl_files(cert_pem: str, key_pem: str) -> None:
    """Write cert and key to data/ssl/."""
    _ssl_dir().mkdir(parents=True, exist_ok=True)
    _cert_path().write_text(cert_pem)
    _key_path().write_text(key_pem)
    _cert_path().chmod(0o644)
    _key_path().chmod(0o600)


def remove_ssl_files() -> None:
    """Delete cert and key files."""
    for f in (_cert_path(), _key_path()):
        if f.exists():
            f.unlink()
