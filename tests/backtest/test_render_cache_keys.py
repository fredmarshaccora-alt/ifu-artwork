"""Regression: tiny float drift in view_dir / focal must not bust the
render cache. Tests the precision contract in serve._view_keys.

OrbitControls quantises camera deltas by a few 1e-4 between identical
user gestures (mouse pixel rounding feeding into a normalised vector
+ a slightly different focal each frame). At 3-decimal precision we
were missing the cache on what felt to the user like the same angle.
"""
from __future__ import annotations


def test_view_keys_absorb_micro_drift():
    """Two view_dir vectors that differ by less than 0.005 must map
    to the same cache key."""
    from serve import _view_keys

    vd_a = (0.7071, 0.0, 0.7071)
    vd_b = (0.7071 + 0.0009, 0.0, 0.7071 - 0.0009)
    focal = (0.0, 0.0, 0.0)
    key_a = _view_keys(vd_a, focal)
    key_b = _view_keys(vd_b, focal)
    assert key_a == key_b, (
        f"micro-drift busted the cache: {key_a} != {key_b}"
    )


def test_view_keys_distinguish_user_visible_change():
    """A camera turn that the user can actually see (>= ~0.6deg)
    should produce a distinct cache key. ~0.01 along a unit axis is
    ~0.57deg of rotation, right at our tolerance boundary; bump well
    past it to ensure a cache miss."""
    from serve import _view_keys

    vd_a = (0.7071, 0.0, 0.7071)
    vd_b = (0.7171, 0.0, 0.6971)  # ~0.8deg rotation
    focal = (0.0, 0.0, 0.0)
    key_a = _view_keys(vd_a, focal)
    key_b = _view_keys(vd_b, focal)
    assert key_a != key_b, (
        "real camera change collapsed onto the same cache key"
    )


def test_view_keys_quantise_focal_micro_drift():
    """Focal point at 0.01-unit precision absorbs sub-millimetre
    OrbitControls drift but separates real pan."""
    from serve import _view_keys
    vd = (0.7071, 0.0, 0.7071)

    # < 0.05 drift -> same key
    key_a = _view_keys(vd, (0.0, 0.0, 0.0))
    key_b = _view_keys(vd, (0.04, 0.0, 0.0))
    assert key_a == key_b

    # >= 0.1 unit -> different key (the user noticeably panned)
    key_c = _view_keys(vd, (0.5, 0.0, 0.0))
    assert key_a != key_c
