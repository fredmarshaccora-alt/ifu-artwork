"""Unit tests for the F.1 settings store."""
from __future__ import annotations
import pytest

from ifu import settings_store


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_store, "SETTINGS_PATH",
                         tmp_path / "settings.json")
    yield


def test_load_returns_defaults_when_no_file():
    s = settings_store.load()
    assert s["default_detail"] == "normal"
    assert s["default_stroke_color"] == "#00836a"
    assert s["version"] == 1


def test_save_persists_and_merges():
    settings_store.save({"default_detail": "fine"})
    s = settings_store.load()
    assert s["default_detail"] == "fine"
    # Other defaults preserved
    assert s["default_stroke_color"] == "#00836a"


def test_partial_update_doesnt_wipe_other_keys():
    settings_store.save({"default_detail": "fine"})
    settings_store.save({"default_stroke_width_mm": 5.0})
    s = settings_store.load()
    assert s["default_detail"] == "fine"
    assert s["default_stroke_width_mm"] == 5.0


def test_unknown_keys_round_trip():
    """Future features can add their own settings without a migration."""
    settings_store.save({"my_future_pref": [1, 2, 3]})
    s = settings_store.load()
    assert s["my_future_pref"] == [1, 2, 3]


def test_reset_wipes_to_defaults():
    settings_store.save({"default_detail": "fine"})
    s = settings_store.reset()
    assert s["default_detail"] == "normal"


def test_load_handles_corrupt_file():
    """A garbled settings.json must not crash the app."""
    settings_store.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings_store.SETTINGS_PATH.write_text("{not valid json")
    s = settings_store.load()
    # Falls back to defaults
    assert s["default_detail"] == "normal"


def test_get_convenience():
    settings_store.save({"default_detail": "coarse"})
    assert settings_store.get("default_detail") == "coarse"
    assert settings_store.get("missing", "fallback") == "fallback"


def test_save_does_not_downgrade_version():
    """Schema version is preserved.  If a hostile patch tried to set
    version=0, we keep the higher of the two."""
    settings_store.save({"version": 2})
    settings_store.save({"version": 0, "default_detail": "fine"})
    assert settings_store.load()["version"] == 2
