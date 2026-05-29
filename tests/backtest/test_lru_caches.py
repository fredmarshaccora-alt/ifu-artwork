"""Regression: _RENDER_CACHE / _SIL_CACHE / _FOOT_CACHE are LRU.

Old behaviour was FIFO (`pop(next(iter(cache)))`), so a frequently-used
key inserted early would be evicted before a never-touched key inserted
later. Switch project to project to confirm an entry the user keeps
hitting stays in the cache.
"""
from __future__ import annotations
from collections import OrderedDict


def test_cache_put_evicts_least_recently_used():
    from serve import _cache_put, _cache_get

    cache: "OrderedDict[str, int]" = OrderedDict()
    MAX = 3

    _cache_put(cache, 'a', 1, MAX)
    _cache_put(cache, 'b', 2, MAX)
    _cache_put(cache, 'c', 3, MAX)

    # Touch 'a' so it's now the most-recently-used
    assert _cache_get(cache, 'a') == 1

    # Adding 'd' should evict 'b' (the actual LRU), NOT 'a'
    _cache_put(cache, 'd', 4, MAX)

    assert 'a' in cache, "frequently-used entry was wrongly evicted"
    assert 'b' not in cache, "LRU entry should have been evicted"
    assert 'c' in cache
    assert 'd' in cache


def test_cache_get_returns_none_on_miss():
    from serve import _cache_get
    cache: "OrderedDict[str, int]" = OrderedDict()
    assert _cache_get(cache, 'nope') is None


def test_cache_put_replaces_existing_key():
    from serve import _cache_put, _cache_get
    cache: "OrderedDict[str, int]" = OrderedDict()
    _cache_put(cache, 'a', 1, 5)
    _cache_put(cache, 'a', 99, 5)
    assert _cache_get(cache, 'a') == 99
    assert len(cache) == 1
