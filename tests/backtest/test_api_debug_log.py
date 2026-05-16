"""G.7 API tests for /api/debug/log -- the rolling server log buffer
the editor's overlay polls.

The endpoint is a single-user diagnostic; we pin down the contract:

  * returns {events: [], latest_seq: int}
  * making a request causes new events to appear, including one with
    op='render.start' / 'render.done' / 'render.cache_hit' etc on
    /api/render calls
  * the ?since=<seq> filter trims to events with seq > the given value
"""
from __future__ import annotations


def test_debug_log_returns_envelope(server_url):
    import requests
    r = requests.get(f"{server_url}/api/debug/log", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert "events" in body
    assert "latest_seq" in body
    assert isinstance(body["events"], list)
    assert isinstance(body["latest_seq"], int)


def test_debug_log_captures_recent_request(server_url):
    """Hitting /api/sources should append a 'req' event AND an 'ok'
    event with method/path/status/ms to the buffer."""
    import requests
    # Establish baseline
    base = requests.get(f"{server_url}/api/debug/log").json()
    base_seq = base["latest_seq"]
    # Generate a request
    requests.get(f"{server_url}/api/sources")
    # Fetch deltas only
    r = requests.get(f"{server_url}/api/debug/log",
                      params={"since": base_seq})
    new_events = r.json().get("events") or []
    # Should see at least one event for our /api/sources call
    sources_events = [e for e in new_events
                       if "/api/sources" in (e.get("path") or "")]
    assert sources_events, \
        f"no /api/sources event captured; got: {new_events}"
    levels = {e.get("level") for e in sources_events}
    # Both the req-in and the response-out logged
    assert "req" in levels
    assert "ok" in levels


def test_debug_log_since_filter(server_url):
    import requests
    # Get a seq number
    snap = requests.get(f"{server_url}/api/debug/log").json()
    seq = snap["latest_seq"]
    # since=that_seq should return only events newer than seq
    r = requests.get(f"{server_url}/api/debug/log",
                      params={"since": seq})
    events = r.json().get("events") or []
    for e in events:
        assert e["seq"] > seq, \
            f"event seq {e['seq']} <= since={seq}; filter broken"
