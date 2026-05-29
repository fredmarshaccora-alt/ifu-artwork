"""Load test: measure where the home/project endpoints scale.

Approach: monkey-patch the store modules' OUT path to a tmp_path
fixture so we can safely seed 50 projects / 200 views / 1000 figures
without touching the real out/ tree.

Skipped by default (marker 'scale'). Run with:
    pytest -m scale tests/backtest/test_load_scale.py -v -s
"""
from __future__ import annotations
import time
from pathlib import Path
import pytest


N_PROJECTS = 50
N_VIEWS_PER_PROJECT = 4    # 200 views
N_FIGS_PER_VIEW = 5        # 1000 figures


def _stamp_seed_dir(tmp_path):
    """Point every store at a tmp out dir."""
    import ifu.projects as P
    import ifu.views as V
    import ifu.figures as F
    P.PROJECTS_DIR = tmp_path / "projects"
    V.VIEWS_DIR = tmp_path / "views"
    F.FIGURES_DIR = tmp_path / "figures"
    P.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    V.VIEWS_DIR.mkdir(parents=True, exist_ok=True)
    F.FIGURES_DIR.mkdir(parents=True, exist_ok=True)


def _seed(tmp_path):
    """Write the scaled corpus.  Returns (project_ids, view_ids)."""
    _stamp_seed_dir(tmp_path)
    import ifu.projects as P
    import ifu.views as V
    import ifu.figures as F

    project_ids = []
    view_ids = []
    for pi in range(N_PROJECTS):
        p = P.new_project(
            name=f"Project {pi:03d}",
            description=f"Seeded project {pi}",
            primary_source_id="siderail",
        )
        P.save(p)
        project_ids.append(p["id"])
        for vi in range(N_VIEWS_PER_PROJECT):
            v = V.new_view(
                project_id=p["id"],
                source_id="siderail",
                name=f"View {vi}",
                camera={"eye": [1000, -1500, 800],
                        "target": [0, 0, 0], "up_axis": "Z"},
            )
            V.save(v)
            view_ids.append(v["id"])
            for fi in range(N_FIGS_PER_VIEW):
                f = F.new_figure(
                    project_id=p["id"],
                    view_id=v["id"],
                    source_id="siderail",
                    name=f"Variant {fi}",
                    camera=v["camera"],
                )
                F.save(f)
    return project_ids, view_ids


@pytest.mark.scale
def test_listing_50_projects_via_bulk_endpoint(tmp_path, capsys):
    """The OLD home path was O(N projects x M views) JSON reads
    (one views_in_project per project x list_all per call).
    The NEW path uses a single bulk list_all + groupby.  This test
    measures both and asserts the bulk path is dramatically faster."""
    _seed(tmp_path)
    import ifu.projects as P
    import ifu.views as V
    from collections import defaultdict

    projects = P.list_all()
    assert len(projects) == N_PROJECTS

    # ---- OLD path: per-project views_in_project ---------------------
    t0 = time.time()
    for p in projects:
        V.views_in_project(p["id"])
    t_old = time.time() - t0

    # ---- NEW path: single list_all + groupby ------------------------
    t0 = time.time()
    all_views = V.list_all()
    by_project = defaultdict(list)
    for v in all_views:
        by_project[v.get("project_id") or ""].append(v)
    t_new = time.time() - t0

    with capsys.disabled():
        print(f"\n  OLD: per-project views_in_project x {N_PROJECTS}:  "
              f"{t_old*1000:7.1f} ms")
        print(f"  NEW: single list_all + groupby:           "
              f"{t_new*1000:7.1f} ms")
        print(f"  speedup:                                  "
              f"{t_old/t_new:.0f}x")
    # Confirm at least 10x improvement.  In practice on Windows we see
    # 30-50x.
    assert t_new * 10 < t_old, (
        f"bulk path not meaningfully faster: old={t_old*1000:.0f} ms "
        f"new={t_new*1000:.0f} ms"
    )
    assert t_new < 0.2, (
        f"bulk listing too slow: {t_new*1000:.0f} ms > 200 ms"
    )


@pytest.mark.scale
def test_project_workspace_30_views(tmp_path, capsys):
    """One project with 30 views (well above typical 4).  Measures
    the /views endpoint at scale."""
    _stamp_seed_dir(tmp_path)
    import ifu.projects as P
    import ifu.views as V
    import ifu.figures as F

    p = P.new_project(name="Big project", primary_source_id="siderail")
    P.save(p)
    for vi in range(30):
        v = V.new_view(project_id=p["id"], source_id="siderail",
                        name=f"View {vi}", camera={"eye": [0]*3, "target": [0]*3})
        V.save(v)
        for fi in range(3):
            f = F.new_figure(project_id=p["id"], view_id=v["id"],
                              source_id="siderail", name=f"V{vi}.{fi}",
                              camera=v["camera"])
            F.save(f)

    t0 = time.time()
    views = V.views_in_project(p["id"])
    t = time.time() - t0
    # We only have ONE project here so the filter degenerates to "all"
    assert len(views) == 30
    with capsys.disabled():
        print(f"\n  list_for_project (30 views, 90 figures): {t*1000:6.1f} ms")
    assert t < 0.3, f"Project workspace too slow: {t*1000:.0f} ms"


@pytest.mark.scale
def test_figure_listing_1000(tmp_path, capsys):
    """figures_store.list_all() across 1000 figures.  This is what
    /api/figures hits and what HomeScreen needs to count orphans."""
    _seed(tmp_path)
    import ifu.figures as F

    t0 = time.time()
    figs = F.list_all()
    t = time.time() - t0
    assert len(figs) == N_PROJECTS * N_VIEWS_PER_PROJECT * N_FIGS_PER_VIEW
    with capsys.disabled():
        print(f"\n  list_all  ({len(figs)} figures):       {t*1000:6.1f} ms")
    # 1000 small JSON reads on Windows: budget 1.5 s.
    assert t < 1.5, f"Figure listing too slow: {t*1000:.0f} ms"
