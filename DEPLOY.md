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

### 1. Upload your existing data ✅ needed once

Your local `out/` folder has figures, views, projects, and sources that the
cloud server doesn't have yet (its `/data` disk started empty).

```bash
# In Git Bash from the project root:
bash upload_data_to_render.sh
```

Requires an SSH key added to Render (see step 2). Safe to re-run — rsync.

### 2. Add your SSH key to Render ✅ needed for data upload + SSH access

In the Render dashboard for the `ifu-api` service:

**Settings → SSH Keys → Add SSH Public Key**

Paste the contents of `~/.ssh/id_rsa.pub` (or `id_ed25519.pub`).
If you don't have one: `ssh-keygen -t ed25519` in Git Bash.

After adding it, `render ssh srv-d8d0b5ek1jcs738f9on0` or the scp in
`upload_data_to_render.sh` will work.

### 3. Set Onshape API secrets ✅ needed to import new Onshape documents

In the Render dashboard: **Environment → Add Secret File / Secret**

| Key | Value |
|-----|-------|
| `ONSHAPE_ACCESS_KEY` | your Onshape API access key |
| `ONSHAPE_SECRET_KEY` | your Onshape API secret key |

Keys are in your Onshape account: **Account → API Keys**.
Already-imported sources (your local STEP/figures) work without these.

### 4. Auth / access control ✅ before sharing with the team

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
