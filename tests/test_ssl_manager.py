"""Tests for SSL certificate management."""
import ipaddress
from pathlib import Path
import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID


@pytest.fixture(autouse=True)
def _ssl_data_dir(data_dir, monkeypatch):
    """Point ssl_manager at temp data dir."""
    import app.ssl_manager as sm
    monkeypatch.setattr(sm, "_DATA_DIR", data_dir)
    monkeypatch.setenv("DATA_PATH", str(data_dir))


# ---------------------------------------------------------------------------
# generate_self_signed_cert
# ---------------------------------------------------------------------------

def test_generate_cert_for_ip():
    from app.ssl_manager import generate_self_signed_cert
    cert_pem, key_pem = generate_self_signed_cert("192.168.1.10")
    assert "BEGIN CERTIFICATE" in cert_pem
    assert "BEGIN RSA PRIVATE KEY" in key_pem or "BEGIN PRIVATE KEY" in key_pem


def test_generated_cert_has_correct_cn():
    from app.ssl_manager import generate_self_signed_cert
    cert_pem, _ = generate_self_signed_cert("myserver.local")
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "myserver.local"


def test_generated_cert_has_ip_san():
    from app.ssl_manager import generate_self_signed_cert
    cert_pem, _ = generate_self_signed_cert("10.0.0.1")
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    ips = san.value.get_values_for_type(x509.IPAddress)
    assert ipaddress.ip_address("10.0.0.1") in ips


def test_generated_cert_has_dns_san():
    from app.ssl_manager import generate_self_signed_cert
    cert_pem, _ = generate_self_signed_cert("myserver.home")
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    names = san.value.get_values_for_type(x509.DNSName)
    assert "myserver.home" in names


def test_generated_cert_includes_localhost():
    from app.ssl_manager import generate_self_signed_cert
    cert_pem, _ = generate_self_signed_cert("192.168.1.1")
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    names = san.value.get_values_for_type(x509.DNSName)
    assert "localhost" in names


def test_generated_cert_validity_two_years():
    from app.ssl_manager import generate_self_signed_cert
    import datetime
    cert_pem, _ = generate_self_signed_cert("192.168.1.1")
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    try:
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
    except AttributeError:
        delta = cert.not_valid_after - cert.not_valid_before
    assert delta.days >= 729


# ---------------------------------------------------------------------------
# save / remove / ssl_enabled
# ---------------------------------------------------------------------------

def test_ssl_not_enabled_by_default(data_dir):
    from app.ssl_manager import ssl_enabled
    assert ssl_enabled() is False


def test_save_ssl_files_creates_files(data_dir):
    from app.ssl_manager import generate_self_signed_cert, save_ssl_files, ssl_enabled
    cert_pem, key_pem = generate_self_signed_cert("192.168.1.1")
    save_ssl_files(cert_pem, key_pem)
    assert ssl_enabled() is True


def test_save_ssl_files_content_roundtrip(data_dir):
    from app.ssl_manager import generate_self_signed_cert, save_ssl_files, _cert_path, _key_path
    cert_pem, key_pem = generate_self_signed_cert("192.168.1.1")
    save_ssl_files(cert_pem, key_pem)
    assert _cert_path().read_text() == cert_pem
    assert _key_path().read_text() == key_pem


def test_remove_ssl_files(data_dir):
    from app.ssl_manager import generate_self_signed_cert, save_ssl_files, remove_ssl_files, ssl_enabled
    cert_pem, key_pem = generate_self_signed_cert("192.168.1.1")
    save_ssl_files(cert_pem, key_pem)
    remove_ssl_files()
    assert ssl_enabled() is False


def test_remove_ssl_files_when_none_exist(data_dir):
    from app.ssl_manager import remove_ssl_files
    remove_ssl_files()  # should not raise


# ---------------------------------------------------------------------------
# get_cert_info
# ---------------------------------------------------------------------------

def test_get_cert_info_none_when_no_cert(data_dir):
    from app.ssl_manager import get_cert_info
    assert get_cert_info() is None


def test_get_cert_info_returns_cn_and_expiry(data_dir):
    from app.ssl_manager import generate_self_signed_cert, save_ssl_files, get_cert_info
    cert_pem, key_pem = generate_self_signed_cert("192.168.1.50")
    save_ssl_files(cert_pem, key_pem)
    info = get_cert_info()
    assert info is not None
    assert info["cn"] == "192.168.1.50"
    assert "2028" in info["expires"] or "2027" in info["expires"]  # ~2 years from now
