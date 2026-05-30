// Front-end -> API origin.  Loaded BEFORE the app scripts so API_BASE
// picks it up.
//
//  - Local dev / single-host deploy: leave empty -> same-origin.
//  - Split deploy (UI on Vercel, API on Render): set this to your Render
//    service URL, e.g. "https://ifu-api.onrender.com" (NO trailing slash).
//    On Vercel, override this file's content with that value.
window.IFU_API_BASE = window.IFU_API_BASE || "";

// --- Supabase magic-link auth (public keys -- safe to ship to the browser).
// auth.js reads these; if IFU_SUPABASE_URL is empty, auth is disabled (local
// dev). The server enforces the real check (IFU_REQUIRE_AUTH on Render).
window.IFU_SUPABASE_URL = window.IFU_SUPABASE_URL ||
  "https://rcpwmjuytphonxydwyyf.supabase.co";
window.IFU_SUPABASE_ANON_KEY = window.IFU_SUPABASE_ANON_KEY ||
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJjcHdtanV5dHBob254eWR3eXlmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM0OTk1NTIsImV4cCI6MjA4OTA3NTU1Mn0.Y3j5yYVChVz_Yq5vx3IYRB7kE4xYsiwGYE0IomPskrI";
// Comma-separated e-mail domains allowed to sign in (UX hint; the server
// enforces the same list authoritatively).
window.IFU_AUTH_DOMAINS = window.IFU_AUTH_DOMAINS || "accora.care";
