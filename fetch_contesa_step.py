"""Export the Contesa V2 (FL8) top assembly from Onshape as STEP.

Onshape doc:   b112cdaa5ec09a28f81ca7c7  'Contesa V2 US'
default w:     0c1fa64d6ea5b9f87d9bdb3e
top assembly:  0a03a83f17a3c3550242614b  'P199-20-22'

Onshape translation flow:
  POST /assemblies/d/{d}/w/{w}/e/{e}/translations  -> translation id
  GET  /translations/{id}  (poll for DONE)
  GET  /documents/d/{d}/externaldata/{externalId}  -> downloads STEP bytes
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(r"C:\Users\FredMarshAccora\Projects\onshape-analytics")))
from onshape_analytics.client import OnshapeClient  # noqa: E402


DID = "b112cdaa5ec09a28f81ca7c7"
WID = "0c1fa64d6ea5b9f87d9bdb3e"
EID = "0a03a83f17a3c3550242614b"
OUT = Path(__file__).parent / "contesa_top_level.step"


def main():
    c = OnshapeClient()
    print(f"requesting STEP export of P199-20-22 ({EID[:8]}...)")
    body = {
        "formatName": "STEP",
        "storeInDocument": False,
        "stepVersionString": "AP242",
        "grouping": True,
    }
    resp = c.post(
        f"/assemblies/d/{DID}/w/{WID}/e/{EID}/translations",
        json=body,
    )
    tid = resp.get("id")
    if not tid:
        print("no translation id in response:", resp); return 1
    print(f"  translation id {tid}; polling for DONE...")

    t0 = time.time()
    state = None
    external_id = None
    while True:
        s = c.get(f"/translations/{tid}")
        state = s.get("requestState") or s.get("state")
        if state in ("DONE", "FAILED"):
            external_id = s.get("resultExternalDataIds", [None])[0]
            break
        if time.time() - t0 > 600:
            print("  timeout after 10min, state=", state); return 1
        print(f"  state={state}  elapsed={time.time()-t0:.0f}s")
        time.sleep(10)

    if state != "DONE":
        print("translation failed:", s); return 1
    print(f"  done in {time.time()-t0:.0f}s, external_id={external_id}")

    # Download the STEP bytes
    url = f"/documents/d/{DID}/externaldata/{external_id}"
    print(f"  downloading...")
    # Use the session directly (we need raw bytes, not JSON)
    full_url = c._url(url)
    r = c._session.get(full_url)
    r.raise_for_status()
    OUT.write_bytes(r.content)
    print(f"wrote {OUT}  {OUT.stat().st_size/1024/1024:.1f}MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
