"""Phase 0 migration: extract the embedded CSS + JS out of
build_viewer.py's HTML_TEMPLATE into real static files.

Strategy (lossless + self-verifying):
  1. Import build_viewer.HTML_TEMPLATE (the un-formatted template, still
     brace-doubled for str.format).
  2. Partition it on unique multi-line markers into:
       pre_head | css | body_html | classic_js | module_js | tail
  3. ROUND-TRIP CHECK: reassemble the pieces with the same separators and
     assert it equals the original template byte-for-byte.  If not, abort
     WITHOUT modifying anything.
  4. Un-double braces ({{->{ , }}->}) in the css/classic/module pieces
     (they leave str.format, so they must carry single braces) and write:
       static/css/viewer.css
       static/js/viewer.classic.js
       static/js/viewer.module.js
  5. Build a NEW short HTML_TEMPLATE that <link>s the css and <script
     src>s the js (keeping {svg_blocks} + an inline {js_catalogue}), and
     rewrite build_viewer.py's HTML_TEMPLATE assignment in place.

Run:  python migrate_phase0_extract.py
A backup is written to build_viewer.py.phase0bak first.
"""
from __future__ import annotations
import sys
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass
from pathlib import Path

import build_viewer

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "build_viewer.py"
CSS_OUT = ROOT / "static" / "css" / "viewer.css"
CLASSIC_OUT = ROOT / "static" / "js" / "viewer.classic.js"
MODULE_OUT = ROOT / "static" / "js" / "viewer.module.js"

# --- unique multi-line separators (must each occur exactly where we think)
SEP_STYLE_OPEN  = "<style>\n"
SEP_STYLE_CLOSE = "\n</style>\n"
SEP_CLASSIC_OPEN = "<script>\n{js_catalogue}\n"
SEP_CLASSIC_CLOSE = '\n</script>\n\n<script type="module">\n'
SEP_MODULE_CLOSE = "\n</script>\n</body>"


def undouble(s: str) -> str:
    return s.replace("{{", "{").replace("}}", "}")


def redouble(s: str) -> str:
    return s.replace("{", "{{").replace("}", "}}")


def main() -> int:
    T = build_viewer.HTML_TEMPLATE

    # ---- partition ----------------------------------------------------
    if SEP_STYLE_OPEN not in T or SEP_CLASSIC_OPEN not in T:
        print("FAIL: expected markers not found; template shape changed.")
        return 1
    pre_head, _, rest = T.partition(SEP_STYLE_OPEN)
    css_body, _, after_style = rest.partition(SEP_STYLE_CLOSE)
    body_html, _, after_copen = after_style.partition(SEP_CLASSIC_OPEN)
    classic_body, _, after_cclose = after_copen.partition(SEP_CLASSIC_CLOSE)
    module_body, _, tail = after_cclose.partition(SEP_MODULE_CLOSE)

    if not (css_body and classic_body and module_body):
        print("FAIL: one or more extracted pieces are empty.")
        return 1

    # ---- round-trip verify (lossless?) --------------------------------
    reconstructed = (
        pre_head + SEP_STYLE_OPEN + css_body + SEP_STYLE_CLOSE
        + body_html + SEP_CLASSIC_OPEN + classic_body
        + SEP_CLASSIC_CLOSE + module_body + SEP_MODULE_CLOSE + tail
    )
    if reconstructed != T:
        print("FAIL: round-trip reconstruction != original template. "
              "Aborting; nothing modified.")
        # Help debug where it diverged
        for i, (a, b) in enumerate(zip(reconstructed, T)):
            if a != b:
                print(f"  first diff at char {i}: "
                      f"{reconstructed[i-40:i+10]!r} vs {T[i-40:i+10]!r}")
                break
        print(f"  len(reconstructed)={len(reconstructed)} len(T)={len(T)}")
        return 1
    print(f"[OK] round-trip lossless: template {len(T)} chars splits cleanly")
    print(f"     css={len(css_body)}  classic={len(classic_body)}  "
          f"module={len(module_body)} chars")

    # ---- sanity: no stray .format placeholders in extracted pieces ----
    # The only legitimate single-brace placeholders are {svg_blocks} and
    # {js_catalogue}, both of which live in pre_head/body_html, NOT in the
    # extracted css/js.  After undoubling, the extracted files must NOT
    # contain those tokens.
    for name, piece in (("css", css_body), ("classic", classic_body),
                        ("module", module_body)):
        u = undouble(piece)
        if "{svg_blocks}" in u or "{js_catalogue}" in u:
            print(f"FAIL: {name} contains a .format placeholder; "
                  f"extraction boundary is wrong.")
            return 1
        # Re-doubling must be an exact inverse (no odd brace counts).
        if redouble(u) != piece:
            print(f"FAIL: {name} brace doubling is not invertible "
                  f"(odd/unbalanced braces?). Aborting.")
            return 1
    print("[OK] brace doubling is invertible for all three pieces")

    # ---- write static files -------------------------------------------
    for p in (CSS_OUT, CLASSIC_OUT, MODULE_OUT):
        p.parent.mkdir(parents=True, exist_ok=True)
    CSS_OUT.write_text(undouble(css_body), encoding="utf-8")
    CLASSIC_OUT.write_text(undouble(classic_body) + "\n", encoding="utf-8")
    MODULE_OUT.write_text(undouble(module_body) + "\n", encoding="utf-8")
    print(f"[OK] wrote {CSS_OUT.relative_to(ROOT)} "
          f"({CSS_OUT.stat().st_size//1024}KB)")
    print(f"[OK] wrote {CLASSIC_OUT.relative_to(ROOT)} "
          f"({CLASSIC_OUT.stat().st_size//1024}KB)")
    print(f"[OK] wrote {MODULE_OUT.relative_to(ROOT)} "
          f"({MODULE_OUT.stat().st_size//1024}KB)")

    # ---- build the new (short) HTML_TEMPLATE ---------------------------
    # pre_head ends just before "<style>"; body_html begins with
    # "</head>\n<body>...".  Keep importmap + {svg_blocks} + an inline
    # {js_catalogue} script; reference the extracted files.
    new_template = (
        pre_head
        + '<link rel="stylesheet" href="/static/css/viewer.css"/>\n'
        + body_html
        + "<script>\n{js_catalogue}\n</script>\n"
        + '<script src="/static/js/viewer.classic.js"></script>\n'
        + '<script type="module" src="/static/js/viewer.module.js"></script>'
        + SEP_MODULE_CLOSE + tail
    )

    # ---- rewrite build_viewer.py's HTML_TEMPLATE assignment -----------
    src = SRC.read_text(encoding="utf-8")
    start_marker = 'HTML_TEMPLATE = r"""'
    si = src.find(start_marker)
    if si < 0:
        print("FAIL: could not find HTML_TEMPLATE assignment in source.")
        return 1
    body_start = si + len(start_marker)
    ei = src.find('"""', body_start)
    if ei < 0:
        print("FAIL: could not find closing triple-quote.")
        return 1
    old_body = src[body_start:ei]
    if old_body != T:
        print("FAIL: in-source template body != imported HTML_TEMPLATE "
              "(unexpected). Aborting to be safe.")
        print(f"  in-source len={len(old_body)} imported len={len(T)}")
        return 1

    backup = SRC.with_suffix(".py.phase0bak")
    backup.write_text(src, encoding="utf-8")
    print(f"[OK] backed up source -> {backup.name}")

    new_src = src[:body_start] + new_template + src[ei:]
    SRC.write_text(new_src, encoding="utf-8")
    saved = (len(src) - len(new_src)) / 1024
    print(f"[OK] rewrote build_viewer.py HTML_TEMPLATE "
          f"({len(src)//1024}KB -> {len(new_src)//1024}KB, "
          f"-{saved:.0f}KB)")
    print("\nNext: add Flask static serving if needed, rebuild, run smokes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
