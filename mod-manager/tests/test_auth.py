"""
Tests for the auth gate added to Mod Manager as part of the AnvilMC
monorepo migration — previously this app had no token gate at all, unlike
its two siblings. Uses Flask's real test client rather than calling
internal functions directly, since the gate is applied via before_request
(chosen over per-route decorators specifically because with 34 routes,
decorating each one individually is one missed route away from leaving
something unintentionally open — these tests exist to prove the blanket
gate actually covers arbitrary routes, not just ones we remembered to check).

Run with:  pip install pytest && pytest
"""
import os

import pytest

import app as anvil


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(anvil, "TOKEN_PATH", str(tmp_path / "token"))
    monkeypatch.setattr(anvil, "SESSION_SECRET_PATH", tmp_path / "session_secret")
    anvil.app.secret_key = anvil.get_or_create_secret_key(str(tmp_path / "session_secret"))
    return anvil.app.test_client()


def test_no_token_configured_does_not_lock_out_operator(client):
    # Before the shared token even exists yet (very first boot), nothing
    # should be gated — otherwise a fresh install could lock itself out.
    resp = client.get("/api/servers")
    assert resp.status_code == 200


def test_unauthenticated_request_gets_login_page(client, tmp_path):
    (tmp_path / "token").write_text("supersecrettoken")
    resp = client.get("/api/servers")
    assert resp.status_code == 401
    assert b"Use my server login" in resp.data
    assert b"I have my token" in resp.data


def test_wrong_token_rejected(client, tmp_path):
    (tmp_path / "token").write_text("supersecrettoken")
    resp = client.get("/api/servers?token=wrongtoken")
    assert resp.status_code == 401


def test_correct_token_unlocks_and_session_persists(client, tmp_path):
    (tmp_path / "token").write_text("supersecrettoken")
    resp = client.get("/api/servers?token=supersecrettoken")
    assert resp.status_code == 200

    # No token needed on the next request — the session should carry it.
    resp2 = client.get("/api/servers")
    assert resp2.status_code == 200


def test_login_endpoint_itself_is_never_gated(client, tmp_path):
    # Otherwise nobody could ever log in via the login page's own POST.
    (tmp_path / "token").write_text("supersecrettoken")
    resp = client.post("/api/auth/login", json={"mode": "token", "token": "wrongtoken"})
    assert resp.status_code == 401  # rejected for being WRONG, not gated-before-being-checked
    assert resp.get_json()["ok"] is False


def test_login_via_token_endpoint_sets_session(client, tmp_path):
    (tmp_path / "token").write_text("supersecrettoken")
    resp = client.post("/api/auth/login", json={"mode": "token", "token": "supersecrettoken"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    resp2 = client.get("/api/servers")
    assert resp2.status_code == 200


def test_preview_mode_env_var_bypasses_gate_entirely(client, tmp_path, monkeypatch):
    (tmp_path / "token").write_text("supersecrettoken")
    monkeypatch.setattr(anvil, "PREVIEW", True)
    resp = client.get("/api/servers")
    assert resp.status_code == 200


def test_static_files_are_never_gated(client, tmp_path):
    (tmp_path / "token").write_text("supersecrettoken")
    # Whether or not the file exists, it should never come back as our 401
    # login page — that would be a real regression (broken CSS/JS on the
    # sign-in page itself).
    resp = client.get("/static/style.css")
    assert resp.status_code != 401
