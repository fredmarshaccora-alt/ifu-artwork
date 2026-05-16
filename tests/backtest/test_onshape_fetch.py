"""G.2 unit tests: Onshape URL parsing + dynamic source registry.

We don't actually hit the Onshape API here -- the network path is
exercised manually.  These tests pin down the deterministic logic that
the import worker depends on.
"""
from __future__ import annotations
import pytest

from ifu.onshape_fetch import (parse_onshape_url, OnshapeURLError,
                                encode_configuration)
from ifu import sources_store


# ----- URL parser ----------------------------------------------------

class TestParseOnshapeURL:

    def test_workspace_url(self):
        url = ("https://cad.onshape.com/documents/"
               "835e6bd90b01779d102c6244/w/57594ac630641ef7dd431b7a/"
               "e/41130e2363641e1fb1763b3b")
        ids = parse_onshape_url(url)
        assert ids["did"] == "835e6bd90b01779d102c6244"
        assert ids["wv"] == "w"
        assert ids["wvid"] == "57594ac630641ef7dd431b7a"
        assert ids["eid"] == "41130e2363641e1fb1763b3b"

    def test_version_url(self):
        url = ("https://cad.onshape.com/documents/abc1234567890123/"
               "v/def4567890123456/e/9876543210fedcba")
        ids = parse_onshape_url(url)
        assert ids["wv"] == "v"
        assert ids["wvid"] == "def4567890123456"

    def test_microversion_url(self):
        url = ("https://cad.onshape.com/documents/abc1234567890123/"
               "m/def4567890123456/e/9876543210fedcba")
        ids = parse_onshape_url(url)
        assert ids["wv"] == "m"

    def test_url_with_query_string(self):
        url = ("https://cad.onshape.com/documents/abc1234567890123/"
               "w/def4567890123456/e/9876543210fedcba"
               "?configuration=foo&bar=baz")
        ids = parse_onshape_url(url)
        assert ids["did"] == "abc1234567890123"
        assert ids["eid"] == "9876543210fedcba"

    def test_url_with_fragment(self):
        url = ("https://cad.onshape.com/documents/abc1234567890123/"
               "w/def4567890123456/e/9876543210fedcba#part-1")
        ids = parse_onshape_url(url)
        assert ids["did"] == "abc1234567890123"

    def test_empty_url_rejected(self):
        with pytest.raises(OnshapeURLError):
            parse_onshape_url("")

    def test_none_rejected(self):
        with pytest.raises(OnshapeURLError):
            parse_onshape_url(None)

    def test_non_onshape_host_rejected(self):
        with pytest.raises(OnshapeURLError):
            parse_onshape_url(
                "https://google.com/documents/abc/w/def/e/ghi")

    def test_garbage_path_rejected(self):
        with pytest.raises(OnshapeURLError):
            parse_onshape_url("https://cad.onshape.com/help")

    def test_missing_element_segment_returns_no_eid(self):
        """A doc URL without /e/<eid> parses, but eid is None.
        The import worker rejects this with a friendlier error."""
        url = ("https://cad.onshape.com/documents/abc1234567890123/"
               "w/def4567890123456")
        ids = parse_onshape_url(url)
        assert ids["eid"] is None


# ----- Dynamic source registry --------------------------------------

class TestSourcesRegistry:

    def _cleanup(self, *ids):
        for sid in ids:
            sources_store.unregister(sid)

    def test_static_sources_listed(self):
        """all_sources() should contain the three baked sources."""
        all_ = sources_store.all_sources()
        ids = [s["id"] for s in all_]
        assert "siderail" in ids
        assert "presto" in ids
        assert "contesa" in ids

    def test_register_then_find(self):
        sid = "_test_dyn_register"
        try:
            entry = sources_store.register(
                source_id=sid, label="Test source",
                step_path="C:/fake/path.step",
                onshape_ids={"did": "x", "wid": "y", "eid": "z"})
            assert entry["id"] == sid
            assert entry["origin"] == "dynamic"
            found = sources_store.find(sid)
            assert found is not None
            assert found["label"] == "Test source"
            assert found["onshape_ids"]["did"] == "x"
        finally:
            self._cleanup(sid)

    def test_register_idempotent(self):
        """Re-registering the same id overwrites, doesn't duplicate."""
        sid = "_test_dyn_upsert"
        try:
            sources_store.register(
                source_id=sid, label="first", step_path="C:/a.step")
            sources_store.register(
                source_id=sid, label="second", step_path="C:/b.step")
            matches = [s for s in sources_store.list_dynamic()
                       if s["id"] == sid]
            assert len(matches) == 1
            assert matches[0]["label"] == "second"
            assert matches[0]["step_path"] == "C:/b.step"
        finally:
            self._cleanup(sid)

    def test_unregister(self):
        sid = "_test_dyn_unreg"
        sources_store.register(source_id=sid, label="x",
                                step_path="C:/x.step")
        assert sources_store.find(sid) is not None
        assert sources_store.unregister(sid) is True
        assert sources_store.find(sid) is None
        # Second unregister returns False (idempotent)
        assert sources_store.unregister(sid) is False


# ----- Configuration encoding ---------------------------------------

class TestEncodeConfiguration:

    def test_empty(self):
        assert encode_configuration({}) == ""
        assert encode_configuration(None) == ""

    def test_single_value(self):
        assert encode_configuration({"size": "M"}) == "size=M"

    def test_multiple_values(self):
        # Order is preserved by dict insertion in py3.7+
        s = encode_configuration({"size": "M", "color": "red"})
        # Either order is acceptable as long as semicolon-joined
        parts = set(s.split(";"))
        assert parts == {"size=M", "color=red"}

    def test_skips_empty_and_none(self):
        s = encode_configuration({"a": "1", "b": "", "c": None, "d": "4"})
        parts = set(s.split(";"))
        assert "a=1" in parts
        assert "d=4" in parts
        assert "b=" not in parts
        assert "c=" not in parts
