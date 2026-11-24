"""Tests for the TOFU (Trust On First Use) certificate pinning flow."""

import ssl
from unittest.mock import patch



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_pem() -> str:
    """Return a self-signed PEM cert for testing."""
    from app.ssl_manager import generate_self_signed_cert

    cert_pem, _ = generate_self_signed_cert("testserver.local")
    return cert_pem


# ---------------------------------------------------------------------------
# cert_utils unit tests
# ---------------------------------------------------------------------------


class TestCertUtils:
    def test_fingerprint_returns_colon_hex(self):
        from app.cert_utils import fingerprint

        pem = _make_test_pem()
        fp = fingerprint(pem)
        # SHA-256 = 32 bytes = 64 hex chars + 31 colons = 95 chars
        assert len(fp) == 95
        assert fp == fp.upper()
        parts = fp.split(":")
        assert len(parts) == 32
        assert all(len(p) == 2 for p in parts)

    def test_cert_info_returns_expected_fields(self):
        from app.cert_utils import cert_info

        pem = _make_test_pem()
        info = cert_info(pem)
        assert "fingerprint" in info
        assert "subject_cn" in info
        assert "issuer_cn" in info
        assert "not_before" in info
        assert "not_after" in info
        assert "testserver.local" in info["subject_cn"]

    def test_build_pinned_ssl_ctx_returns_ssl_context(self):
        from app.cert_utils import build_pinned_ssl_ctx

        pem = _make_test_pem()
        ctx = build_pinned_ssl_ctx(pem)
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert not ctx.check_hostname

    def test_fingerprint_is_stable(self):
        """Same cert always yields the same fingerprint."""
        from app.cert_utils import fingerprint

        pem = _make_test_pem()
        assert fingerprint(pem) == fingerprint(pem)

    def test_different_certs_have_different_fingerprints(self):
        from app.cert_utils import fingerprint
        from app.ssl_manager import generate_self_signed_cert

        pem_a, _ = generate_self_signed_cert("host-a.local")
        pem_b, _ = generate_self_signed_cert("host-b.local")
        assert fingerprint(pem_a) != fingerprint(pem_b)

    def test_fetch_server_cert_returns_pem(self):
        """fetch_server_cert wraps socket I/O and returns a PEM string."""
        from unittest.mock import MagicMock

        from app.cert_utils import fetch_server_cert
        from app.ssl_manager import generate_self_signed_cert
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding

        pem, _ = generate_self_signed_cert("testserver.local")
        cert_obj = x509.load_pem_x509_certificate(pem.encode())
        der_bytes = cert_obj.public_bytes(Encoding.DER)

        mock_ssock = MagicMock()
        mock_ssock.getpeercert.return_value = der_bytes
        mock_ssock.__enter__ = lambda s: s
        mock_ssock.__exit__ = MagicMock(return_value=False)

        mock_sock = MagicMock()
        mock_sock.__enter__ = lambda s: s
        mock_sock.__exit__ = MagicMock(return_value=False)

        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_ssock

        with patch("app.cert_utils.socket.create_connection", return_value=mock_sock), \
             patch("app.cert_utils.ssl.create_default_context", return_value=mock_ctx):
            result = fetch_server_cert("https://testserver.local:9443")

        assert result.startswith("-----BEGIN CERTIFICATE-----")
        assert result.strip().endswith("-----END CERTIFICATE-----")


# ---------------------------------------------------------------------------
# Admin TOFU flow — Portainer (/admin/integrations/portainer/test)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Setup wizard TOFU flow — Portainer (/setup/portainer/test)
# ---------------------------------------------------------------------------


