# Deploying the IFU viewer

**Single service on Render.** Flask serves the UI (viewer.html + /static/*) and
the OCCT compute API (/api/*) from one Docker container with a persistent disk.

```
Browser ──HTTPS──▶  https://ifu-api-xmm5.onrender.com
                        Flask: serves UI at /
                        Flask: compute API at /api/*
                        Persistent disk /data  (figures, views, projects,
                                                sources, imports, thumbnails)
```

**Live URLs**

| | |
|---|---|
| App | https://ifu-api-xmm5.onrender.com |
| Render dashboard | https://dashboard.render.com/web/srv-d8d0b5ek1jcs738f9on0 |
| GitHub repo | https://github.com/fredmarshaccora-alt/ifu-artwork |

---

## Deploying changes

Push to `main` → Render auto-builds and deploys (takes ~2 min; Docker layer
cache means only changed layers rebuild). Watch it at the dashboard URL above.

---

## First-time setup checklist

### 1. Upload your existing data ✅ DONE (2026-05-30)

The local `out/` data (291 files: figures, views, projects, sources,
imports) was seeded to the Render `/data` disk over HTTPS via the
token-guarded `/api/admin/upload` endpoint, because **SSH/scp to Render
is blocked on the Accora network** (port 22 intercepted by the corporate
proxy).

To re-seed in future (after more local work):
```bash
# 1. Set IFU_UPLOAD_TOKEN on the Render service (a random secret).
# 2. Redeploy so the container picks it up.
# 3. Locally:
set IFU_UPLOAD_TOKEN=<that token>
python upload_data_https.py
# 4. Clear IFU_UPLOAD_TOKEN on Render + redeploy to disable the endpoint.
```
The endpoint is **disabled by default** (403 unless the token env var is
set), writes only under figures/views/projects/sources/imports, and has a
path-traversal guard. `upload_data_https.py` uses `truststore` so TLS
verification stays on through the corporate SSL-inspection proxy.

### 2. Set Onshape API secrets ✅ needed to import new Onshape documents

In the Render dashboard: **Environment → Add Secret File / Secret**

| Key | Value |
|-----|-------|
| `ONSHAPE_ACCESS_KEY` | your Onshape API access key |
| `ONSHAPE_SECRET_KEY` | your Onshape API secret key |

Keys are in your Onshape account: **Account → API Keys**.
Already-imported sources (your local STEP/figures) work without these.

### 3. Auth / access control ✅ before sharing with the team

The app has no login. Put **Cloudflare Access** in front — see **`CLOUDFLARE.md`**
for the step-by-step. Needs a custom domain (e.g. `ifu.accora.com`) and your
IdP (Entra/Google) or email OTP. I'll make the one small app-side change once
you pick the domain.

---

## Costs (rough)

| Item | ~Cost |
|------|-------|
| Render `standard` instance | £18/mo |
| 10 GB persistent disk | £1.20/mo |
| **Total** | **~£20/mo** |

Free tier doesn't support Docker + persistent disk. Bump to `pro` (~£60/mo)
if big assemblies OOM (watch memory in the Render dashboard metrics).

---

## Local dev — unchanged

```bash
python serve.py   # http://127.0.0.1:5000
```

`IFU_DATA_DIR` unset → uses `./out`. Everything local, nothing changes.
