/* Debug screenshot bridge.
 *
 * Pushes the current 2D drawing pane + 3D canvas to the server
 * (POST /api/debug/shot) so Claude can pull them via a token-guarded GET
 * (GET /api/debug/shot/<which>?token=...) instead of driving a slow browser.
 *
 *   - Auto-captures (throttled) while the user is actively interacting.
 *   - Manual: the floating 📷 button (bottom-right) or Ctrl+Shift+C.
 *
 * No-op if the API isn't reachable.  Captures are downscaled to keep them
 * small.  The 2D rasterise uses the pane's own <svg> + <style>, so inline
 * styles + highlight overlays show; head-level CSS weights may not (fine for
 * debugging shape/alignment/colour).
 */
(function () {
  'use strict';
  var API = (window.IFU_API_BASE || '');
  var MAXW = 1400;
  var lastPush = 0;

  function _activeSvg() {
    return document.querySelector('.svg-pane.active svg')
        || document.querySelector('.svg-pane[data-view="__live__"] svg')
        || document.querySelector('.svg-pane svg');
  }

  function _push(which, dataUrl, note) {
    if (!dataUrl) return Promise.resolve();
    return fetch(API + '/api/debug/shot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ which: which, png: dataUrl, note: note || '' }),
    }).catch(function () {});
  }

  function _svgToPng(svg) {
    return new Promise(function (resolve) {
      try {
        var clone = svg.cloneNode(true);
        var vb = (svg.getAttribute('viewBox') || '').split(/\s+/).map(parseFloat);
        var w = svg.clientWidth || (vb.length === 4 ? vb[2] : 1000);
        var h = svg.clientHeight || (vb.length === 4 ? vb[3] : 1000);
        clone.setAttribute('width', w);
        clone.setAttribute('height', h);
        var xml = new XMLSerializer().serializeToString(clone);
        var src = 'data:image/svg+xml;base64,' +
          btoa(unescape(encodeURIComponent(xml)));
        var img = new Image();
        img.onload = function () {
          var iw = img.width || w, ih = img.height || h;
          var scale = Math.min(1, MAXW / iw);
          var cw = Math.max(1, Math.round(iw * scale));
          var ch = Math.max(1, Math.round(ih * scale));
          var cv = document.createElement('canvas');
          cv.width = cw; cv.height = ch;
          var ctx = cv.getContext('2d');
          ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, cw, ch);
          ctx.drawImage(img, 0, 0, cw, ch);
          try { resolve(cv.toDataURL('image/png')); } catch (e) { resolve(null); }
        };
        img.onerror = function () { resolve(null); };
        img.src = src;
      } catch (e) { resolve(null); }
    });
  }

  function _downscale(dataUrl) {
    return new Promise(function (resolve) {
      try {
        var img = new Image();
        img.onload = function () {
          var scale = Math.min(1, MAXW / (img.width || MAXW));
          if (scale >= 1) return resolve(dataUrl);
          var cv = document.createElement('canvas');
          cv.width = Math.round(img.width * scale);
          cv.height = Math.round(img.height * scale);
          cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height);
          try { resolve(cv.toDataURL('image/png')); } catch (e) { resolve(dataUrl); }
        };
        img.onerror = function () { resolve(dataUrl); };
        img.src = dataUrl;
      } catch (e) { resolve(dataUrl); }
    });
  }

  function captureNow(note) {
    var jobs = [];
    var svg = _activeSvg();
    if (svg) jobs.push(_svgToPng(svg).then(function (p) { return _push('2d', p, note); }));
    var d3 = window.IFU_VIEWER && window.IFU_VIEWER.capture3D && window.IFU_VIEWER.capture3D();
    if (d3) jobs.push(_downscale(d3).then(function (p) { return _push('3d', p, note); }));
    return Promise.all(jobs);
  }
  window.IFU_SEND_SHOT = captureNow;

  // ---- auto-capture while actively interacting -----------------------------
  var _lastInteract = 0;
  ['mousedown', 'wheel', 'keydown', 'touchstart'].forEach(function (ev) {
    window.addEventListener(ev, function () { _lastInteract = Date.now(); },
      { passive: true });
  });
  var _loadedAt = Date.now();
  setInterval(function () {
    var now = Date.now();
    // Capture continuously for the first 30s after load (catches the render
    // + footprint settling without needing an interaction), then fall back
    // to interaction-gated so an idle tab stops uploading.
    var fresh = (now - _loadedAt) < 30000;
    if (!fresh && now - _lastInteract > 8000) return;
    if (now - lastPush < 2500) return;        // throttle
    lastPush = now;
    captureNow('auto');
  }, 1500);

  // One-shot capture after load + on route change, so a fresh navigation
  // (including one driven server-side for debugging) pushes a current view
  // even without a user interaction.
  function _loadCapture() { setTimeout(function () { captureNow('load'); }, 3500); }
  if (document.readyState === 'complete') _loadCapture();
  else window.addEventListener('load', _loadCapture, { once: true });
  window.addEventListener('hashchange', function () {
    setTimeout(function () { captureNow('route'); }, 2800);
  });

  // ---- manual triggers -----------------------------------------------------
  window.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey &&
        (e.key === 'C' || e.key === 'c')) {
      e.preventDefault();
      captureNow('manual');
    }
  });
  function addButton() {
    if (document.getElementById('ifu-shot-btn')) return;
    var b = document.createElement('button');
    b.id = 'ifu-shot-btn';
    b.textContent = '📷';
    b.title = 'Send current 2D+3D view to Claude (debug)';
    b.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:2147483646;' +
      'width:36px;height:36px;border-radius:50%;border:1px solid #2c5d51;' +
      'background:#13302a;color:#fff;cursor:pointer;font-size:16px;opacity:.45;' +
      'box-shadow:0 2px 8px rgba(0,0,0,.3)';
    b.onmouseenter = function () { b.style.opacity = '1'; };
    b.onmouseleave = function () { b.style.opacity = '.45'; };
    b.onclick = function () {
      b.textContent = '⏳';
      captureNow('manual').then(function () {
        b.textContent = '✓';
        setTimeout(function () { b.textContent = '📷'; }, 1200);
      });
    };
    document.body.appendChild(b);
  }
  if (document.body) addButton();
  else document.addEventListener('DOMContentLoaded', addButton, { once: true });
})();
