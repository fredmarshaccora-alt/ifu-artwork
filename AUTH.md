# Authentication — Supabase magic-link

The app is gated by **Supabase magic-link** auth. Team members enter their
`@accora.care` email, click the link in the email, and they're in. No
passwords, no custom domain, no Cloudflare.

(We started down the Cloudflare Access route — see `CLOUDFLARE.md` — but it
needs a custom domain + DNS + API-token wrangling. Supabase was simpler and
reuses a stack Accora already runs.)

## How it works

```
Browser ──▶ login overlay (static/js/auth.js + supabase-js)
   │           └─ signInWithOtp(email)  →  Supabase emails a magic link
   ▼
Click link ──▶ app reloads, session token mirrored into `ifu_token` cookie
   │
   ▼
Every /api/* request carries the cookie ──▶ serve.py before_request:
   • verifies the JWT against Supabase's public JWKS (ES256, no shared secret)
   • checks the email domain is in IFU_ALLOWED_EMAIL_DOMAINS
   • 401 if missing/expired, 403 if wrong domain
```

Cookie (not an `Authorization` header) because the app loads thumbnails via
`<img src>`, which can't send custom headers — a cookie rides along with both
`fetch()` and `<img>` requests automatically.

## The pieces

| Where | What |
|-------|------|
| `static/config.js` | Public Supabase URL + anon key + allowed domains (safe to ship) |
| `static/js/auth.js` | Login overlay, magic-link send, cookie sync, sign-out helper |
| `serve.py` `_require_auth` | Server-side JWT verification + domain check (opt-in) |
| Render env | `IFU_REQUIRE_AUTH=1`, `SUPABASE_JWKS_URL=…/auth/v1/.well-known/jwks.json`, `IFU_ALLOWED_EMAIL_DOMAINS=accora.care` |
| Supabase | Auth → URL Configuration → Site URL + Redirect URL = the app URL |

## Common tasks

**Add/abide more email domains:** edit the `IFU_ALLOWED_EMAIL_DOMAINS` env var
on Render (comma-separated, e.g. `accora.care,accora.uk.com`) and update
`window.IFU_AUTH_DOMAINS` in `config.js` for the matching UX hint. Redeploy.

**Turn auth off (e.g. debugging):** set `IFU_REQUIRE_AUTH=0` on Render and
redeploy. Local dev (`python serve.py`) is always open — auth is opt-in.

**Switch to a different Supabase project:** update the URL + anon key in
`config.js`, the `SUPABASE_JWKS_URL` env var, and add the app URL to the new
project's redirect allowlist.

**Add "Sign in with Microsoft/Google":** enable the provider in Supabase Auth,
register the OAuth app, then add a button calling
`IFU_AUTH.client.auth.signInWithOAuth({provider})`. Magic-link keeps working
alongside it.

**Who's signed in (server-side):** `request.environ["ifu_user_email"]` is set
on every authenticated request — available for audit logging / per-user data.

## Lock down the bare onrender URL (optional hardening)

Auth already protects all `/api/*` data on every hostname, so the bare
`*.onrender.com` URL is safe — its data endpoints 401 without a session. If
you later add a custom domain, the same auth applies; nothing extra needed.
