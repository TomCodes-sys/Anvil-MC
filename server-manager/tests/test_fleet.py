"""
Tests for Bedrock detection and the Crafty API token's external persistence
(the fix for it surviving an uninstall/reinstall of this app).

Run with:  pip install pytest && pytest
"""
import app as anvil


def test_is_bedrock_world_true_when_marker_present(tmp_path):
    (tmp_path / anvil.BEDROCK_MARKER_FILE).touch()
    assert anvil._is_bedrock_world(str(tmp_path)) is True


def test_is_bedrock_world_false_for_java_server(tmp_path):
    (tmp_path / "server.jar").touch()
    assert anvil._is_bedrock_world(str(tmp_path)) is False


def test_is_bedrock_world_false_for_nonexistent_path():
    assert anvil._is_bedrock_world("/does/not/exist/at/all") is False


def test_external_crafty_api_persists_across_calls(tmp_path, monkeypatch):
    ext_path = tmp_path / "crafty_api.json"
    monkeypatch.setattr(anvil, "EXTERNAL_CRAFTY_API_PATH", ext_path)
    anvil._save_external_crafty_api("https://192.168.1.50:8443", "sometoken")
    url, token = anvil._load_external_crafty_api()
    assert url == "https://192.168.1.50:8443"
    assert token == "sometoken"


def test_external_crafty_api_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(anvil, "EXTERNAL_CRAFTY_API_PATH", tmp_path / "does_not_exist.json")
    url, token = anvil._load_external_crafty_api()
    assert url == "" and token == ""


def test_load_settings_recovers_crafty_api_after_simulated_uninstall(tmp_path, monkeypatch):
    # Simulates: token was saved, then the Danger Zone uninstall cleared
    # /opt/anvilmc/server-manager/data (settings.json gone), then reinstalled
    # (fresh empty settings.json). The externally-persisted copy should be
    # picked back up automatically instead of leaving the Crafty API blank again.
    settings_file = tmp_path / "settings.json"
    ext_path = tmp_path / "crafty_api.json"
    monkeypatch.setattr(anvil, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(anvil, "EXTERNAL_CRAFTY_API_PATH", ext_path)

    s = anvil.load_settings()
    s["crafty_api"] = {"url": "https://192.168.1.50:8443", "token": "sometoken"}
    anvil.save_settings(s)

    settings_file.unlink()  # simulate the uninstall wiping /opt/.../data/settings.json

    recovered = anvil.load_settings()
    assert recovered["crafty_api"]["url"] == "https://192.168.1.50:8443"
    assert recovered["crafty_api"]["token"] == "sometoken"
