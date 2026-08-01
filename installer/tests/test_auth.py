"""
Unit tests for token reading and session-secret persistence. These paths
default to real /etc locations in app.py, so every test monkeypatches the
relevant module-level constant to a tmp_path file instead of touching the
real filesystem.

Run with:  pip install pytest && pytest
"""
import stat

import app as anvil


def test_get_token_returns_none_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(anvil, "TOKEN_PATH", str(tmp_path / "token"))
    assert anvil.get_token() is None


def test_get_token_reads_and_strips_whitespace(tmp_path, monkeypatch):
    token_path = tmp_path / "token"
    token_path.write_text("  my-secret-token  \n")
    monkeypatch.setattr(anvil, "TOKEN_PATH", str(token_path))
    assert anvil.get_token() == "my-secret-token"


def test_get_or_create_secret_key_creates_and_persists(tmp_path):
    secret_path = tmp_path / "session_secret"
    assert not secret_path.exists()

    first = anvil.get_or_create_secret_key(str(secret_path))
    assert secret_path.exists()
    assert len(first) == 64  # secrets.token_hex(32) -> 64 hex chars

    # The whole point: calling it again (as happens on every process
    # restart) must return the SAME value, not silently invalidate every
    # existing session cookie.
    second = anvil.get_or_create_secret_key(str(secret_path))
    assert second == first


def test_get_or_create_secret_key_is_root_only_permissions(tmp_path):
    secret_path = tmp_path / "session_secret"
    anvil.get_or_create_secret_key(str(secret_path))
    mode = stat.S_IMODE(secret_path.stat().st_mode)
    assert mode == 0o600


def test_get_or_create_secret_key_falls_back_gracefully_when_unwritable(tmp_path, monkeypatch):
    # Simulate a path that can't be written (e.g. preview mode on Windows
    # with no access to /etc) — should return a usable secret instead of
    # raising, not silently crash the app on import. Forced via monkeypatch
    # rather than real permission bits, since tests may run as root (which
    # ignores permission bits and would make this test meaningless).
    def _raise(*args, **kwargs):
        raise PermissionError("simulated: can't create /etc/anvil-installer here")
    monkeypatch.setattr(anvil.os, "makedirs", _raise)

    result = anvil.get_or_create_secret_key(str(tmp_path / "session_secret"))
    assert isinstance(result, str) and len(result) == 64
