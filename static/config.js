// Front-end -> API origin.  Loaded BEFORE the app scripts so API_BASE
// picks it up.
//
//  - Local dev / single-host deploy: leave empty -> same-origin.
//  - Split deploy (UI on Vercel, API on Render): set this to your Render
//    service URL, e.g. "https://ifu-api.onrender.com" (NO trailing slash).
//    On Vercel, override this file's content with that value.
window.IFU_API_BASE = window.IFU_API_BASE || "";
