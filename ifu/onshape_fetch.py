"""Onshape document import (Phase G.2).

Turn a pasted Onshape document URL into an on-disk STEP file plus a
registered dynamic source.  Two halves:

  1) URL parsing -- pull did + (wid OR vid OR mid) + eid out of the
     fragment that the Onshape "Share" button gives you.
  2) Translation flow -- POST /api/v10/partstudios/.../translations OR
     /api/v10/assemblies/.../translations, poll until DONE, then GET
     the resulting blob and write it as a .step file.

We use the OnshapeClient (already wired up in onshape_client.py) for
JSON requests but issue the binary download via a raw requests call
because the client always unconditionally calls resp.json().

This module is *server-side only*; the routes/UI live in serve.py and
build_viewer.py.  Heavy work (translation polling + STEP download)
runs on a daemon thread so the Flask request that kicked it off
returns immediately with a job_id the UI can poll.
"""
from __future__ import annotations
import re
import time
import uuid
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .config import OUT
from .onshape_client import OnshapeClient

IMPORTS_DIR = OUT / "imports"


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

# Examples we care about:
#   https://cad.onshape.com/documents/{DID}/w/{WID}/e/{EID}
#   https://cad.onshape.com/documents/{DID}/v/{VID}/e/{EID}
#   https://cad.onshape.com/documents/{DID}/m/{MID}/e/{EID}
# Trailing query/hash is tolerated.
_URL_RE = re.compile(
    r"/documents/(?P<did>[0-9a-f]{16,32})"
    r"/(?P<wv>[wvm])/(?P<wvid>[0-9a-f]{16,32})"
    r"(?:/e/(?P<eid>[0-9a-f]{16,32}))?",
    re.IGNORECASE,
)


class OnshapeURLError(ValueError):
    """Raised when a string isn't a recognisable Onshape doc URL."""


def parse_onshape_url(url: str) -> dict:
    """Pick the document/workspace/version + element ids out of an
    Onshape URL.

    Returns: ``{did, wv, wvid, eid}`` where ``wv`` is ``"w"`` (workspace
    HEAD), ``"v"`` (a specific Version), or ``"m"`` (a microversion).

    Raises ``OnshapeURLError`` if the URL isn't a valid Onshape link.
    """
    if not url or not isinstance(url, str):
        raise OnshapeURLError("URL is empty")
    try:
        parsed = urlparse(url.strip())
    except Exception as e:
        raise OnshapeURLError(f"could not parse URL: {e}")
    if parsed.netloc and "onshape.com" not in parsed.netloc.lower():
        raise OnshapeURLError(
            f"not an Onshape URL (host: {parsed.netloc!r})")
    m = _URL_RE.search(parsed.path)
    if not m:
        raise OnshapeURLError(
            "URL is missing /documents/<did>/{w|v|m}/<id>/e/<eid> segment")
    return {
        "did": m.group("did"),
        "wv": m.group("wv").lower(),
        "wvid": m.group("wvid"),
        "eid": m.group("eid"),
    }


# ---------------------------------------------------------------------------
# Onshape API helpers (translation flow)
# ---------------------------------------------------------------------------

def _client() -> OnshapeClient:
    if OnshapeClient is None:
        raise RuntimeError(
            "Onshape client unavailable -- check ONSHAPE_ACCESS_KEY / "
            "ONSHAPE_SECRET_KEY in the onshape-analytics .env")
    return OnshapeClient()


def get_document_info(did: str) -> dict:
    """Fetch top-level document metadata: name, owner, default workspace.

    Returns the raw Onshape response trimmed to the keys we use:
      ``{id, name, defaultWorkspace: {id, name}}``.
    """
    c = _client()
    resp = c.get(f"/documents/{did}") or {}
    return {
        "id": resp.get("id") or did,
        "name": resp.get("name") or "(unnamed document)",
        "defaultWorkspace": resp.get("defaultWorkspace") or {},
        "owner": (resp.get("owner") or {}).get("name"),
    }


def get_element_info(did: str, wv: str, wvid: str, eid: str) -> dict:
    """Fetch metadata for a specific element (assembly or part studio).

    Onshape's /documents/d/{did}/{wv}/{wvid}/elements lists all elements;
    we filter to the eid.  Returns ``{id, name, type}`` where ``type`` is
    one of ``"ASSEMBLY"``, ``"PARTSTUDIO"``, etc.
    """
    c = _client()
    resp = c.get(f"/documents/d/{did}/{wv}/{wvid}/elements",
                  params={"elementId": eid})
    if not resp:
        raise RuntimeError(f"element {eid!r} not found in document {did}")
    el = resp[0] if isinstance(resp, list) else resp
    return {
        "id": el.get("id") or eid,
        "name": el.get("name") or "(unnamed)",
        "type": (el.get("elementType") or "").upper(),
    }


def get_element_configuration(did: str, wv: str, wvid: str,
                                eid: str) -> dict:
    """Fetch the configuration parameter definitions for an element.

    Onshape's /elements/d/{did}/{wv}/{wvid}/e/{eid}/configuration returns
    one ``configurationParameters`` list -- each entry describes a knob
    (enum, bool, length, etc.) with its options and current default.

    Returns a UI-friendly normalised shape:
      ``{has_config: bool, parameters: [{id, name, type, default,
                                         options}]}``
    where ``options`` is a list of ``{value, label}`` for enum parameters,
    and empty for other types.
    """
    c = _client()
    try:
        resp = c.get(
            f"/elements/d/{did}/{wv}/{wvid}/e/{eid}/configuration") or {}
    except Exception:
        return {"has_config": False, "parameters": []}
    params = resp.get("configurationParameters") or []
    out_params = []
    for p in params:
        msg = p.get("message") or {}
        ptype = (p.get("typeName") or "").upper()
        param_id = msg.get("parameterId")
        name = msg.get("parameterName") or param_id
        default = msg.get("defaultValue")
        opts = []
        if ptype == "BTMCONFIGURATIONPARAMETERENUM":
            for o in msg.get("options") or []:
                om = o.get("message") or {}
                opts.append({
                    "value": om.get("option"),
                    "label": om.get("optionName") or om.get("option"),
                })
        out_params.append({
            "id": param_id,
            "name": name,
            "type": ptype.replace("BTMCONFIGURATIONPARAMETER", "")
                          .lower() or "unknown",
            "default": default,
            "options": opts,
        })
    return {
        "has_config": bool(out_params),
        "parameters": out_params,
    }


def encode_configuration(values: dict) -> str:
    """Turn ``{parameter_id: value}`` into the URL-safe string Onshape
    expects on translation requests.  Empty dict -> empty string."""
    if not values:
        return ""
    # Onshape's format: "key1=val1;key2=val2"
    parts = []
    for k, v in values.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    return ";".join(parts)


def start_step_translation(did: str, wv: str, wvid: str, eid: str,
                            element_type: str = "ASSEMBLY",
                            configuration: str = "") -> dict:
    """Kick off an Onshape STEP translation job for the element.

    Onshape returns ``{id, requestState, ...}`` where ``id`` is the
    translation id used to poll status.

    Args:
      element_type: ``"ASSEMBLY"`` or ``"PARTSTUDIO"`` -- selects which
        REST endpoint family to call.
    """
    c = _client()
    body = {
        "formatName": "STEP",
        # AS-DESIGNED keeps the geometry exactly as authored.  AP214 is
        # the most-supported flavour; ANSI units = mm by Onshape default.
        "storeInDocument": False,
        "elementId": eid,
        "step": {
            "stepVersion": "AP214",
            "stepUnit": "millimeter",
        },
    }
    if configuration:
        # Onshape accepts the encoded "key1=val1;key2=val2" string under
        # the top-level ``configuration`` field on the translation body.
        body["configuration"] = configuration
    if element_type == "PARTSTUDIO":
        path = f"/partstudios/d/{did}/{wv}/{wvid}/e/{eid}/translations"
    else:
        path = f"/assemblies/d/{did}/{wv}/{wvid}/e/{eid}/translations"
    resp = c.post(path, json=body) or {}
    return {
        "translation_id": resp.get("id"),
        "state": resp.get("requestState") or "ACTIVE",
        "raw": resp,
    }


def poll_translation(translation_id: str) -> dict:
    """Check status of an in-flight translation job.

    Returns ``{state, result_external_data_ids}`` where ``state`` is one of
    ``"ACTIVE"``, ``"DONE"``, ``"FAILED"``.  When DONE, the
    ``resultExternalDataIds`` list contains the download id.
    """
    c = _client()
    resp = c.get(f"/translations/{translation_id}") or {}
    return {
        "state": resp.get("requestState") or "UNKNOWN",
        "external_data_ids": resp.get("resultExternalDataIds") or [],
        "failure_reason": resp.get("failureReason"),
        "did": resp.get("documentId"),
        "raw": resp,
    }


def download_external_data(did: str, external_data_id: str,
                            dest: Path) -> Path:
    """Stream Onshape's translation result to ``dest``.

    Uses the OnshapeClient's session directly (bypassing the JSON
    auto-decode in `client.get`) because the response body is raw STEP.
    """
    c = _client()
    url = c._url(f"/documents/d/{did}/externaldata/{external_data_id}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with c._session.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=128 * 1024):
                if chunk:
                    f.write(chunk)
    return dest


# ---------------------------------------------------------------------------
# Async job tracker (in-memory; resets on server restart -- fine for a
# single-user local tool)
# ---------------------------------------------------------------------------

# job_id -> dict with:
#   {id, url, status: "queued"|"resolving"|"translating"|"downloading"
#                    |"ready"|"error",
#    progress: 0..100, message: str,
#    document_name, element_name, element_type,
#    source_id, step_path, error, started_at, updated_at}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _update_job(job_id: str, **patch) -> dict:
    with _JOBS_LOCK:
        if job_id not in _JOBS:
            return {}
        _JOBS[job_id].update(patch)
        _JOBS[job_id]["updated_at"] = _now_iso()
        return dict(_JOBS[job_id])


def get_job(job_id: str) -> Optional[dict]:
    with _JOBS_LOCK:
        if job_id not in _JOBS:
            return None
        return dict(_JOBS[job_id])


def list_jobs() -> list[dict]:
    with _JOBS_LOCK:
        return [dict(j) for j in _JOBS.values()]


def _run_import(job_id: str, url: str) -> None:
    """Body of the import worker thread.  Updates _JOBS in place."""
    try:
        _update_job(job_id, status="resolving", progress=5,
                     message="parsing URL...")
        ids = parse_onshape_url(url)
        did, wv, wvid, eid = ids["did"], ids["wv"], ids["wvid"], ids["eid"]
        if not eid:
            raise OnshapeURLError(
                "URL must include the /e/<eid> element segment")

        _update_job(job_id, status="resolving", progress=15,
                     message="fetching document metadata...")
        doc = get_document_info(did)
        elem = get_element_info(did, wv, wvid, eid)
        _update_job(job_id,
                     document_name=doc["name"],
                     element_name=elem["name"],
                     element_type=elem["type"],
                     onshape_ids={"did": did, "wid": wvid if wv == "w" else None,
                                   "vid": wvid if wv == "v" else None,
                                   "mid": wvid if wv == "m" else None,
                                   "eid": eid, "wv": wv})

        _update_job(job_id, status="translating", progress=25,
                     message=f"translating {elem['name']} to STEP...")
        tr = start_step_translation(did, wv, wvid, eid,
                                     element_type=elem["type"])
        tid = tr["translation_id"]
        if not tid:
            raise RuntimeError(
                f"Onshape did not return a translation id: {tr['raw']!r}")

        # Poll every 3s for up to 15 min.  Most assemblies finish in
        # under a minute; really chunky ones (e.g. Presto) can take 5+.
        deadline = time.time() + 15 * 60
        last_state = "ACTIVE"
        while time.time() < deadline:
            time.sleep(3.0)
            st = poll_translation(tid)
            last_state = st["state"]
            if last_state == "DONE":
                break
            if last_state == "FAILED":
                raise RuntimeError(
                    f"Onshape translation failed: "
                    f"{st.get('failure_reason') or '(no reason given)'}")
            # crude progress bump: cap at 70 while polling
            cur = _JOBS.get(job_id, {}).get("progress", 25)
            _update_job(job_id, progress=min(70, cur + 3),
                         message="translating... " + last_state.lower())
        else:
            raise TimeoutError(
                "Onshape translation did not finish within 15 minutes")

        ext_ids = st.get("external_data_ids") or []
        if not ext_ids:
            raise RuntimeError(
                "Onshape reported DONE but no resultExternalDataIds")

        _update_job(job_id, status="downloading", progress=75,
                     message="downloading STEP...")
        # Filename: doc-name slug + first 6 chars of did to disambiguate
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_",
                       doc["name"])[:48].strip("_") or "onshape"
        source_id = f"{slug}_{did[:6]}".lower()
        IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
        step_path = IMPORTS_DIR / f"{source_id}.step"
        # If we already have a STEP for this same source id (e.g. user
        # re-imported the same document), overwrite -- the bytes ARE
        # the new geometry.
        download_external_data(st.get("did") or did, ext_ids[0], step_path)

        _update_job(job_id, status="ready", progress=100,
                     message="ready",
                     source_id=source_id,
                     step_path=str(step_path))
    except Exception as e:
        _update_job(job_id, status="error", progress=100,
                     error=str(e), message=f"error: {e}")


def start_import(url: str) -> dict:
    """Kick off a background import.  Returns the initial job dict."""
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "id": job_id,
            "url": url,
            "status": "queued",
            "progress": 0,
            "message": "queued",
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
        }
    t = threading.Thread(target=_run_import, args=(job_id, url),
                          daemon=True, name=f"onshape-import-{job_id}")
    t.start()
    return dict(_JOBS[job_id])
