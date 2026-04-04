
import pytest
import yaml
from fastapi.testclient import TestClient

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
def client(config_file, monkeypatch):
    """TestClient with env vars set and config pointed at temp file."""
    monkeypatch.setenv("PORTAINER_URL", "https://portainer.test:9443")
    monkeypatch.setenv("PORTAINER_API_KEY", "test-api-key")
    monkeypatch.setenv("PORTAINER_VERIFY_SSL", "false")

    # Import after env vars are set so startup picks them up
    from app.main import app
    return TestClient(app)
