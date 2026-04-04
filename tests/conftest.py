
import pytest
import yaml
from fastapi.testclient import TestClient
from pathlib import Path

# ---------------------------------------------------------------------------
# Compatibility patch: passlib 1.7.4 + bcrypt 5.0.0 detection
# bcrypt 5.0.0 raises ValueError for passwords > 72 bytes; patch detect_wrap_bug
# so it returns False (no bug) instead of crashing.
# ---------------------------------------------------------------------------

def _patch_passlib_bcrypt():
    try:
        import passlib.handlers.bcrypt as _ph_bcrypt
        import passlib.handlers.bcrypt as _pbcrypt_mod
        # Force-load the backend module to find the function
        import sys, types
        # Monkey-patch: once bcrypt loads its backend, the detect_wrap_bug
        # function is a local closure inside _load_backend_mixin. We can't
        # patch it directly, but we can ensure the backend is loaded with
        # bcrypt's hashpw accepting >72-byte passwords by patching bcrypt.hashpw.
        import bcrypt as _bcrypt_lib
        _orig_hashpw = _bcrypt_lib.hashpw
        def _safe_hashpw(password, salt):
            if len(password) > 72:
                password = password[:72]
            return _orig_hashpw(password, salt)
        _bcrypt_lib.hashpw = _safe_hashpw
    except Exception:
        pass

_patch_passlib_bcrypt()

SAMPLE_CONFIG = {
    "ssh": {
        "default_key": "/app/keys/id_ed25519",
        "default_user": "root",
        "default_port": 22,
        "connect_timeout": 15,
        "command_timeout": 600,
    },
    "hosts": [
        {"name": "Test Host", "host": "192.168.1.10"},
        {"name": "Custom User Host", "host": "192.168.1.20", "user": "admin", "port": 2222},
    ],
}


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Create a temp config.yml and point config_manager at it."""
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.dump(SAMPLE_CONFIG, default_flow_style=False))

    import app.config_manager as cm
    monkeypatch.setattr(cm, "_CONFIG_PATH", cfg)

    return cfg


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Create a temp data dir and point credentials module at it."""
    d = tmp_path / "data"
    d.mkdir()

    import app.credentials as creds
    monkeypatch.setattr(creds, "_DATA_DIR", d)
    monkeypatch.setattr(creds, "_SECRET_FILE", d / ".secret")
    monkeypatch.setattr(creds, "_CREDS_FILE", d / "credentials.json")

    # Also patch app.auth so get_session_secret() doesn't try to write /app/data
    import app.auth as auth
    monkeypatch.setattr(auth, "_DATA_DIR", d)
    monkeypatch.setattr(auth, "_SESSION_SECRET_FILE", d / ".session_secret")

    return d


@pytest.fixture
def client(config_file, data_dir, monkeypatch):
    """TestClient with env vars set and config/data dirs pointed at temp paths."""
    monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "test-api-key")
    monkeypatch.setenv("PORTAINER_VERIFY_SSL", "false")

    # Import after env vars are set so startup picks them up
    from app.main import app
    return TestClient(app)
