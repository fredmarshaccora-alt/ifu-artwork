"""Optional Onshape API client loader.

The Onshape feature-tree sidebar is *optional*: the viewer builds and
serves perfectly fine without it -- any source whose ``onshape_ids`` is
None just gets the STEP tree as fallback.

We import the client lazily so that test environments / CI without
Onshape credentials don't see import failures.
"""
from __future__ import annotations
import sys
from pathlib import Path

# Locations where the Onshape client project might live; tried in order.
_CLIENT_PATHS = [
    Path(r"C:\Users\FredMarshAccora\Projects\onshape-analytics"),
]

OnshapeClient = None

for _p in _CLIENT_PATHS:
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
    _env = _p / ".env"
    if _env.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(_env)
        except Exception:
            pass

try:
    from onshape_analytics.client import OnshapeClient as _OnshapeClient
    OnshapeClient = _OnshapeClient
except Exception as _exc:
    print(f"  (Onshape client unavailable: {_exc}; feature trees will be empty)",
          flush=True)
