
import pytest
import yaml
from fastapi.testclient import TestClient
from pathlib import Path

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
