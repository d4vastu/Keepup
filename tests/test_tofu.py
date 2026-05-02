"""Tests for the TOFU (Trust On First Use) certificate pinning flow."""

import ssl
from unittest.mock import AsyncMock, patch

import pytest


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
        import ssl
        from unittest.mock import MagicMock, patch

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


class TestAdminPortainerTOFUFlow:
    """Tests for the admin Portainer test endpoint TOFU flow."""

    def test_ssl_error_returns_cert_prompt(self, client):
        """An SSL error causes the cert trust prompt to be rendered."""
        pem = _make_test_pem()
        with patch(
            "app.portainer_client.PortainerClient.get_endpoints",
            new=AsyncMock(side_effect=Exception("SSL certificate verify failed")),
        ), patch("app.admin.fetch_server_cert", return_value=pem):
            response = client.post(
                "/admin/integrations/portainer/test",
                data={"portainer_url": "https://portainer.test:9443", "portainer_api_key": "key"},
            )
        assert response.status_code == 200
        assert "Trust this certificate" in response.text
        assert "SHA-256" in response.text or "fingerprint" in response.text.lower()

    def test_ssl_error_shows_fingerprint_in_prompt(self, client):
        """Cert prompt contains the certificate fingerprint."""
        from app.cert_utils import fingerprint

        pem = _make_test_pem()
        expected_fp = fingerprint(pem)
        with patch(
            "app.portainer_client.PortainerClient.get_endpoints",
            new=AsyncMock(side_effect=Exception("SSL certificate verify failed")),
        ), patch("app.admin.fetch_server_cert", return_value=pem):
            response = client.post(
                "/admin/integrations/portainer/test",
                data={"portainer_url": "https://portainer.test:9443", "portainer_api_key": "key"},
            )
        assert expected_fp[:16] in response.text  # partial fingerprint match

    def test_trust_accepted_with_valid_cert_returns_connected(self, client):
        """trust_accepted=1 with a valid pending cert pins and succeeds."""
        pem = _make_test_pem()

        # Step 1: trigger SSL error to store pending cert in session
        with patch(
            "app.portainer_client.PortainerClient.get_endpoints",
            new=AsyncMock(side_effect=Exception("SSL certificate verify failed")),
        ), patch("app.admin.fetch_server_cert", return_value=pem):
            client.post(
                "/admin/integrations/portainer/test",
                data={"portainer_url": "https://portainer.test:9443", "portainer_api_key": "key"},
            )

        # Step 2: re-POST with trust_accepted=1; mock successful re-test
        with patch(
            "app.portainer_client.PortainerClient.get_endpoints",
            new=AsyncMock(return_value=[{"Id": 1}]),
        ):
            response = client.post(
                "/admin/integrations/portainer/test",
                data={
                    "portainer_url": "https://portainer.test:9443",
                    "portainer_api_key": "key",
                    "trust_accepted": "1",
                },
            )
        assert response.status_code == 200
        assert "Connected" in response.text or "pinned" in response.text.lower()

    def test_trust_accepted_without_pending_cert_shows_error(self, client):
        """trust_accepted=1 with no pending cert in session shows an error."""
        response = client.post(
            "/admin/integrations/portainer/test",
            data={
                "portainer_url": "https://portainer.test:9443",
                "portainer_api_key": "key",
                "trust_accepted": "1",
            },
        )
        assert response.status_code == 200
        # Should show an error, not silently succeed
        assert "Connected" not in response.text or "error" in response.text.lower()

    def test_ssl_fetch_failure_shows_ssl_error_message(self, client):
        """When cert fetch itself fails, a generic SSL error message is shown."""
        with patch(
            "app.portainer_client.PortainerClient.get_endpoints",
            new=AsyncMock(side_effect=Exception("SSL certificate verify failed")),
        ), patch("app.admin.fetch_server_cert", side_effect=Exception("connection refused")):
            response = client.post(
                "/admin/integrations/portainer/test",
                data={"portainer_url": "https://portainer.test:9443", "portainer_api_key": "key"},
            )
        assert response.status_code == 200
        assert "SSL" in response.text

    def test_cert_changed_shows_changed_prompt(self, client):
        """If a different cert is presented than the one pinned, a 'changed' prompt appears."""
        from app.cert_utils import fingerprint as fp_fn
        from app.config_manager import save_portainer_config
        from app.ssl_manager import generate_self_signed_cert

        # Pin an existing cert
        old_pem, _ = generate_self_signed_cert("old.local")
        old_fp = fp_fn(old_pem)
        save_portainer_config(
            url="https://portainer.test:9443",
            pinned_cert_pem=old_pem,
            pinned_fingerprint=old_fp,
        )

        # Server now presents a different cert
        new_pem, _ = generate_self_signed_cert("new.local")
        with patch(
            "app.portainer_client.PortainerClient.get_endpoints",
            new=AsyncMock(side_effect=Exception("SSL certificate verify failed")),
        ), patch("app.admin.fetch_server_cert", return_value=new_pem):
            response = client.post(
                "/admin/integrations/portainer/test",
                data={"portainer_url": "https://portainer.test:9443", "portainer_api_key": "key"},
            )
        assert response.status_code == 200
        assert "changed" in response.text.lower() or "Certificate changed" in response.text


# ---------------------------------------------------------------------------
# Setup wizard TOFU flow — Portainer (/setup/portainer/test)
# ---------------------------------------------------------------------------


class TestSetupPortainerTOFUFlow:
    """Tests for the setup wizard Portainer test endpoint TOFU flow."""

    def test_ssl_error_returns_cert_prompt(self, anon_client):
        """SSL error in setup wizard shows cert trust prompt."""
        pem = _make_test_pem()
        with patch(
            "app.portainer_client.PortainerClient.get_endpoints",
            new=AsyncMock(side_effect=Exception("SSL certificate verify failed")),
        ), patch("app.auth_router.fetch_server_cert", return_value=pem):
            response = anon_client.post(
                "/setup/portainer/test",
                data={"portainer_url": "https://portainer.test:9443", "portainer_api_key": "key"},
            )
        assert response.status_code == 200
        assert "Trust this certificate" in response.text

    def test_trust_accepted_returns_connected(self, anon_client):
        """trust_accepted=1 in setup wizard with valid pending cert succeeds."""
        pem = _make_test_pem()

        # Step 1: trigger SSL error
        with patch(
            "app.portainer_client.PortainerClient.get_endpoints",
            new=AsyncMock(side_effect=Exception("SSL certificate verify failed")),
        ), patch("app.auth_router.fetch_server_cert", return_value=pem):
            anon_client.post(
                "/setup/portainer/test",
                data={"portainer_url": "https://portainer.test:9443", "portainer_api_key": "key"},
            )

        # Step 2: trust
        with patch(
            "app.portainer_client.PortainerClient.get_endpoints",
            new=AsyncMock(return_value=[{"Id": 1}]),
        ):
            response = anon_client.post(
                "/setup/portainer/test",
                data={
                    "portainer_url": "https://portainer.test:9443",
                    "portainer_api_key": "key",
                    "trust_accepted": "1",
                },
            )
        assert response.status_code == 200
        assert "Connected" in response.text or "pinned" in response.text.lower()
