# Deploying the IFU viewer for the team

**Architecture: split.**

```
  Browser ──HTTPS──▶  Vercel (static UI: index.html + /static/*)
     │
     └──HTTPS (CORS)──▶  Render (Flask + OCCT compute API: /api/*)
                              └── persistent disk /data  (figures, views,
                                  projects, sources, imported STEPs, thumbs)
```

Why split: the UI is static files (great fit for Vercel); the API is
CPU-heavy, stateful OCCT rendering that needs an always-on box with a
persistent disk — which Vercel's serverless model can't do (renders
exceed the function time limit and the filesystem isn't durable). Render
runs the long-lived container.

The Phase-0 work already separated the front-end (`viewer.html` +
`static/`) from the back-end, so the seam is clean. `window.IFU_API_BASE`
(set by `static/config.js`) points the UI at the API; CORS lets the
cross-origin calls through.

---

## 1. Back-end → Render

Files: `Dockerfile`, `.dockerignore`, `requirements.txt`, `wsgi.py`,
`gunicorn.conf.py`, `render.yaml`.

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → select the repo (it reads
   `render.yaml`). It builds the Docker image and creates the `ifu-api`
   web service with a 10 GB disk at `/data`.
3. In the service's **Environment**, set the two secrets:
   - `ONSHAPE_ACCESS_KEY`
   - `ONSHAPE_SECRET_KEY`
   (only needed to import *new* Onshape docs — see Open Items.)
4. Note the service URL, e.g. `https://ifu-api.onrender.com`.
5. Health check: `GET https://ifu-api.onrender.com/api/healthz` → `{"ok":true,...}`.

Notes:
- **One worker on purpose** (`gunicorn.conf.py`): the in-memory shape
  cache + render lock assume a single process. Renders queue — fine for a
  small team. To scale, run multiple instances behind a load balancer
  (each gets its own cache; they share `/data`).
- **Plan/RAM**: `render.yaml` starts at `standard` (2 GB). Big assemblies
  may need `pro` (4 GB) — bump if you see OOM/restarts.
- **Region**: set to `frankfurt` (EU) for data residency; change if you
  prefer another.

## 2. Front-end → Vercel

Files: `vercel.json`, `static/config.js`.

1. Edit **`static/config.js`** → set your Render URL (no trailing slash):
   ```js
   window.IFU_API_BASE = "https://ifu-api.onrender.com";
   ```
   Commit it.
2. Vercel → **New Project** → import the repo. `vercel.json` assembles
   `public/` (= `out/viewer.html` as `index.html` + `static/*`) and
   serves it. No framework, no install step.
3. Deploy → note the Vercel URL, e.g. `https://accora-ifu.vercel.app`.
4. Back on Render, set **`IFU_ALLOWED_ORIGIN`** to that Vercel URL and
   redeploy (locks CORS to your front-end instead of `*`).

That's the happy path. **Read the open items before relying on it.**

---

## Open items (need your call / IT)

These are real and I could not resolve them from here:

1. **Auth — there is none in the app.** Put **Cloudflare Access** in
   front of both origins (gates by your Accora identity, no app login).
   Full step-by-step in **`CLOUDFLARE.md`**. It needs a custom domain on
   Cloudflare + your IdP, and one small app-side credential change I'll
   make once you've picked the domain structure. Don't expose the Render
   API publicly before this.

2. **Onshape import — RESOLVED (vendored).** The client used to load from
   a hardcoded local path; it's now vendored into the repo
   (`ifu/onshape_client_vendored.py`) and used automatically when the
   local copy isn't present (i.e. on Render). Set `ONSHAPE_ACCESS_KEY` /
   `ONSHAPE_SECRET_KEY` as Render secrets and imports work in the
   container. (Dev still uses your local sibling project if present.)

3. **Existing data + STEP files.** Your current figures/views/projects
   and imported `*.step` live in the local `out/` folder. The Render disk
   starts empty. To carry your work over, copy `out/figures`,
   `out/views`, `out/projects`, `out/sources`, `out/imports`,
   `out/figures/*.png`, `out/views/*.png` to `/data` on the Render disk
   (via a one-off shell on the instance, or an upload step). Or start
   fresh and re-import on the server.

4. **The cloud build is untested from here.** I can't run Docker / deploy
   to Render/Vercel locally, so the first build may need a tweak — most
   likely an extra apt lib for the OCCT wheels, or bumping the Render
   plan for RAM. Ping me with the build log and I'll fix it fast.

5. **Cost (rough):** Render `standard` ~\$25/mo + disk; Vercel
   Hobby/Pro. Modest.

---

## Local dev — unchanged

```
python serve.py            # http://127.0.0.1:5000, sources load on demand
```
`IFU_DATA_DIR` unset → uses `./out`. `config.js` empty → same-origin.
Nothing about local development changed.
