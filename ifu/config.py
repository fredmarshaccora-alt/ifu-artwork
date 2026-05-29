"""Source registry and view presets.

The ``SOURCES`` list is the single source of truth for which STEP files
the pipeline knows about.  Each entry is a tuple:

    (file_id, file_label, step_path, hlr_kwargs, pre_rotation, onshape_ids)

  * ``file_id``      : URL-safe identifier
  * ``file_label``   : human-readable name in the picker
  * ``step_path``    : on-disk path (loaded lazily by the server)
  * ``hlr_kwargs``   : passed verbatim to ``run_hlr_per_solid`` --
                       at minimum ``mesh_defl`` and ``sample_defl``
  * ``pre_rotation`` : ``((axis_x, axis_y, axis_z), angle_deg)`` or None.
                       Re-orients the model so its long axis is world X
                       and "up" is world Z -- the frame our STD_VIEWS
                       are built around.
  * ``onshape_ids``  : ``{"did", "wid", "eid"}`` for the assembly, or
                       None.  Enables the feature-tree sidebar.

``VIEWS`` is the list of standard camera directions for each source.
``SOURCE_VIEW_SUBSET`` lets a heavy source restrict which views get
baked (e.g. Contesa: iso only).
``SOURCE_SKIP_CATEGORIES`` omits HLR categories from the SVG to keep
size manageable.
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Iterable, Sequence

HERE = Path(__file__).resolve().parent.parent   # project root

# Data root for ALL persisted state (figures, views, projects, sources,
# imported STEPs, thumbnails, debug captures, baked SVGs).  Override with
# IFU_DATA_DIR so a deployed instance can point it at a persistent disk
# (e.g. Render's mounted volume at /data) while local dev keeps using
# ./out.  Everything downstream derives from OUT, so this one switch
# moves all server state onto durable storage.
OUT = Path(os.environ.get("IFU_DATA_DIR") or (HERE / "out"))
try:
    OUT.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


SOURCES: Sequence[tuple] = [
    ("siderail",  "Folding siderail",
     Path(r"C:\Users\FredMarshAccora\Downloads\P194-03-00 Folding siderail ASSE.STEP"),
     {"mesh_defl": 0.8, "sample_defl": 1.0},
     None,
     None),
    ("presto",    "Presto bed (top assembly)",
     HERE.parent / "step_lineart_test" / "presto_top_level.step",
     {"mesh_defl": 1.5, "sample_defl": 1.0},
     ((0, 1, 0), -90),
     {"did": "835e6bd90b01779d102c6244",
      "wid": "57594ac630641ef7dd431b7a",
      "eid": "41130e2363641e1fb1763b3b"}),
    ("contesa",   "Contesa V2 / FL8 (top assembly)",
     HERE / "contesa_top_level.step",
     # 61MB STEP - coarser tessellation to keep mesh memory reasonable
     {"mesh_defl": 3.0, "sample_defl": 1.5},
     # Native bbox: X=2153 (length), Y=1448 (height incl. headboard),
     # Z=1016 (width).  Contesa STEP is Y-up; rotate +90deg about X to
     # put height on world Z so the iso view comes out upright.
     ((1, 0, 0), 90),
     {"did": "b112cdaa5ec09a28f81ca7c7",
      "wid": "0c1fa64d6ea5b9f87d9bdb3e",
      "eid": "0a03a83f17a3c3550242614b"}),
]


VIEWS: Sequence[tuple[str, str, tuple[float, float, float]]] = [
    ("iso",   "Iso 3/4 (front-right-above)", (-0.5, -1.0,  0.7)),
    ("front", "Front elevation",              ( 0.0, -1.0,  0.25)),
    ("side",  "Side elevation",               (-1.0,  0.0,  0.25)),
]


SOURCE_VIEW_SUBSET: dict[str, Iterable[str]] = {
    "contesa": ["iso"],
}


SOURCE_SKIP_CATEGORIES: dict[str, tuple[str, ...]] = {
    "contesa": ("hidden_outline", "hidden_sharp"),
}
