"""
Unit tests for the shared-token fallback (Anvil Server Manager reuses the
Installer's token, with two further legacy fallbacks for installs from
before the AnvilMC monorepo migration) and session-secret persistence.

Every test monkeypatches all three path constants explicitly, even when a
given test isn't exercising all of them — leaving one unpatched would mean
falling through to whatever the real default path constant is, which could
silently pass or fail depending on what actually exists on the machine
running the tests. Not hypothetical: this file used to only patch two of
the three tiers, and a third tier was added later without updating it here.

Run with:  pip install pytest && pytest
"""
import app as anvil


def _patch_all_token_paths(monkeypatch, tmp_path, shared=None, legacy=None, oldest=None):
    monkeypatch.setattr(anvil, "TOKEN_PATH", str(shared or tmp_path / "shared_token_missing"))
    monkeypatch.setattr(anvil, "LEGACY_TOKEN_PATH", str(legacy or tmp_path / "legacy_token_missing"))
    monkeypatch.setattr(anvil, "OLDEST_LEGACY_TOKEN_PATH", str(oldest or tmp_path / "oldest_token_missing"))


def test_get_token_prefers_shared_monorepo_token(tmp_path, monkeypatch):
    shared, legacy, oldest = tmp_path / "shared", tmp_path / "legacy", tmp_path / "oldest"
    shared.write_text("shared-value")
    legacy.write_text("legacy-value")
    oldest.write_text("oldest-value")
    _patch_all_token_paths(monkeypatch, tmp_path, shared, legacy, oldest)
    assert anvil.get_token() == "shared-value"


def test_get_token_falls_back_to_legacy_when_shared_missing(tmp_path, monkeypatch):
    # This is the upgrade path: an install from before the monorepo (but
    # after the Installer+Server Manager token was already shared) still has
    # its own token file at this path, and shouldn't be locked out until
    # it's re-run through install.sh.
    legacy, oldest = tmp_path / "legacy", tmp_path / "oldest"
    legacy.write_text("legacy-value")
    oldest.write_text("oldest-value")
    _patch_all_token_paths(monkeypatch, tmp_path, legacy=legacy, oldest=oldest)
    assert anvil.get_token() == "legacy-value"


def test_get_token_falls_back_to_oldest_legacy_when_others_missing(tmp_path, monkeypatch):
    # The original, pre-token-sharing generation of installs — even older
    # than the "legacy" tier above. Still shouldn't be locked out.
    oldest = tmp_path / "oldest"
    oldest.write_text("oldest-value")
    _patch_all_token_paths(monkeypatch, tmp_path, oldest=oldest)
    assert anvil.get_token() == "oldest-value"


def test_get_token_none_when_none_of_the_three_exist(tmp_path, monkeypatch):
    _patch_all_token_paths(monkeypatch, tmp_path)
    assert anvil.get_token() is None


def test_get_or_create_secret_key_persists_across_calls(tmp_path):
    secret_path = tmp_path / "session_secret"
    first = anvil.get_or_create_secret_key(str(secret_path))
    second = anvil.get_or_create_secret_key(str(secret_path))
    assert first == second
    assert len(first) == 64
