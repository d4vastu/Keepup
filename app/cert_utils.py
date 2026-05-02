"""
TOFU (Trust On First Use) certificate pinning utilities.

Handles cert fetching, fingerprint extraction, and building custom SSL contexts
so integrations can pin self-signed certificates without disabling TLS entirely.
"""
import hashlib
import socket
import ssl
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding


def fetch_server_cert(url: str) -> str:
    """Fetch the leaf TLS certificate from a server without verifying it.

    Returns the certificate as a PEM-encoded string.
    Raises on connection failure.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 443

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            der = ssock.getpeercert(binary_form=True)

    cert = x509.load_der_x509_certificate(der)
    return cert.public_bytes(Encoding.PEM).decode("ascii")


def fingerprint(pem: str) -> str:
    """Return the SHA-256 fingerprint of a PEM certificate as colon-separated hex."""
    cert = x509.load_pem_x509_certificate(pem.encode())
    der = cert.public_bytes(Encoding.DER)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def cert_info(pem: str) -> dict:
    """Parse a PEM certificate and return display fields for the TOFU UI."""
    cert = x509.load_pem_x509_certificate(pem.encode())

    def _cn(name: x509.Name) -> str:
        attrs = name.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        return attrs[0].value if attrs else str(name)

    fp = fingerprint(pem)
    return {
        "fingerprint": fp,
        "subject_cn": _cn(cert.subject),
        "issuer_cn": _cn(cert.issuer),
        "not_before": cert.not_valid_before_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "not_after": cert.not_valid_after_utc.strftime("%Y-%m-%d %H:%M UTC"),
    }


def build_pinned_ssl_ctx(pem: str) -> ssl.SSLContext:
    """Return an SSLContext that trusts only the pinned certificate.

    check_hostname is disabled because self-signed certs often use an IP
    address that doesn't match the cert's CN/SAN.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=pem)
    return ctx
