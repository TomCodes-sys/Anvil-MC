"""
Unit tests for the pure, no-network functions in app.py — mostly server
detection (loader, MC version, Bedrock, world name) and the small path/bucket
helpers. These are exactly the functions that have caused real bugs before
(NeoForge misidentified as Forge, data loss on update, etc.), so they're the
highest-value things to pin down with tests rather than relying on manual
curl/clicking to catch a regression.

Run with:  pip install pytest && pytest
"""
import json
import zipfile
from pathlib import Path

import pytest

import app as anvil


# ---------------------------------------------------------------------------
# detect_loader
# ---------------------------------------------------------------------------

def test_detect_loader_vanilla_when_no_markers(tmp_path):
    assert anvil.detect_loader(tmp_path) == "vanilla"


def test_detect_loader_fabric_marker_file(tmp_path):
    (tmp_path / "fabric-server-launch.jar").touch()
    assert anvil.detect_loader(tmp_path) == "fabric"


def test_detect_loader_neoforge_before_forge(tmp_path):
    # Regression test for the exact bug described in the ecosystem's memory:
    # NeoForge and Forge ship the same run.sh, so NeoForge's own marker
    # (libraries/net/neoforged) must be checked before Forge's, or a NeoForge
    # server gets misidentified as plain Forge.
    (tmp_path / "libraries" / "net" / "neoforged").mkdir(parents=True)
    (tmp_path / "libraries" / "net" / "minecraftforge").mkdir(parents=True)
    assert anvil.detect_loader(tmp_path) == "neoforge"


def test_detect_loader_forge_without_neoforge_marker(tmp_path):
    (tmp_path / "libraries" / "net" / "minecraftforge").mkdir(parents=True)
    assert anvil.detect_loader(tmp_path) == "forge"


def test_detect_loader_filename_fallback(tmp_path):
    (tmp_path / "paper-1.20.1-196.jar").touch()
    assert anvil.detect_loader(tmp_path) == "paper"


# ---------------------------------------------------------------------------
# detect_mc_version
# ---------------------------------------------------------------------------

def _make_jar_with_version(path: Path, version_id: str):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("version.json", json.dumps({"id": version_id}))


def test_detect_mc_version_from_embedded_version_json(tmp_path):
    _make_jar_with_version(tmp_path / "server.jar", "1.20.1")
    version, source = anvil.detect_mc_version(tmp_path)
    assert version == "1.20.1"
    assert source == "jar"


def test_detect_mc_version_from_forge_libraries_folder(tmp_path):
    (tmp_path / "libraries" / "net" / "minecraft" / "server" / "1.20.1").mkdir(parents=True)
    version, source = anvil.detect_mc_version(tmp_path)
    assert version == "1.20.1"
    assert source == "forge"


def test_detect_mc_version_prefers_jar_over_forge_folder(tmp_path):
    # If both exist, the embedded version.json (higher confidence) wins.
    _make_jar_with_version(tmp_path / "server.jar", "1.21.0")
    (tmp_path / "libraries" / "net" / "minecraft" / "server" / "1.20.1").mkdir(parents=True)
    version, source = anvil.detect_mc_version(tmp_path)
    assert version == "1.21.0"
    assert source == "jar"


def test_detect_mc_version_from_crafty_versions_folder(tmp_path):
    (tmp_path / "versions" / "1.19.4").mkdir(parents=True)
    version, source = anvil.detect_mc_version(tmp_path)
    assert version == "1.19.4"
    assert source == "versions_folder"


def test_detect_mc_version_versions_folder_picks_most_recently_modified(tmp_path):
    import os as _os
    old_dir = tmp_path / "versions" / "1.20.1"
    new_dir = tmp_path / "versions" / "1.19.4"  # numerically lower, but touched more recently
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    now = anvil.time.time() if hasattr(anvil, "time") else __import__("time").time()
    _os.utime(old_dir, (now - 100, now - 100))
    _os.utime(new_dir, (now, now))
    version, source = anvil.detect_mc_version(tmp_path)
    # Regression test: this used to sort by numeric value and always pick the
    # highest version number, which gets a real downgrade wrong. mtime (what
    # was actually touched most recently) is a better proxy for "what's
    # currently active" than raw numeric magnitude.
    assert version == "1.19.4"
    assert source == "versions_folder"


def test_detect_mc_version_jar_selection_is_deterministic_with_multiple_jars(tmp_path):
    # Regression test for "detects a random version": multiple jars used to
    # be scanned in whatever order the filesystem happened to return
    # (glob() gives no ordering guarantee), so an old leftover jar could win
    # over the one Crafty most recently wrote. The most recently modified
    # jar should always win now, regardless of directory iteration order.
    import os as _os
    old_jar = tmp_path / "server-old.jar"
    new_jar = tmp_path / "server.jar"
    _make_jar_with_version(old_jar, "1.19.4")
    _make_jar_with_version(new_jar, "1.20.1")
    now = __import__("time").time()
    _os.utime(old_jar, (now - 100, now - 100))
    _os.utime(new_jar, (now, now))
    version, source = anvil.detect_mc_version(tmp_path)
    assert version == "1.20.1"
    assert source == "jar"


def test_detect_mc_version_guess_from_filename_is_lowest_confidence(tmp_path):
    (tmp_path / "forge-1.20.1-47.2.20-installer.jar").touch()
    version, source = anvil.detect_mc_version(tmp_path)
    assert version == "1.20.1"
    assert source == "guess"


def test_detect_mc_version_nothing_found(tmp_path):
    version, source = anvil.detect_mc_version(tmp_path)
    assert version == ""
    assert source == ""


# ---------------------------------------------------------------------------
# is_bedrock_world
# ---------------------------------------------------------------------------

def test_is_bedrock_world_true_when_marker_present(tmp_path):
    (tmp_path / anvil.BEDROCK_MARKER_FILE).touch()
    assert anvil.is_bedrock_world(tmp_path) is True


def test_is_bedrock_world_false_for_java_server(tmp_path):
    (tmp_path / "server.jar").touch()
    assert anvil.is_bedrock_world(tmp_path) is False


# ---------------------------------------------------------------------------
# detect_world_name
# ---------------------------------------------------------------------------

def test_detect_world_name_reads_level_name(tmp_path):
    (tmp_path / "server.properties").write_text("level-name=MyWorld\nother-key=value\n")
    assert anvil.detect_world_name(tmp_path) == "MyWorld"


def test_detect_world_name_defaults_to_world(tmp_path):
    assert anvil.detect_world_name(tmp_path) == "world"


# ---------------------------------------------------------------------------
# bucket_for / dest_dir_for
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content_type,expected_bucket", [
    ("mod", "mods"), ("plugin", "plugins"), ("datapack", "datapacks"),
])
def test_bucket_for(content_type, expected_bucket):
    assert anvil.bucket_for(content_type) == expected_bucket


def test_dest_dir_for_datapack_uses_world_name(tmp_path):
    data = {"world_name": "survival"}
    dest = anvil.dest_dir_for(str(tmp_path), data, "datapack")
    assert dest == tmp_path / "survival" / "datapacks"


def test_dest_dir_for_mod(tmp_path):
    dest = anvil.dest_dir_for(str(tmp_path), {}, "mod")
    assert dest == tmp_path / "mods"


# ---------------------------------------------------------------------------
# required_dependency_project_ids (Modrinth branch only — no network needed)
# ---------------------------------------------------------------------------

def test_required_dependency_project_ids_modrinth_filters_to_required_only():
    version = {"raw_dependencies": [
        {"project_id": "abc", "dependency_type": "required"},
        {"project_id": "def", "dependency_type": "optional"},
        {"project_id": "ghi", "dependency_type": "incompatible"},
        {"project_id": "abc", "dependency_type": "required"},  # duplicate, should dedupe
    ]}
    result = anvil.required_dependency_project_ids("modrinth", "self-project-id", version)
    assert result == ["abc"]


# ---------------------------------------------------------------------------
# scan_untracked_files — pre-existing mods dropped into mods/ by hand
# ---------------------------------------------------------------------------

def test_scan_untracked_files_finds_jar_not_in_tracked_list(tmp_path, monkeypatch):
    (tmp_path / "mods").mkdir()
    (tmp_path / "mods" / "some-mod.jar").write_bytes(b"fake jar bytes")
    monkeypatch.setattr(anvil, "_modrinth_identify_by_hash", lambda h: None)
    monkeypatch.setattr(anvil, "save_server_data", lambda path, data: None)
    data = {"mods": [], "world_name": "world"}
    result = anvil.scan_untracked_files(str(tmp_path), data, "mod")
    assert len(result) == 1
    assert result[0]["file_name"] == "some-mod.jar"
    assert result[0]["identified"] is None


def test_scan_untracked_files_skips_already_tracked(tmp_path, monkeypatch):
    (tmp_path / "mods").mkdir()
    (tmp_path / "mods" / "tracked-mod.jar").write_bytes(b"fake jar bytes")
    monkeypatch.setattr(anvil, "_modrinth_identify_by_hash", lambda h: None)
    monkeypatch.setattr(anvil, "save_server_data", lambda path, data: None)
    data = {"mods": [{"file_name": "tracked-mod.jar"}], "world_name": "world"}
    result = anvil.scan_untracked_files(str(tmp_path), data, "mod")
    assert result == []


def test_scan_untracked_files_surfaces_modrinth_identification(tmp_path, monkeypatch):
    (tmp_path / "mods").mkdir()
    (tmp_path / "mods" / "mystery.jar").write_bytes(b"fake jar bytes")
    fake_match = {"source": "modrinth", "project_id": "abc123", "title": "Some Mod",
                  "version_id": "v1", "version_number": "1.2.3", "file_name": "mystery.jar"}
    monkeypatch.setattr(anvil, "_modrinth_identify_by_hash", lambda h: fake_match)
    monkeypatch.setattr(anvil, "save_server_data", lambda path, data: None)
    data = {"mods": [], "world_name": "world"}
    result = anvil.scan_untracked_files(str(tmp_path), data, "mod")
    assert len(result) == 1
    assert result[0]["identified"] == fake_match


def test_scan_untracked_files_no_folder_returns_empty(tmp_path):
    data = {"mods": [], "world_name": "world"}
    result = anvil.scan_untracked_files(str(tmp_path), data, "mod")
    assert result == []


def test_scan_untracked_files_skips_datapacks_content_type(tmp_path):
    data = {"world_name": "world"}
    result = anvil.scan_untracked_files(str(tmp_path), data, "datapack")
    assert result == []


def test_scan_untracked_files_uses_cache_on_second_call(tmp_path, monkeypatch):
    # Regression test for a real bug: without caching, every Installed-tab
    # refresh re-hashed and re-queried Modrinth for every untracked file,
    # even if nothing had changed — this fires constantly (every install,
    # update, revert, tab switch). The second scan of an unchanged file
    # should reuse the cached result instead of calling the identify
    # function again.
    (tmp_path / "mods").mkdir()
    (tmp_path / "mods" / "cached-mod.jar").write_bytes(b"fake jar bytes")
    monkeypatch.setattr(anvil, "save_server_data", lambda path, data: None)
    call_count = {"n": 0}

    def _fake_identify(h):
        call_count["n"] += 1
        return None
    monkeypatch.setattr(anvil, "_modrinth_identify_by_hash", _fake_identify)

    data = {"mods": [], "world_name": "world"}
    anvil.scan_untracked_files(str(tmp_path), data, "mod")
    anvil.scan_untracked_files(str(tmp_path), data, "mod")  # data carries the cache forward, same as a real request
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# External Crafty API persistence — survives an uninstall/reinstall (the
# Danger Zone uninstall clears data/, and is also the SAME file Server
# Manager uses, so this doubles as a test that both apps agree on it)
# ---------------------------------------------------------------------------

def test_external_crafty_api_persists_across_calls(tmp_path, monkeypatch):
    ext_path = tmp_path / "crafty_api.json"
    monkeypatch.setattr(anvil, "EXTERNAL_CRAFTY_API_PATH", ext_path)
    anvil._save_external_crafty_api("https://192.168.1.50:8443", "sometoken")
    url, token = anvil._load_external_crafty_api()
    assert url == "https://192.168.1.50:8443"
    assert token == "sometoken"


def test_load_settings_recovers_crafty_api_after_simulated_uninstall(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    ext_path = tmp_path / "crafty_api.json"
    monkeypatch.setattr(anvil, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(anvil, "EXTERNAL_CRAFTY_API_PATH", ext_path)

    s = anvil.load_settings()
    s["crafty_url"] = "https://192.168.1.50:8443"
    s["crafty_token"] = "sometoken"
    anvil.save_settings(s)

    settings_file.unlink()  # simulate the uninstall wiping /opt/.../data/settings.json

    recovered = anvil.load_settings()
    assert recovered["crafty_url"] == "https://192.168.1.50:8443"
    assert recovered["crafty_token"] == "sometoken"
