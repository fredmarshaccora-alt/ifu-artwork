/* Supabase magic-link auth gate.
 *
 * Loads BEFORE the app scripts.  Responsibilities:
 *   1. Synchronously mirror any existing Supabase session token into an
 *      `ifu_token` cookie, so the app's first fetch()/<img> requests are
 *      authenticated (the server reads this cookie).
 *   2. Show a full-screen login overlay when there's no valid session, and
 *      send a magic link on submit.
 *   3. Keep the cookie in sync as the token refreshes; clear it on sign-out.
 *
 * Disabled (no-op) when window.IFU_SUPABASE_URL is empty -- local dev.
 */
(function () {
  "use strict";

  var URL_ = window.IFU_SUPABASE_URL || "";
  var ANON = window.IFU_SUPABASE_ANON_KEY || "";
  if (!URL_ || !ANON) return; // auth disabled (local/dev)

  var REF = (URL_.match(/^https?:\/\/([^.]+)\./) || [])[1] || "";
  var STORAGE_KEY = "sb-" + REF + "-auth-token";
  var DOMAINS = (window.IFU_AUTH_DOMAINS || "")
    .split(",").map(function (s) { return s.trim().toLowerCase(); })
    .filter(Boolean);

  // ---- cookie helpers ------------------------------------------------------
  function setCookie(tok, maxAge) {
    document.cookie = "ifu_token=" + tok +
      "; path=/; SameSite=Lax; Secure; max-age=" + (maxAge || 3600);
  }
  function clearCookie() {
    document.cookie = "ifu_token=; path=/; SameSite=Lax; Secure; max-age=0";
  }

  // ---- 1. synchronous token mirror (runs before the app boots) -------------
  function storedSession() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      var s = JSON.parse(raw);
      // supabase-js may wrap the session under currentSession
      return s.access_token ? s : (s.currentSession || null);
    } catch (e) { return null; }
  }
  (function mirrorNow() {
    var s = storedSession();
    if (s && s.access_token) {
      var exp = s.expires_at ? s.expires_at * 1000 : 0;
      if (!exp || exp > Date.now() + 5000) setCookie(s.access_token);
    }
  })();

  // ---- 2. login overlay ----------------------------------------------------
  function emailAllowed(email) {
    email = (email || "").toLowerCase();
    if (!DOMAINS.length) return true;
    return DOMAINS.some(function (d) { return email.endsWith("@" + d); });
  }

  function buildOverlay() {
    var o = document.createElement("div");
    o.id = "ifu-auth-overlay";
    o.style.cssText = [
      "position:fixed", "inset:0", "z-index:2147483647",
      "background:#0f1c1a", "color:#fff",
      "display:flex", "align-items:center", "justify-content:center",
      "font-family:Arial,Helvetica,sans-serif"
    ].join(";");
    o.innerHTML =
      '<div style="width:340px;max-width:88vw;background:#13302a;border:1px solid #1f4a40;' +
        'border-radius:14px;padding:28px 26px;box-shadow:0 12px 40px rgba(0,0,0,.45)">' +
        '<div style="font-size:20px;font-weight:bold;letter-spacing:.5px;margin-bottom:4px">ACCORA IFU viewer</div>' +
        '<div id="ifu-auth-sub" style="font-size:13px;color:#9fc7bd;margin-bottom:18px">Sign in with your Accora email to continue.</div>' +
        '<input id="ifu-auth-email" type="email" autocomplete="email" placeholder="you@accora.care" ' +
          'style="width:100%;box-sizing:border-box;padding:11px 12px;border-radius:8px;border:1px solid #2c5d51;' +
          'background:#0f231f;color:#fff;font-size:14px;outline:none"/>' +
        '<button id="ifu-auth-btn" ' +
          'style="width:100%;margin-top:12px;padding:11px;border:0;border-radius:8px;cursor:pointer;' +
          'background:#00836a;color:#fff;font-size:14px;font-weight:bold">Email me a sign-in link</button>' +
        '<div id="ifu-auth-msg" style="font-size:12.5px;margin-top:12px;min-height:18px;color:#cfe8e1"></div>' +
      '</div>';
    document.body.appendChild(o);
    return o;
  }

  function showOverlay(client) {
    if (document.getElementById("ifu-auth-overlay")) return;
    var ensure = function () {
      var o = buildOverlay();
      var email = o.querySelector("#ifu-auth-email");
      var btn = o.querySelector("#ifu-auth-btn");
      var msg = o.querySelector("#ifu-auth-msg");
      function submit() {
        var v = (email.value || "").trim();
        if (!v) { msg.textContent = "Enter your email."; return; }
        if (!emailAllowed(v)) {
          msg.style.color = "#ffb4a8";
          msg.textContent = "Only " + DOMAINS.map(function (d) { return "@" + d; }).join(" / ") + " addresses are allowed.";
          return;
        }
        btn.disabled = true; btn.textContent = "Sending…";
        msg.style.color = "#cfe8e1"; msg.textContent = "";
        client.auth.signInWithOtp({
          email: v,
          options: { emailRedirectTo: window.location.origin + window.location.pathname }
        }).then(function (res) {
          btn.disabled = false; btn.textContent = "Email me a sign-in link";
          if (res.error) { msg.style.color = "#ffb4a8"; msg.textContent = res.error.message; }
          else { msg.style.color = "#9fe8c9"; msg.textContent = "Check your inbox — click the link to sign in."; }
        });
      }
      btn.addEventListener("click", submit);
      email.addEventListener("keydown", function (e) { if (e.key === "Enter") submit(); });
      email.focus();
    };
    if (document.body) ensure();
    else document.addEventListener("DOMContentLoaded", ensure, { once: true });
  }

  function removeOverlay() {
    var o = document.getElementById("ifu-auth-overlay");
    if (o) o.remove();
  }

  // ---- 3. supabase client + auth state -------------------------------------
  function init() {
    if (!window.supabase || !window.supabase.createClient) {
      // supabase-js not loaded yet -- retry shortly.
      return setTimeout(init, 60);
    }
    var client = window.supabase.createClient(URL_, ANON, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true }
    });

    client.auth.onAuthStateChange(function (event, session) {
      if (session && session.access_token) {
        setCookie(session.access_token);
        if (event === "SIGNED_IN") {
          // Came back from a magic link -- token is in the URL hash
          // (implicit flow) or a ?code= param (PKCE). Reload to a clean URL
          // so the app boots fresh with the cookie already set.
          var fromLink = window.location.hash.indexOf("access_token") !== -1 ||
                         window.location.search.indexOf("code=") !== -1;
          if (fromLink) {
            window.location.replace(window.location.origin + window.location.pathname);
            return;
          }
          removeOverlay();
        }
      } else if (event === "SIGNED_OUT") {
        clearCookie();
        showOverlay(client);
      }
    });

    client.auth.getSession().then(function (res) {
      var session = res && res.data && res.data.session;
      if (session && session.access_token) {
        setCookie(session.access_token);
        removeOverlay();
      } else {
        clearCookie();
        showOverlay(client);
      }
    });

    // Expose a sign-out helper for a future menu item.
    window.IFU_AUTH = {
      signOut: function () { clearCookie(); return client.auth.signOut(); },
      client: client
    };
  }

  init();
})();
