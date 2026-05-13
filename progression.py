"""Build a side-by-side comparison of the 3D rendering experiments.

Each row = one STEP file. Each column = one variant.
Pulls v5 (best of the line-art pipeline) from step_lineart_test/out for
reference so the contrast is visible at a glance.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageChops


HERE = Path(__file__).parent
LINEART_OUT = HERE.parent / "step_lineart_test" / "out"
OUT = HERE / "out"

# (row label, stem, lineart_pattern)
ROWS = [
    ("Siderail", "P194-03-00 Folding siderail ASSE", "{stem}__iso__v5.png"),
    ("Presto bed", "presto_top_level", "{stem}__iso__v10.png"),
]

COLS = [
    ("ref  best line-art (existing)",   "lineart",  None),
    ("t1b painted metal  (PBR+HDRI)",    "this",     "{stem}__iso__t1b_painted.png"),
    ("t2  SSAO clay",                    "this",     "{stem}__iso__t2_ssao.png"),
    ("t3  cel / toon",                   "this",     "{stem}__iso__t3_toon.png"),
    ("t4  shaded + outline (clean)",     "this",     "{stem}__iso__t4_clean.png"),
    ("t4  shaded + outline + crease",    "this",     "{stem}__iso__t4_crease.png"),
    ("t5  HLR vector  (Composer)",       "this",     "{stem}__iso__t5_smart.png"),
    ("t5  HLR vector + smooth",          "this",     "{stem}__iso__t5_detailed.png"),
]


def trim_white(img, pad=8):
    bg = Image.new("RGB", img.size, "white")
    diff = ImageChops.difference(img, bg)
    bbox = diff.getbbox()
    if bbox is None:
        return img
    x0, y0, x1, y1 = bbox
    return img.crop((max(0, x0 - pad), max(0, y0 - pad),
                     min(img.size[0], x1 + pad), min(img.size[1], y1 + pad)))


def main():
    cell_w, cell_h = 560, 340
    title_h = 32
    margin = 14
    header_h = 50
    row_label_w = 90
    cols = len(COLS)
    rows = len(ROWS)
    W = row_label_w + cols * cell_w + (cols + 1) * margin
    H = header_h + rows * (cell_h + title_h) + (rows + 1) * margin

    canvas = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(canvas)
    try:
        font_h = ImageFont.truetype("arialbd.ttf", 24)
        font_b = ImageFont.truetype("arialbd.ttf", 18)
        font_r = ImageFont.truetype("arialbd.ttf", 20)
        font_n = ImageFont.truetype("arial.ttf", 14)
    except Exception:
        font_h = ImageFont.load_default()
        font_b = font_r = font_n = font_h

    draw.text((margin, 10),
              "IFU image: 3D rendering approaches  -  same STEP, same iso camera, "
              "only the shading model changes",
              fill="black", font=font_h)

    for ri, (rlabel, stem, lineart_pattern) in enumerate(ROWS):
        ry = header_h + margin + ri * (cell_h + title_h + margin)
        draw.text((10, ry + cell_h // 2 - 10), rlabel,
                  fill="black", font=font_r)
        for ci, (clabel, src, pattern) in enumerate(COLS):
            x = row_label_w + margin + ci * (cell_w + margin)
            base = (LINEART_OUT if src == "lineart" else OUT)
            pat = pattern if pattern else lineart_pattern
            f = base / pat.format(stem=stem)
            draw.rectangle([x, ry, x + cell_w, ry + cell_h + title_h],
                           outline="lightgrey")
            draw.text((x + 8, ry + 6), clabel, fill="black", font=font_b)
            if not f.exists():
                draw.text((x + 16, ry + cell_h // 2),
                          "(missing)\n" + f.name, fill="grey", font=font_n)
                continue
            img = Image.open(f).convert("RGB")
            img = trim_white(img, pad=12)
            iw, ih = img.size
            s = min(cell_w / iw, cell_h / ih)
            nw, nh = int(iw * s), int(ih * s)
            img = img.resize((nw, nh), Image.LANCZOS)
            ox = x + (cell_w - nw) // 2
            oy = ry + title_h + (cell_h - nh) // 2
            canvas.paste(img, (ox, oy))

    dest = HERE / "progression.png"
    canvas.save(dest)
    print(f"wrote {dest}  {W}x{H}  {dest.stat().st_size/1024:.0f}KB")


if __name__ == "__main__":
    main()
