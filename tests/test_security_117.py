"""
Tests for OP#117 — Audit logging for sensitive operations.

Covers:
  - Audit log file is created at data_dir/audit.log
  - Each entry has the required shape: ts, request_id, actor, source_ip,
    action, target, result, details
  - All covered sensitive actions emit an entry with the expected action string
  - Credentials are never written to the audit log (redaction test)
  - Log rotation handler is configured (RotatingFileHandler)
"""

import json
import logging.handlers
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import app.audit as audit_mod
from app.audit import audit, setup_audit_log


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_entries(data_dir: Path) -> list[dict]:
    log_path = data_dir / "audit.log"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def _last_entry(data_dir: Path) -> dict:
    entries = _read_entries(data_dir)
    assert entries, "audit.log is empty"
    return entries[-1]


def _entries_with_action(data_dir: Path, action: str) -> list[dict]:
    return [e for e in _read_entries(data_dir) if e.get("action") == action]


def _assert_valid_shape(entry: dict) -> None:
    required = {"ts", "request_id", "actor", "source_ip", "action", "target", "result", "details"}
    missing = required - entry.keys()
    assert not missing, f"audit entry missing fields: {missing}"
    assert entry["ts"]
    assert entry["request_id"]


# ---------------------------------------------------------------------------
# Unit tests — audit module
# ---------------------------------------------------------------------------


def test_setup_creates_rotating_handler(tmp_path):
    """setup_audit_log configures a RotatingFileHandler."""
    setup_audit_log(tmp_path)
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in audit_mod._audit_log.handlers)
    for h in list(audit_mod._audit_log.handlers):
        audit_mod._audit_log.removeHandler(h)
        h.close()


def test_audit_entry_shape(data_dir):
    """audit() writes a valid JSON entry with all required keys."""
    req = MagicMock()
    req.state.request_id = "test-req-id"
    req.headers.get.return_value = None
    req.client.host = "10.0.0.1"

    with patch("app.auth.get_admin_username", return_value="testadmin"):
        audit(req, "test.action", target="test-target", result="ok", details={"k": "v"})

    entry = _last_entry(data_dir)
    assert entry["ts"]
    assert entry["request_id"] == "test-req-id"
    assert entry["actor"] == "testadmin"
    assert entry["source_ip"] == "10.0.0.1"
    assert entry["action"] == "test.action"
    assert entry["target"] == "test-target"
    assert entry["result"] == "ok"
    assert entry["details"] == {"k": "v"}


def test_audit_x_forwarded_for(data_dir):
    """source_ip reads the first address from X-Forwarded-For."""
    req = MagicMock()
    req.state.request_id = "req-fwd"
    req.headers.get.return_value = "203.0.113.5, 10.0.0.1"
    req.client.host = "172.16.0.1"

    with patch("app.auth.get_admin_username", return_value="testadmin"):
        audit(req, "test.ip", actor="testadmin")

    assert _last_entry(data_dir)["source_ip"] == "203.0.113.5"


def test_audit_generates_request_id_when_missing(data_dir):
    """If request.state has no request_id, a UUID is generated."""
    req = MagicMock(spec=[])  # spec=[] → no attributes → getattr returns MagicMock but not AttributeError
    req.headers = MagicMock()
    req.headers.get.return_value = None
    req.client = MagicMock()
    req.client.host = "10.0.0.2"
    # state has no request_id attribute
    req.state = MagicMock(spec=[])

    with patch("app.auth.get_admin_username", return_value="testadmin"):
        audit(req, "test.no_id", actor="testadmin")

    entry = _last_entry(data_dir)
    # UUID4 has 36 characters (with hyphens)
    assert len(entry["request_id"]) == 36


# ---------------------------------------------------------------------------
# Integration tests — sensitive actions via HTTP
# ---------------------------------------------------------------------------


def test_login_success_audit(anon_client, data_dir):
    """Successful login emits auth.login.success."""
    anon_client.post(
        "/login",
        data={"username": "testadmin", "password": "testpassword123"},
        follow_redirects=False,
    )
    entries = _entries_with_action(data_dir, "auth.login.success")
    assert entries, "expected auth.login.success entry"
    e = entries[-1]
    assert e["result"] == "ok"
    assert e["actor"] == "testadmin"
    _assert_valid_shape(e)


def test_login_failure_audit(anon_client, data_dir):
    """Failed login emits auth.login.failure."""
    anon_client.post(
        "/login",
        data={"username": "testadmin", "password": "wrongpassword"},
        follow_redirects=False,
    )
    entries = _entries_with_action(data_dir, "auth.login.failure")
    assert entries, "expected auth.login.failure entry"
    e = entries[-1]
    assert e["result"] == "denied"
    assert e["actor"] == "testadmin"
    _assert_valid_shape(e)


def test_password_change_audit(client, data_dir):
    """Password change emits account.password.change."""
    client.post(
        "/admin/account/password",
        data={
            "current_password": "testpassword123",
            "new_password": "newsecurepassword123",
            "new_password_confirm": "newsecurepassword123",
        },
    )
    entries = _entries_with_action(data_dir, "account.password.change")
    assert entries, "expected account.password.change entry"
    _assert_valid_shape(entries[-1])


def test_backup_key_regen_audit(client, data_dir):
    """Backup key regeneration emits account.backup_key.regen."""
    client.post(
        "/admin/account/backup-key",
        data={"current_password": "testpassword123"},
    )
    entries = _entries_with_action(data_dir, "account.backup_key.regen")
    assert entries, "expected account.backup_key.regen entry"
    _assert_valid_shape(entries[-1])


def test_mfa_enroll_audit(client, data_dir):
    """MFA enrollment emits account.mfa.enroll."""
    # GET stores the TOTP secret in session; POST verifies and enrolls
    client.get("/admin/account/mfa/setup")
    with patch("app.admin.pyotp") as mock_pyotp:
        mock_totp = MagicMock()
        mock_totp.verify.return_value = True
        mock_pyotp.TOTP.return_value = mock_totp
        client.post(
            "/admin/account/mfa/setup",
            data={"totp_code": "123456"},
        )
    entries = _entries_with_action(data_dir, "account.mfa.enroll")
    assert entries, "expected account.mfa.enroll entry"
    _assert_valid_shape(entries[-1])


def test_mfa_remove_audit(client, data_dir):
    """MFA removal emits account.mfa.remove."""
    with patch("app.admin.verify_password", return_value=True):
        with patch("app.admin.verify_totp", return_value=True):
            client.post(
                "/admin/account/mfa/remove",
                data={"current_password": "testpassword123", "totp_code": "123456"},
            )
    entries = _entries_with_action(data_dir, "account.mfa.remove")
    assert entries, "expected account.mfa.remove entry"
    _assert_valid_shape(entries[-1])


def test_factory_reset_audit(client, data_dir):
    """Factory reset emits account.factory_reset (logged before the wipe)."""
    client.post(
        "/admin/account/factory-reset",
        data={"current_password": "testpassword123", "confirm_text": "RESET"},
    )
    entries = _entries_with_action(data_dir, "account.factory_reset")
    assert entries, "expected account.factory_reset entry"
    e = entries[-1]
    assert e["result"] == "ok"
    _assert_valid_shape(e)


def test_credential_save_audit(client, data_dir):
    """Saving SSH credentials emits credential.save for the correct host slug."""
    client.post(
        "/admin/hosts/test-host/credentials",
        data={
            "auth_method": "password",
            "ssh_password": "supersecretpassword",
            "ssh_key": "",
            "sudo_password": "",
        },
    )
    entries = _entries_with_action(data_dir, "credential.save")
    assert entries, "expected credential.save entry"
    e = entries[-1]
    assert e["target"] == "test-host"
    assert e["result"] == "ok"
    assert e["details"]["auth_method"] == "password"
    _assert_valid_shape(e)


def test_credential_save_no_secret_in_log(client, data_dir):
    """The SSH password must NOT appear anywhere in the audit log."""
    secret_value = "topsecretpassword_XYZ987"
    client.post(
        "/admin/hosts/test-host/credentials",
        data={
            "auth_method": "password",
            "ssh_password": secret_value,
            "ssh_key": "",
            "sudo_password": "",
        },
    )
    log_text = (data_dir / "audit.log").read_text()
    assert secret_value not in log_text, "credential value must not appear in audit log"


def test_host_upgrade_trigger_audit(client, data_dir):
    """Triggering a host upgrade emits host.upgrade.trigger."""
    with patch("app.main.run_host_update_buffered", new=AsyncMock(return_value=[])):
        client.post("/api/host/test-host/update", data={})
    entries = _entries_with_action(data_dir, "host.upgrade.trigger")
    assert entries, "expected host.upgrade.trigger entry"
    e = entries[-1]
    assert e["target"] == "test-host"
    assert e["result"] == "ok"
    assert "job_id" in e["details"]
    _assert_valid_shape(e)


def test_host_reboot_trigger_audit(client, data_dir):
    """Triggering a host reboot emits host.reboot.trigger."""
    with patch("app.main.reboot_host", new=AsyncMock(return_value=None)):
        client.post("/api/host/test-host/restart", data={"confirmed": "yes"})
    entries = _entries_with_action(data_dir, "host.reboot.trigger")
    assert entries, "expected host.reboot.trigger entry"
    e = entries[-1]
    assert e["target"] == "test-host"
    _assert_valid_shape(e)


def test_host_delete_audit(client, data_dir):
    """Deleting a host emits host.delete."""
    client.delete("/admin/hosts/test-host")
    entries = _entries_with_action(data_dir, "host.delete")
    assert entries, "expected host.delete entry"
    e = entries[-1]
    assert e["target"] == "test-host"
    _assert_valid_shape(e)
