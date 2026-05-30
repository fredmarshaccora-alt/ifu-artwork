"""Annotation-arrow geometry for the 2D vector (HLR) output.

The 3D editor lets the user place two kinds of annotation arrows on a figure:

  * **straight**  -- a shaft + arrowhead pointing along a 3D direction
                     ("insert/remove this way").
  * **rotation**  -- a curved arrow sweeping around a 3D axis
                     ("turn this / rotate to fold").

Both are authored in MODEL space in the three.js pane (same frame as the GLB
and as the explode offsets).  This module projects them into the exact same
``(u, v)`` millimetre plane the HLR line-art uses -- ``u = P . x_axis``,
``v = P . y_axis`` (rotation-only projection, see ``t5_hlr_vector`` /
``_project_solid_bboxes``) -- so an arrow lands precisely on the line drawing.

Arrows are pure annotation: they are drawn as a flat overlay on top of the
line-art and are NOT subject to hidden-line occlusion.  Output is a fragment of
SVG elements authored in (u, v) space, meant to be dropped inside the line-art's
``<g transform="scale(1,-1)">`` group, plus the (u, v) bounding box of the
arrows so the caller can union it into the viewBox (otherwise an arrow that
sticks out past the part silhouettes would be clipped).

No OCCT dependency -- this is plain vector maths so it is cheap to unit-test.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional


# --- Proportions (all in MODEL millimetres, matching line-weight units) -------
# An arrowhead's length as a fraction of the shaft length, clamped to a sane mm
# range so heads stay legible on both tiny brackets and long pulls.
_HEAD_LEN_FRAC = 0.18
_HEAD_LEN_MIN = 4.0
_HEAD_LEN_MAX = 22.0
# Half-width of the arrowhead base relative to its length.
_HEAD_HALFWIDTH_FRAC = 0.55
# Arc resolution for rotation arrows (segments per radian).
_ARC_SEG_PER_RAD = 16
_ARC_MIN_SEGS = 8

# Default appearance.  Accora teal reads as "annotation" against black line-art.
DEFAULT_ARROW_STYLE = {
    "stroke": "#00836a",
    "width": 1.4,      # mm
    "fill": "#00836a",  # arrowhead fill
}


# --- vector helpers (3-tuples in model space) --------------------------------

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _norm(a):
    m = math.sqrt(_dot(a, a))
    if m < 1e-12:
        return None
    return (a[0] / m, a[1] / m, a[2] / m)


def _perp_basis(n):
    """Return two unit vectors (u, v) spanning the plane perpendicular to n."""
    n = _norm(n) or (0.0, 0.0, 1.0)
    # Pick the world axis least parallel to n as a seed.
    seed = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = _norm(_cross(seed, n)) or (1.0, 0.0, 0.0)
    v = _norm(_cross(n, u)) or (0.0, 1.0, 0.0)
    return u, v


def rotate_point(p, axis, angle_deg):
    """Rodrigues rotation of point ``p`` about world axis through the origin.

    Mirrors ``t5_hlr_vector.rotate_shape`` (origin (0,0,0)) so arrows track the
    same up_axis view override that the server applies to the shape.
    """
    if not axis or not angle_deg:
        return p
    k = _norm(axis)
    if k is None:
        return p
    th = math.radians(angle_deg)
    c, s = math.cos(th), math.sin(th)
    # p*cos + (k x p)*sin + k*(k.p)*(1-cos)
    kxp = _cross(k, p)
    kp = _dot(k, p)
    return (
        p[0] * c + kxp[0] * s + k[0] * kp * (1 - c),
        p[1] * c + kxp[1] * s + k[1] * kp * (1 - c),
        p[2] * c + kxp[2] * s + k[2] * kp * (1 - c),
    )


# --- projection to the line-art (u, v) plane ---------------------------------

def project(p, x_axis, y_axis):
    """Project a model point to (u, v) -- the SAME frame as the HLR polylines."""
    return (
        p[0] * x_axis[0] + p[1] * x_axis[1] + p[2] * x_axis[2],
        p[0] * y_axis[0] + p[1] * y_axis[1] + p[2] * y_axis[2],
    )


def _head_triangle_2d(tip2, base_dir2, head_len, half_w):
    """Build a 2D arrowhead triangle from the projected tip and shaft direction.

    The head is sized in (u, v) millimetres so it scales with the drawing
    exactly like the line weights do.  ``base_dir2`` points from tip back along
    the shaft (unit, 2D).
    """
    bx = tip2[0] + base_dir2[0] * head_len
    by = tip2[1] + base_dir2[1] * head_len
    # perpendicular in 2D
    px, py = -base_dir2[1], base_dir2[0]
    return [
        tip2,
        (bx + px * half_w, by + py * half_w),
        (bx - px * half_w, by - py * half_w),
    ]


def _unit2(dx, dy):
    m = math.hypot(dx, dy)
    if m < 1e-9:
        return None
    return (dx / m, dy / m)


# --- public: render a list of arrows to an SVG fragment ----------------------

def render_arrows_svg(arrows: Iterable[dict],
                      x_axis, y_axis,
                      up_axis: Optional[dict] = None,
                      style: Optional[dict] = None,
                      precision: int = 2):
    """Return ``(svg_fragment, bbox)`` for a list of arrow dicts.

    ``svg_fragment`` is a string of SVG elements in (u, v) space (to be placed
    inside the line-art's flip group); ``bbox`` is ``(u0, v0, u1, v1)`` of every
    drawn point, or ``None`` when nothing was drawn.

    Each arrow dict:
      straight: {"type":"straight", "anchor":[x,y,z], "dir":[x,y,z], "length":L,
                 "double"?:bool}
      rotation: {"type":"rotation", "center":[x,y,z], "axis":[x,y,z],
                 "radius":R, "sweep":deg, "start"?:deg, "double"?:bool}
    Coordinates are model-space; lengths/radii are model millimetres.
    """
    st = {**DEFAULT_ARROW_STYLE, **(style or {})}
    ax = up_axis.get("axis") if up_axis else None
    ang = float(up_axis.get("angle") or 0) if up_axis else 0.0

    def _proj_model(p3):
        if ang:
            p3 = rotate_point(p3, ax, ang)
        return project(p3, x_axis, y_axis)

    fmt = f"%.{precision}f"
    frags = []
    us, vs = [], []

    def _track(pts2):
        for (u, v) in pts2:
            us.append(u)
            vs.append(v)

    def _polyline(pts2):
        return "M " + " L ".join(f"{fmt % u} {fmt % v}" for u, v in pts2)

    def _emit_shaft(d):
        frags.append(
            f'<path d="{d}" fill="none" stroke="{st["stroke"]}" '
            f'stroke-width="{st["width"]:.3f}" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )

    def _emit_head(tri):
        d = _polyline(tri) + " Z"
        frags.append(f'<path d="{d}" fill="{st["fill"]}" stroke="none"/>')
        _track(tri)

    for a in arrows or []:
        atype = (a.get("type") or "straight").lower()

        if atype == "straight":
            anchor = tuple(float(c) for c in a["anchor"])
            direction = _norm(tuple(float(c) for c in a["dir"]))
            length = float(a.get("length") or 0)
            if direction is None or length <= 0:
                continue
            tip3 = _add(anchor, _scale(direction, length))
            a2 = _proj_model(anchor)
            t2 = _proj_model(tip3)
            shaft2 = _unit2(t2[0] - a2[0], t2[1] - a2[1])
            head_len = max(_HEAD_LEN_MIN,
                           min(_HEAD_LEN_MAX, length * _HEAD_LEN_FRAC))
            half_w = head_len * _HEAD_HALFWIDTH_FRAC
            if shaft2 is None:
                # arrow points straight at/away from camera -> draw a ring marker
                continue
            back2 = (-shaft2[0], -shaft2[1])
            # shaft stops at the head base so the fill doesn't overshoot
            base2 = (t2[0] + back2[0] * head_len, t2[1] + back2[1] * head_len)
            _emit_shaft(_polyline([a2, base2]))
            _track([a2, base2])
            _emit_head(_head_triangle_2d(t2, back2, head_len, half_w))
            if a.get("double"):
                fwd2 = shaft2
                base_a = (a2[0] + fwd2[0] * head_len, a2[1] + fwd2[1] * head_len)
                _emit_head(_head_triangle_2d(a2, fwd2, head_len, half_w))
                _track([base_a])

        elif atype == "rotation":
            center = tuple(float(c) for c in a["center"])
            axis = _norm(tuple(float(c) for c in a["axis"]))
            radius = float(a.get("radius") or 0)
            sweep = float(a.get("sweep") or 270.0)
            start = float(a.get("start") or 0.0)
            if axis is None or radius <= 0 or abs(sweep) < 1e-3:
                continue
            ub, vb = _perp_basis(axis)
            sweep_rad = math.radians(sweep)
            nseg = max(_ARC_MIN_SEGS,
                       int(abs(sweep_rad) * _ARC_SEG_PER_RAD))
            arc2 = []
            for i in range(nseg + 1):
                th = math.radians(start) + sweep_rad * (i / nseg)
                ct, sct = math.cos(th), math.sin(th)
                p3 = _add(center,
                          _add(_scale(ub, radius * ct), _scale(vb, radius * sct)))
                arc2.append(_proj_model(p3))
            _emit_shaft(_polyline(arc2))
            _track(arc2)
            # arrowhead at the sweep end, tangent to the arc (2D)
            head_len = max(_HEAD_LEN_MIN,
                           min(_HEAD_LEN_MAX, radius * 0.45))
            half_w = head_len * _HEAD_HALFWIDTH_FRAC
            end2, prev2 = arc2[-1], arc2[-2]
            tan2 = _unit2(end2[0] - prev2[0], end2[1] - prev2[1])
            if tan2 is not None:
                back2 = (-tan2[0], -tan2[1])
                _emit_head(_head_triangle_2d(end2, back2, head_len, half_w))
            if a.get("double"):
                s2, n2 = arc2[0], arc2[1]
                tan0 = _unit2(s2[0] - n2[0], s2[1] - n2[1])
                if tan0 is not None:
                    back0 = (-tan0[0], -tan0[1])
                    _emit_head(_head_triangle_2d(s2, back0, head_len, half_w))

    if not frags:
        return "", None
    fragment = ('<g class="layer layer-arrows" pointer-events="none">'
                + "".join(frags) + '</g>')
    return fragment, (min(us), min(vs), max(us), max(vs))
