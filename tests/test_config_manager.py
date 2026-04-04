import yaml

from app.config_manager import (
    add_host,
    delete_host,
    get_hosts,
    get_ssh_config,
    slugify,
    update_host,
    update_ssh_config,
)


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

def test_slugify_basic():
    assert slugify("Proxmox Main") == "proxmox-main"


def test_slugify_underscores():
    assert slugify("my_host") == "my-host"


def test_slugify_already_lower():
    assert slugify("pbs") == "pbs"


# ---------------------------------------------------------------------------
# get_hosts
# ---------------------------------------------------------------------------

def test_get_hosts_returns_all(config_file):
    hosts = get_hosts()
    assert len(hosts) == 2


def test_get_hosts_adds_slug(config_file):
    hosts = get_hosts()
    assert hosts[0]["slug"] == "test-host"
    assert hosts[1]["slug"] == "custom-user-host"


def test_get_hosts_filters_placeholders(config_file, monkeypatch):
    import yaml

    config = yaml.safe_load(config_file.read_text())
    config["hosts"].append({"name": "Unfilled", "host": "192.168.5.XXX"})
    config_file.write_text(yaml.dump(config))

    hosts = get_hosts()
    names = [h["name"] for h in hosts]
    assert "Unfilled" not in names


def test_get_hosts_empty_config(tmp_path, monkeypatch):
    import app.config_manager as cm

    cfg = tmp_path / "config.yml"
    cfg.write_text("ssh: {}\nhosts: []\n")
    monkeypatch.setattr(cm, "_CONFIG_PATH", cfg)

    assert get_hosts() == []


def test_load_config_missing_file_returns_defaults(tmp_path, monkeypatch):
    import app.config_manager as cm

    monkeypatch.setattr(cm, "_CONFIG_PATH", tmp_path / "nonexistent.yml")
    config = cm.load_config()
    assert config == {"hosts": [], "ssh": {}}


# ---------------------------------------------------------------------------
# add_host
# ---------------------------------------------------------------------------

def test_add_host_minimal(config_file):
    add_host(name="New Host", host="10.0.0.1", user=None, port=None, key=None)
    hosts = get_hosts()
    names = [h["name"] for h in hosts]
    assert "New Host" in names


def test_add_host_with_all_fields(config_file):
    add_host(name="Full Host", host="10.0.0.2", user="ubuntu", port=2222, key="/app/keys/other")
    hosts = get_hosts()
    host = next(h for h in hosts if h["name"] == "Full Host")
    assert host["user"] == "ubuntu"
    assert host["port"] == 2222
    assert host["key"] == "/app/keys/other"


def test_add_host_persists_to_file(config_file):
    add_host(name="Persisted", host="10.0.0.3", user=None, port=None, key=None)
    raw = yaml.safe_load(config_file.read_text())
    names = [h["name"] for h in raw["hosts"]]
    assert "Persisted" in names


def test_add_host_does_not_store_none_fields(config_file):
    add_host(name="Minimal", host="10.0.0.4", user=None, port=None, key=None)
    raw = yaml.safe_load(config_file.read_text())
    host = next(h for h in raw["hosts"] if h["name"] == "Minimal")
    assert "user" not in host
    assert "port" not in host
    assert "key" not in host


# ---------------------------------------------------------------------------
# update_host
# ---------------------------------------------------------------------------

def test_update_host_changes_name(config_file):
    update_host("test-host", name="Renamed Host", host="192.168.1.10", user=None, port=None, key=None)
    hosts = get_hosts()
    names = [h["name"] for h in hosts]
    assert "Renamed Host" in names
    assert "Test Host" not in names


def test_update_host_changes_ip(config_file):
    update_host("test-host", name="Test Host", host="10.10.10.10", user=None, port=None, key=None)
    hosts = get_hosts()
    host = next(h for h in hosts if h["name"] == "Test Host")
    assert host["host"] == "10.10.10.10"


def test_update_host_adds_optional_fields(config_file):
    update_host("test-host", name="Test Host", host="192.168.1.10", user="ubuntu", port=2222, key=None)
    hosts = get_hosts()
    host = next(h for h in hosts if h["name"] == "Test Host")
    assert host["user"] == "ubuntu"
    assert host["port"] == 2222


def test_update_host_unknown_slug_is_noop(config_file):
    before = get_hosts()
    update_host("nonexistent", name="X", host="1.2.3.4", user=None, port=None, key=None)
    after = get_hosts()
    assert len(before) == len(after)


# ---------------------------------------------------------------------------
# delete_host
# ---------------------------------------------------------------------------

def test_delete_host_removes_it(config_file):
    delete_host("test-host")
    names = [h["name"] for h in get_hosts()]
    assert "Test Host" not in names


def test_delete_host_leaves_others(config_file):
    delete_host("test-host")
    names = [h["name"] for h in get_hosts()]
    assert "Custom User Host" in names


def test_delete_host_unknown_slug_is_noop(config_file):
    before = len(get_hosts())
    delete_host("does-not-exist")
    assert len(get_hosts()) == before


# ---------------------------------------------------------------------------
# SSH config
# ---------------------------------------------------------------------------

def test_get_ssh_config_returns_defaults(config_file):
    ssh = get_ssh_config()
    assert ssh["default_user"] == "root"
    assert ssh["default_port"] == 22
    assert ssh["connect_timeout"] == 15


def test_update_ssh_config_persists(config_file):
    update_ssh_config(
        default_user="ubuntu",
        default_port=2222,
        default_key="/app/keys/new_key",
        connect_timeout=30,
        command_timeout=300,
    )
    ssh = get_ssh_config()
    assert ssh["default_user"] == "ubuntu"
    assert ssh["default_port"] == 2222
    assert ssh["connect_timeout"] == 30
    assert ssh["command_timeout"] == 300


def test_update_ssh_config_writes_to_file(config_file):
    update_ssh_config("admin", 22, "/app/keys/id_ed25519", 10, 120)
    raw = yaml.safe_load(config_file.read_text())
    assert raw["ssh"]["default_user"] == "admin"
