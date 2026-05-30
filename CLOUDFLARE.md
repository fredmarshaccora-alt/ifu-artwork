> ⚠️ **Superseded — not in use.** We chose **Supabase magic-link** auth
> instead (no custom domain / DNS / token needed). See **`AUTH.md`** for the
> live setup. This file is kept only as a reference for the Cloudflare route.

# Auth via Cloudflare Access (Zero Trust)

The app has no login of its own. Cloudflare Access sits **in front** of
both origins and gates them by your identity provider (Entra/Google
Workspace) or email one-time-PIN — no code in the app, your team signs
in with their existing Accora accounts.

Cloudflare Access can only protect hostnames that are **proxied through
Cloudflare DNS**, so you need a custom domain (raw `*.onrender.com` /
`*.vercel.app` can't be Access-protected directly). Use a subdomain of a
domain you control, e.g. `accora.com` → `ifu.accora.com` (UI) and
`ifu-api.accora.com` (API).

## Setup

1. **Domain in Cloudflare.** Add (or already have) the zone in
   Cloudflare. In **DNS**, create two records, both **proxied** (orange
   cloud):
   - `ifu` → CNAME → your Vercel deployment (then add `ifu.accora.com`
     as a Custom Domain in the Vercel project).
   - `ifu-api` → CNAME → your Render service (add `ifu-api.accora.com`
     as a Custom Domain in the Render service).

2. **Point the app at the custom API domain.**
   - `static/config.js`: `window.IFU_API_BASE = "https://ifu-api.accora.com";`
   - Render env `IFU_ALLOWED_ORIGIN = https://ifu.accora.com`

3. **Cloudflare Zero Trust → Access → Applications → Add (Self-hosted).**
   Add **two** applications (one per hostname) OR one covering the
   parent — for a single SSO session, add both under the same Zero Trust
   org so users authenticate once:
   - App A: `ifu.accora.com`
   - App B: `ifu-api.accora.com`
   Policy for both: **Allow**, rule = *Emails ending in* `@accora.com`
   (or an IdP group). Add your IdP (Entra ID / Google) under
   **Settings → Authentication**, or use the built-in **One-time PIN**.

4. **CORS for the API app (important for the split).** The browser calls
   `ifu-api` from `ifu` cross-origin, carrying the Access cookie. In the
   `ifu-api.accora.com` Access application → **Settings → CORS**: enable
   it, set **Allowed origins** = `https://ifu.accora.com`, **Allow
   credentials** = on, methods `GET,POST,PUT,DELETE,OPTIONS`.

5. **App-side credential change (one small edit I can make when you're
   ready).** For the Access cookie to flow on cross-origin API calls,
   the front-end fetches need `credentials: "include"` and the API must
   return `Access-Control-Allow-Credentials: true` with the **exact**
   origin (not `*`). Tell me you've set up the domains and I'll wire
   that (it's gated so local dev is unaffected).

## Simpler alternative (no CORS wrinkle)

If the cross-origin Access setup is fiddly, route **everything under one
hostname**: a Cloudflare rule/Worker on `ifu.accora.com` sends `/api/*`
to Render and everything else to Vercel. Then the browser sees one
origin → no CORS, one Access policy, one SSO. Caveat: Cloudflare's proxy
times out long requests (~100s on Free/Pro) — fine for typical renders,
but a multi-minute big-assembly render could be cut off. Most renders
are well under that.

## What this needs from you

- A Cloudflare zone + a custom domain/subdomain.
- Your IdP connected in Zero Trust (or use email OTP to start).
- The Render + Vercel deploys live first (so the hostnames resolve).

Once the domains are decided, ping me and I'll make the small app-side
credential/CORS change to match.
