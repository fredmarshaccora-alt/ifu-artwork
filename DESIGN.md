# IFU Artwork — Tech-Illustrator UI Design

How the tool should be shaped to support a real technical-illustrator
workflow: many figures across many projects, controlled regeneration
from latest CAD, audit-ready revision tracking.

This is the **design target** — not what's built today.  We'll get there
in phases (see "Build sequence" at the end).

---

## The illustrator's actual workflow

1. Open a project (e.g. "Presto IFU R03")
2. Pick (or create) a figure within the project
3. Pose the 3D model to the right angle
4. Generate the 2D line drawing
5. Tweak: highlight parts, change line weights, add shading, drop callouts
6. Save the figure (auto + explicit)
7. Place into the IFU Word/InDesign doc later
8. Weeks pass → engineering changes the CAD
9. Illustrator decides per-figure: keep the old CAD snapshot or
   regenerate from the new one
10. If regenerating: same angle, same selections, same styling, but on
    the new geometry — with a side-by-side diff to confirm

The hard constraints:

- **Never auto-update from CAD**: regulatory paranoia.  An IFU figure
  must keep meaning exactly what it meant when it was signed off.
- **One-click manual update available**: when the illustrator IS ready,
  no manual replication of camera/styling/selections.
- **Audit trail**: every figure stamped with "rendered from Onshape
  microversion X at time Y by user Z".

---

## Screen layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│ [≡] IFU Artwork    Presto IFU R03    [save] [export ▾]   [user] [help]   │ ← top bar
├──┬───────────────────┬─────────────────────────────────────┬─────────────┤
│  │ Figures (12)      │                                     │ Properties  │
│  │ ─────────         │                                     │ ─────────   │
│ P│ [+] new figure    │                                     │             │
│ r│                   │            EDITOR CANVAS            │  (changes   │
│ o│ ┌─┐ Fig 1         │     ┌──────────┬──────────┐         │   depending │
│ j│ └─┘ Lower bed     │     │          │          │         │   on what's │
│ e│                   │     │   3D     │   2D     │         │   selected) │
│ c│ ┌─┐ Fig 2         │     │ viewer   │ drawing  │         │             │
│ t│ └─┘ Side rail     │     │          │          │         │             │
│ s│   ⬆ CAD update    │     │          │          │         │             │
│  │                   │     └──────────┴──────────┘         │             │
│  │ ┌─┐ Fig 3         │                                     │             │
│  │ └─┘ Mattress      │      [bottom toolbar: layers,       │             │
│  │                   │       layout, hi-detail, etc.]      │             │
│  │ Sources (3)       │                                     │             │
│  │ ─────────         │                                     │             │
│  │ • Folding siderail│                                     │             │
│  │   R02 ✓ latest    │                                     │             │
│  │ • Presto top      │                                     │             │
│  │   R04 ⬆ 2 behind  │                                     │             │
│  │ • Contesa         │                                     │             │
│  │   R01 ✓ latest    │                                     │             │
├──┴───────────────────┴─────────────────────────────────────┴─────────────┤
│ Source: Presto R04 (2 revisions behind latest) [view changes] [update ▾] │ ← status bar
└──────────────────────────────────────────────────────────────────────────┘
```

The collapsible **left rail** is a Notion-style mini-navigator:
projects (📁), figures within current project (🖼), sources (📐),
settings (⚙).  Hover-expand for labels.

---

## Six surfaces in detail

### 1. Project picker (the home screen)

- Tile grid of recent projects
- Each tile: thumbnail (from first figure), name, count of figures,
  date last edited, badge if any source has CAD updates available
- [+ New project] button
- Optional: pinned projects, archived projects

### 2. Source manager (left panel, "Sources" tab)

Per source row:
```
📐 Folding siderail
   Bound to: P194-03-00.STEP  (R02 — 2026-04-12)
   ⬆ 2 revisions behind latest
   [refresh from Onshape] [open in Onshape] [⋯]
```

Click row → side panel opens with revision history:
```
Revisions
─────────
R04 (latest)   2026-05-15  fred@accora  "Added pin bracket"  [use this]
R03            2026-05-01  alice@accora "Tweaked stop ring"   [use this]
R02 (current)  2026-04-12  fred@accora  "Initial production"  ✓
R01            2026-03-30  bob@accora   "Prototype"           [use this]
```

For non-Onshape sources (e.g. a STEP file dropped from desktop), the
"revision" is the file's content hash — we detect when it changes and
prompt the illustrator to ingest the new file as a new revision.

### 3. Figures list (left panel, "Figures" tab)

Two view modes — toggle in panel header.

**Thumbnail grid**:
```
┌────────┐  ┌────────┐  ┌────────┐
│ [img]  │  │ [img]  │  │ [img]  │
│ Lower  │  │ Side   │  │ Mattres│
│ bed    │  │ rail ⬆ │  │ s      │
└────────┘  └────────┘  └────────┘
```

**List**:
```
┌──┬───────────────────────────────────────────────┐
│  │ Lower bed - iso view                          │
│  │ Source: Presto R04 ✓                          │
├──┼───────────────────────────────────────────────┤
│  │ Side rail - close-up of pivot                 │
│  │ Source: Folding siderail R02 ⬆ 2 behind       │
├──┼───────────────────────────────────────────────┤
...
```

Each figure shows the source-revision binding with a status badge.

### 4. Editor canvas (the main work area)

Same Split layout we have today (2D | 3D) but with new chrome:

- **Floating toolbar (top of canvas)**: layout 2D/Split/3D, layer toggles,
  detail slider (Coarse / Normal / Fine), hi-detail-overlay button
- **Pan/zoom controls** built-in (mouse wheel + drag, plus on-screen
  "fit", "100%", "+", "-" for accessibility)
- **Camera gizmo (top-right of 3D)**: view-cube for one-click iso/front/
  top/etc. (like SolidWorks / Onshape)
- **Selection feedback** in 3D: outline shader instead of color swap
  (much clearer on busy assemblies)

### 5. Properties panel (right, contextual)

Tabs cycle based on context:

**Nothing selected** (Figure tab):
```
Figure: "Side rail - close-up"
Source:  Folding siderail R02  [change ▾]
Camera:  Custom  [reset to iso]
Detail:  ○ Coarse  ● Normal  ○ Fine
Layers:  ☑ Silhouette  ☑ Sharp  ☐ Smooth  ☐ Hidden
Notes:   [textarea — appears in figure caption]
```

**Parts selected** (Styling tab):
```
3 parts selected
─────────
Stroke   [color] [-----O----- 3.0mm]
Fill     [color] ☑ shade  [---O--- 30%]
Dash     [solid ▾]
[Apply]  [Reset]

Applied styles (4)
─────────
[■] [#5] tube_main_5
[■] [#12] bracket_left
[■] [#7] pivot_pin
[■] [#23] cap
```

**Annotation mode** (Callouts tab):
```
[+ callout] [+ leader] [+ dimension]

Callouts in this figure (3)
─────────
[1] "Locking pin"     [edit] [×]
[2] "Pivot bushing"   [edit] [×]
[3] "Pull to release" [edit] [×]
```

### 6. Bottom status bar (revision tracking — the key new piece)

```
Source: Folding siderail R02 ⬆ 2 revisions behind latest
  [view changes]   [update to R04 ▾]
```

`[view changes]` opens the diff modal (see below).
`[update to R04 ▾]` is split: clicking the main button runs the
update; the dropdown lets you pick a specific revision (R03 instead
of R04 if you want to step gradually).

---

## The "update from latest CAD" flow (the regulatory-critical bit)

Triggered by clicking [update to R04] in the status bar.

**Step 1: Server fetches the new revision**
- For Onshape: pull STEP at the new microversion, mesh, bake SVG
- For local file: re-read, hash, store as new revision
- Progress shown in a modal: "Importing Presto R04...  3 of 5 views"

**Step 2: Side-by-side diff preview**
```
┌──────────────────────────────┬──────────────────────────────┐
│       Current (R02)          │       Update (R04)            │
│                              │                              │
│  [rendered figure A]         │  [rendered figure B]         │
│                              │                              │
└──────────────────────────────┴──────────────────────────────┘
Differences
  • 0 parts removed
  • 2 parts added (highlighted yellow in R04)
  • 1 part moved >5mm
  • Your highlighted parts still exist: 3/3 ✓
  • Your callouts still anchor to valid 2D points: 3/3 ✓

[Cancel] [Replace figure with R04] [Save R04 as a NEW figure]
```

The **"save as a new figure"** option is important for regulatory:
keep the R02 version frozen for the released IFU and start a new
figure for the next version.

**Step 3: If "replace" chosen**
- Figure's bound_revision_id flips to R04
- Cached renders for R04 are kept
- Old R02 cached renders are kept too (cheap; we may need to roll
  back)
- Audit log entry: "Fred updated figure to R04 on 2026-05-15 14:32"

**Step 4: If part_idx mapping doesn't survive**
- e.g. R02 had part-007, R04 splits it into two solids → part-007
  ambiguous
- Modal: "Your selection for part-007 no longer maps cleanly.  Pick
  one of: [part_007_a] [part_007_b] [drop selection]"

---

## Data model (local, on-disk JSON)

```
~/Documents/AccoraArtwork/
  Presto-IFU-R03/                       ← project folder
    project.json                        ← metadata + figure list
    sources/
      folding-siderail/
        source.json                     ← Onshape ids, last-pulled microversion
        revisions/
          rev_2026-04-12T11-23-04/
            step.step                   ← cached STEP at that microversion
            svg_iso.svg                 ← baked HLR per standard view
            svg_front.svg
            ...
            glb.glb                     ← cached GLB for 3D pane
            metadata.json               ← microversion, fetched_at, file_hash
          rev_2026-05-01T09-12-44/
            ...
          rev_2026-05-15T16-04-21/      ← latest
            ...
    figures/
      fig_001.json                      ← all the figure state below
      fig_002.json
    exports/
      fig_001_v3.pdf
      figure-pack.pdf
```

`project.json`:
```json
{
  "id": "uuid",
  "name": "Presto IFU R03",
  "description": "...",
  "created_at": "...",
  "last_edited_at": "...",
  "figures": ["fig_001", "fig_002", ...]
}
```

`source.json`:
```json
{
  "id": "uuid",
  "name": "Folding siderail",
  "kind": "onshape",
  "onshape": {"did": "...", "wid": "...", "eid": "..."},
  "current_revision_id": "rev_2026-05-15T16-04-21",
  "revisions": [
    {
      "id": "rev_2026-04-12T11-23-04",
      "microversion": "abc123",
      "fetched_at": "2026-04-12T11:23:04Z",
      "file_hash": "sha256:...",
      "label": "R02"
    },
    ...
  ]
}
```

`fig_001.json`:
```json
{
  "id": "uuid",
  "name": "Side rail - close-up of pivot",
  "source_id": "uuid",
  "bound_revision_id": "rev_2026-04-12T11-23-04",
  "view": {
    "eye": [1200, -1500, 800],
    "target": [0, 0, 200],
    "up_axis": "z"
  },
  "selection": {
    "parts": [5, 12, 7, 23],
    "tree_anchors": ["leaf-Part-3", "leaf-Part-7", ...]
  },
  "styles_per_part": {
    "5":  {"stroke": "#00836a", "width": 3, "fillOn": true, "fillColor": "#cce6e0", "fillAlpha": 0.3},
    "12": {...}
  },
  "layers_on": {"silhouette": true, "sharp": true, "smooth": false, "hidden": false},
  "detail": "normal",
  "annotations": [
    {"id": "...", "kind": "callout", "anchor": [u, v], "label_at": [u, v], "text": "Locking pin"},
    ...
  ],
  "notes": "Figure 4 in IFU section 3.2",
  "audit": [
    {"who": "fred", "what": "created", "at": "2026-04-15T..."},
    {"who": "fred", "what": "updated to revision rev_2026-05-15...", "at": "..."}
  ]
}
```

**Key invariant**: a figure's render is fully reproducible from
`source.json[bound_revision_id]` + `fig.json`.  Lose the project
folder, you can't recover.  Add `git init` to project folder for free
versioning.

---

## Onshape revision tracking

Onshape gives us **microversionId** for every document state — an
immutable hash of the document at that moment, like a git SHA.  We
poll cheaply:

- `GET /api/documents/d/{did}/w/{wid}` returns the current
  microversionId for the workspace
- Compare to source.current_revision.microversion
- If different → badge "⬆ N revisions behind" on the source

No live updates.  Polling is on-demand:
- On project open (one quick call per source)
- Click [refresh] in source row to re-poll
- Optional: background poll every N minutes (off by default)

For NON-Onshape sources (loose STEP files):
- Hash the file on disk
- If hash differs from current_revision.file_hash → prompt user
- "The file P194-03-00.STEP has changed.  Ingest as new revision?"

---

## Build sequence (phased, each ships a usable milestone)

### Phase A — Local "figures" abstraction (no projects yet)
- Migrate "saved view + applied styles + selection" into a Figure
  JSON on disk
- Figures list in left panel (replaces saved-views box)
- Each figure has a bound source + revision (single global revision for now)
- Backtest: load figure, render matches what was saved

### Phase B — Projects + source folder structure
- Project picker / home screen
- Multi-source per project
- File structure as above
- Migration: existing localStorage → first project
- Backtest: create project, add source, create figure, reopen → state restored

### Phase C — Revision tracking + manual refresh
- Onshape microversion polling (manual button)
- Revision history per source
- Per-figure bound revision
- Status bar shows binding + behind-count
- Backtest: bump microversion, verify "behind" badge appears

### Phase D — The diff-and-update flow
- Side-by-side render comparison
- Selection-mapping with conflict resolution UI
- Audit log writes
- Backtest: figure A bound to R01, update to R02, verify state preserved

### Phase E — Polish + UX
- Camera gizmo (view-cube) in 3D
- Outline-shader selection in 3D
- Thumbnails in figure list
- PDF export of full figure pack
- Undo/redo
- Backtest: full Playwright flow from new project to PDF export

Each phase ships a usable tool.  Phase A is the smallest useful step
and unblocks every other.

---

## What's reusable from today's viewer

| Piece | Reuse |
|---|---|
| 2D + 3D editor canvas | Yes -- becomes the centre column of new layout |
| Per-part styling | Yes -- moves into figure JSON |
| Footprint / silhouette / hi-detail endpoints | Yes -- unchanged |
| Saved views | Replaced by figures (saved view IS the figure's view) |
| Applied-styles list | Replaced by figure's styles_per_part |
| Tree sidebar | Stays, moves under Figures panel |
| Onshape API client | Yes -- gains microversion polling |
| Backtest harness | Yes -- expanded for new flows |

The HLR + footprint pipeline is solid.  Everything we're adding is
*around* it: organisation, persistence, version awareness.

---

## Open questions for the user

1. **Onshape doc layout** — do you keep IFU-relevant STEPs in dedicated
   Onshape documents, or share with engineering's working workspaces?
   This affects whether revision tracking is per-document or
   per-version-snapshot.
2. **Sign-off step** — should "lock figure" be its own action (figure
   becomes read-only, must be cloned to edit)?  Useful for IFU
   approval audit, optional otherwise.
3. **Multi-user** — does anyone else at Accora author figures, or is
   it just you?  If just you, single-user local is fine forever; if
   multi-user, we'd want shared project storage (a shared network
   drive is the cheap option).
4. **Export targets** — beyond SVG/PDF, do you need DXF (for
   InDesign), or a specific Word-compatible format?

Answers to these shape what gets built in Phase B onward.
