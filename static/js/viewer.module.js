// --- 3D view-finder (three.js) --------------------------------------------
// Z-locked orbit: camera.up = world Z, so vertical edges in the model stay
// vertical on screen no matter where you orbit to.  Loads the inlined GLB
// for the active source, renders meshes with a Composer-ish look (light
// face fill + heavy crease edges), reads out the live view_dir, and offers
// a "copy view_dir" button to capture an angle for pasting into the
// Python-side STD_VIEWS / VIEWS list.

import * as THREE from 'three';
// Expose for debugging + tests (the ES-module scope is otherwise sealed)
window.THREE = THREE;
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { ViewHelper } from 'three/addons/helpers/ViewHelper.js';
// Onshape-quality look: room IBL + SSAO + tone mapping.  Without these
// the renderer ships pre-PBR-era graphics (raw colors, no soft shading,
// no environment).
import { RoomEnvironment }
  from 'three/addons/environments/RoomEnvironment.js';
import { EffectComposer }
  from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass }
  from 'three/addons/postprocessing/RenderPass.js';
import { SSAOPass }
  from 'three/addons/postprocessing/SSAOPass.js';
import { OutputPass }
  from 'three/addons/postprocessing/OutputPass.js';

const canvas = document.getElementById('webgl-canvas');
const wrap3d = document.getElementById('webgl-wrap');
const readout = document.getElementById('viewdir-readout');

// "Is 3D currently visible?" -- driven by the body's layout class so we
// don't have to query the wrap3d element style (CSS rules with !important
// can stomp on classList).
const is3DVisible = () => {
  const cl = document.body.classList;
  return cl.contains('layout-split') || cl.contains('layout-3d');
};

let scene, camera, renderer, controls, viewHelper;
let composer = null;       // EffectComposer for SSAO postprocess
let ssaoPass = null;       // tunable
let envTexture = null;     // PMREM-baked room environment map
let _useComposer = true;   // toggleable for perf-debug

// On-demand rendering bookkeeping.  Was: animate() called
// renderer.render() every frame at 60 FPS even when idle, burning
// GPU for zero visual change.  Now: render only when dirty.  Dirty
// triggers: OrbitControls 'change', viewHelper animations,
// loadSource(), highlight/style mutations, programmatic camera snaps.
let _needsRender = true;     // true => render this frame
let _interacting = false;    // user is dragging/zooming in OrbitControls
let _lastRenderedAt = 0;     // perf telemetry
function requestRender() { _needsRender = true; }
// Expose so other code paths (style changes, highlight updates) can
// poke it without going through OrbitControls.
window._requestRender = requestRender;
// ViewHelper rendering also needs the main renderer's auto-clear off,
// then we manually clear before main + after main draw the gizmo.
let _viewHelperClock = null;
let loaded = new Map();      // file_id -> THREE.Group
let active = null;           // currently visible group
let partByName = new Map();  // "part_NNN" -> THREE.Object3D
let inited = false;

function init() {
  if (inited) return;
  inited = true;

  scene = new THREE.Scene();
  // Soft, slightly-cool studio backdrop directly in three.js.  We
  // tried transparent canvas + CSS gradient first but the post-
  // process composer wipes the alpha to opaque black, which made
  // the canvas read as empty.  Painting the gradient as a scene
  // background works in both render paths (composer or plain).
  scene.background = new THREE.Color(0xf3f4f6);

  const r = canvas.getBoundingClientRect();
  // OrthographicCamera, NOT perspective: OCCT HLR uses orthographic
  // projection, so the SVG never has converging lines.  If the 3D pane
  // were perspective, the same iso direction would look different
  // between 2D and 3D (perspective foreshortens far edges).  Bounds are
  // re-fit in frame() per source; here we just set up the camera shell.
  const aspect = (r.width || 1) / (r.height || 1);
  camera = new THREE.OrthographicCamera(
    -1000 * aspect, 1000 * aspect, 1000, -1000, -100000, 100000);
  camera.up.set(0, 0, 1);                // Z-up world: verticals stay vertical
  camera.position.set(-2000, -4000, 3000);
  camera.lookAt(0, 0, 0);

  renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: true,
    preserveDrawingBuffer: true,  // required for screenshot exporter
  });
  renderer.setSize(r.width, r.height, false);
  renderer.setPixelRatio(window.devicePixelRatio || 1);
  // ACES Filmic + sRGB output: the single most-impactful one-liner.
  // Without this, MeshStandardMaterial colors are crushed; with it,
  // they pop the same way Onshape's do.
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  // Tuned to match Onshape's soft, slightly-cool default render: a
  // touch under 1.0 keeps the pastel palette from blowing out under
  // direct light.
  renderer.toneMappingExposure = 0.95;
  // Shadow maps for the contact shadow plane below the model
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  // ---- Image-based lighting (IBL) -----------------------------------
  // RoomEnvironment is a built-in scene of soft-coloured panels that,
  // when PMREM-baked, gives every MeshStandardMaterial in the scene
  // sky-lit ambient + soft reflections.  This is the difference
  // between "lit by three point lights" and "looks like a real CAD
  // workspace".
  try {
    const pmrem = new THREE.PMREMGenerator(renderer);
    pmrem.compileEquirectangularShader();
    const roomScene = new RoomEnvironment(renderer);
    envTexture = pmrem.fromScene(roomScene, 0.04).texture;
    scene.environment = envTexture;
    pmrem.dispose();
  } catch (e) {
    console.warn('[3d] IBL setup failed; falling back to lights only:', e);
  }

  // ---- Lights: gentle sun+fill on top of IBL -----------------------
  // IBL provides the ambient + reflections that make MeshStandardMaterial
  // look like CAD plastic; the directional sun adds just enough
  // direction-sense that cylinders read as round and plates have a
  // soft side.  Onshape's default render has very soft, almost
  // shadowless lighting; matching that means low sun intensity.
  scene.add(new THREE.AmbientLight(0xffffff, 0.10));
  const sun = new THREE.DirectionalLight(0xffffff, 0.42);
  sun.position.set(1000, -2000, 2500);
  sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024);
  sun.shadow.camera.near = 100;
  sun.shadow.camera.far  = 12000;
  sun.shadow.camera.left = -3000;
  sun.shadow.camera.right = 3000;
  sun.shadow.camera.top = 3000;
  sun.shadow.camera.bottom = -3000;
  sun.shadow.bias = -0.0008;
  sun.shadow.radius = 6;
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0xffffff, 0.15);
  fill.position.set(-2000, 1000, 500);
  scene.add(fill);

  // ---- Floor: contact shadow only, no grid lines -------------------
  // A big horizontal plane sits just below the model and ONLY receives
  // a soft shadow from the sun (transparent ShadowMaterial).  No
  // visible grid lines -- the gradient backdrop reads as the
  // "ground" surface, just like Onshape.
  const shadowMat = new THREE.ShadowMaterial({
    color: 0x000000,
    opacity: 0.12,    // softer to match the gentler sun
    transparent: true,
  });
  const shadowPlane = new THREE.Mesh(
    new THREE.PlaneGeometry(20000, 20000), shadowMat);
  shadowPlane.position.z = -10;
  shadowPlane.receiveShadow = true;
  shadowPlane.userData._helper = true;
  shadowPlane.userData._shadowPlane = true;
  scene.add(shadowPlane);

  // A faint grid for orientation reference -- shown subtly via a
  // helper that the existing perf code already filters by
  // userData._helper.  Drawn smaller and lighter than before so it
  // doesn't dominate the new studio-style backdrop.
  const grid = new THREE.GridHelper(6000, 60, 0xd4d4d8, 0xeaeaec);
  grid.rotation.x = Math.PI / 2;
  grid.material.transparent = true;
  grid.material.opacity = 0.35;
  grid.userData._helper = true;
  scene.add(grid);
  const axes = new THREE.AxesHelper(300);
  axes.userData._helper = true;
  scene.add(axes);

  controls = new OrbitControls(camera, canvas);
  controls.target.set(0, 0, 0);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  // Pan in the screen plane (drag moves the model with the cursor) instead
  // of the default world-up plane, which feels wrong once the model has an
  // up-axis rotation -- a big part of the "awful navigation".
  controls.screenSpacePanning = true;
  // Sensible speeds + don't let zoom blow past the model or flip over.
  controls.rotateSpeed = 0.9;
  controls.zoomSpeed = 0.9;
  controls.panSpeed = 0.9;
  controls.update();
  // On-demand rendering: only re-render when something visible changed.
  // OrbitControls fires 'change' on every drag/zoom; damping then
  // animates for a few frames after release.  We chase that tail by
  // keeping rendering alive until controls.update() returns false.
  controls.addEventListener('change', requestRender);
  // 'start' / 'end' bracket a user interaction; keep rendering across
  // the whole gesture so damping animates smoothly.
  controls.addEventListener('start', () => { _interacting = true; });
  controls.addEventListener('end',   () => { _interacting = false; });

  // ---- Postprocessing: SSAO (opt-in, ?ssao=1) ----------------------
  // SSAO via three.js's SSAOPass is unreliable with OrthographicCamera
  // -- it sometimes leaves the canvas completely blank because the
  // depth-reconstruction shader assumes perspective.  IBL + tone
  // mapping already gives a big chunk of the Onshape look without it,
  // so leave the composer OFF by default and let curious users opt in
  // via the URL flag to experiment.
  const ssaoOptIn = (new URLSearchParams(location.search)).get('ssao') === '1';
  if (ssaoOptIn) {
    try {
      composer = new EffectComposer(renderer);
      composer.setPixelRatio(window.devicePixelRatio || 1);
      composer.setSize(r.width || 800, r.height || 600);
      composer.addPass(new RenderPass(scene, camera));
      ssaoPass = new SSAOPass(scene, camera, r.width || 800, r.height || 600);
      ssaoPass.kernelRadius = 24;
      ssaoPass.minDistance = 0.0008;
      ssaoPass.maxDistance = 0.06;
      ssaoPass.output = SSAOPass.OUTPUT.Default;
      composer.addPass(ssaoPass);
      composer.addPass(new OutputPass());
      _useComposer = true;
    } catch (e) {
      console.warn('[3d] SSAO setup failed; running plain renderer:', e);
      composer = null; ssaoPass = null; _useComposer = false;
    }
  } else {
    composer = null; ssaoPass = null; _useComposer = false;
  }

  // ViewHelper (orientation gizmo): the floating axis-cube in the corner.
  // Click a face -> camera animates to that direction.  Renders as its
  // own overlay viewport in the bottom-right of the canvas.
  viewHelper = new ViewHelper(camera, renderer.domElement);
  viewHelper.controls = controls;
  viewHelper.controls.center = controls.target;
  _viewHelperClock = new THREE.Clock();
  // Click handling: forward canvas clicks to the helper when they land
  // in its viewport region.
  canvas.addEventListener('pointerdown', (e) => {
    if (!viewHelper) return;
    const rect = canvas.getBoundingClientRect();
    if (viewHelper.handleClick(e)) {
      // The helper consumed this click for navigation; cancel further
      // processing (so we don't accidentally raycast for selection).
      e.stopPropagation();
      requestRender();   // gizmo snap kicks off an animation loop
    }
  }, true);

  window.addEventListener('resize', resize);

  // Distinguish clicks from drag-orbits: only fire raycast on small motion
  let downPos = null;
  canvas.addEventListener('pointerdown', (e) => {
    canvas.classList.add('dragging');
    downPos = [e.clientX, e.clientY];
  });
  window.addEventListener('pointerup', (e) => {
    canvas.classList.remove('dragging');
    if (!downPos) return;
    const dx = e.clientX - downPos[0];
    const dy = e.clientY - downPos[1];
    downPos = null;
    if (Math.hypot(dx, dy) > 4) return;       // it was a drag, not a click
    if (e.target !== canvas) return;          // click landed off-canvas
    handleCanvasClick(e);
  });

  animate();
}

// Click-through state: repeat-clicking the same pixel cycles through ray
// intersections so parts hidden behind other parts become selectable.
let _lastClickPx = null;
let _lastClickRayCycle = 0;

function handleCanvasClick(e) {
  if (!active || !camera) return;
  scene.updateMatrixWorld(true);
  const rect = canvas.getBoundingClientRect();
  const ndc = new THREE.Vector2(
    ((e.clientX - rect.left) / rect.width) * 2 - 1,
    -((e.clientY - rect.top) / rect.height) * 2 + 1,
  );
  const raycaster = new THREE.Raycaster();
  raycaster.setFromCamera(ndc, camera);
  // Get ALL mesh hits, sorted by depth (closest first by default).
  // Then drop adjacent duplicates from the same part so cycling steps
  // through DIFFERENT parts, not different faces of the same part.
  const rawHits = raycaster.intersectObjects([active], true)
    .filter(h => h.object && h.object.isMesh);

  // Arrow-placement mode: the next click drops an arrow on the hit point
  // instead of selecting a part.
  if (_annotMode === 'arrow-straight' || _annotMode === 'arrow-rotation') {
    if (rawHits.length) _placeArrowFromHit(rawHits[0]);
    return;
  }
  // Explode mode: click attaches the translate gizmo to the picked part.
  if (_annotMode === 'explode' && rawHits.length) {
    const pidx = _partIdxOf(rawHits[0].object);
    if (pidx != null) { attachExplodeGizmo(pidx); return; }
  }

  const hits = [];
  let lastIdx = null;
  for (const h of rawHits) {
    const i = _partIdxOf(h.object);
    if (i !== lastIdx) { hits.push({ ...h, _partIdx: i }); lastIdx = i; }
  }

  // If this click is at (essentially) the same pixel as the last,
  // advance to the next-deepest hit.  Otherwise reset the cycle.
  const pxNow = [e.clientX, e.clientY];
  const samePx = _lastClickPx &&
    Math.abs(pxNow[0] - _lastClickPx[0]) < 4 &&
    Math.abs(pxNow[1] - _lastClickPx[1]) < 4;
  if (!samePx) _lastClickRayCycle = 0;
  _lastClickPx = pxNow;

  if (hits.length === 0) {
    if (!e.ctrlKey && !e.metaKey) window.IFU_VIEWER?.clearHighlights?.();
    return;
  }

  // Pick the hit at the current cycle position (modulo for wrap-around)
  const hit = hits[_lastClickRayCycle % hits.length];
  if (samePx) _lastClickRayCycle++;     // next click goes deeper
  const idx = hit._partIdx;
  if (idx != null) {
    window.IFU_VIEWER.togglePartHighlight(idx, {
      append: e.ctrlKey || e.metaKey,
    });
  }
}

function resize() {
  if (!renderer) return;
  const r = canvas.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return;
  renderer.setSize(r.width, r.height, false);
  // Ortho: maintain the on-screen scale by keeping (right - left) / width
  // and (top - bottom) / height equal across resizes.  Use the existing
  // half-height; recompute half-width from the new aspect.
  if (camera.isOrthographicCamera) {
    const halfHeight = (camera.top - camera.bottom) / 2;
    const aspect = r.width / r.height;
    const halfWidth = halfHeight * aspect;
    camera.left = -halfWidth;
    camera.right = halfWidth;
  } else if (camera.isPerspectiveCamera) {
    camera.aspect = r.width / r.height;
  }
  camera.updateProjectionMatrix();
  if (composer) {
    composer.setSize(r.width, r.height);
    if (ssaoPass && ssaoPass.setSize) ssaoPass.setSize(r.width, r.height);
  }
  _needsRender = true;
}

// Rolling FPS estimate.  We keep timestamps of the last 60 rendered
// frames; mean inter-frame delta gives ms/frame, inverted to FPS.
// Exposed via window.IFU_VIEWER.getRendererState() so the ?dbg=1 HUD
// (and tests) can sample it without polling rAF themselves.
const _fpsRing = new Float64Array(60);
let _fpsRingIdx = 0;
let _fpsRingFilled = false;
let _lastFrameMs = 0;
window.IFU_VIEWER_STATE = window.IFU_VIEWER_STATE || {};

function animate() {
  requestAnimationFrame(animate);
  // Sample timing even when 3D is hidden so the metric reflects the
  // animation loop frequency the OS is granting us (handy when the
  // browser tab is backgrounded -- rAF can throttle to ~1 Hz).
  const _nowMs = performance.now();
  if (_lastFrameMs) {
    const dt = _nowMs - _lastFrameMs;
    _fpsRing[_fpsRingIdx] = dt;
    _fpsRingIdx = (_fpsRingIdx + 1) % _fpsRing.length;
    if (_fpsRingIdx === 0) _fpsRingFilled = true;
    // Update window.IFU_VIEWER_STATE every 30 frames (~0.5s @ 60fps)
    // so reads are cheap.
    if (_fpsRingIdx % 30 === 0) {
      const n = _fpsRingFilled ? _fpsRing.length : _fpsRingIdx;
      let sum = 0;
      for (let i = 0; i < n; i++) sum += _fpsRing[i];
      const meanMs = n > 0 ? sum / n : 0;
      window.IFU_VIEWER_STATE.fps = meanMs > 0 ? 1000 / meanMs : 0;
      window.IFU_VIEWER_STATE.frameMs = meanMs;
    }
  }
  _lastFrameMs = _nowMs;

  if (!controls || !is3DVisible()) return;
  // Pump the ViewHelper's animation (face-snap interpolation) every
  // frame even if the user hasn't touched OrbitControls.  This counts
  // as a render trigger so the gizmo animates smoothly.
  if (viewHelper && viewHelper.animating) {
    const dt = _viewHelperClock ? _viewHelperClock.getDelta() : 0.016;
    viewHelper.update(dt);
    _needsRender = true;
  }
  // controls.update() returns true while damping is still animating
  // post-release; keep rendering until that settles.
  const controlsChanged = controls.update();
  if (controlsChanged || _interacting) _needsRender = true;

  // Resize check is cheap and must run every frame so a layout change
  // still gets caught.
  const r = canvas.getBoundingClientRect();
  if (renderer.domElement.width !== Math.round(r.width * (window.devicePixelRatio || 1)) ||
      renderer.domElement.height !== Math.round(r.height * (window.devicePixelRatio || 1))) {
    resize();
    _needsRender = true;
  }

  // ON-DEMAND RENDER: bail out before the GPU work if nothing's dirty.
  // Previously we burned ~16 ms/frame at 60 FPS for an idle scene;
  // now idle == 0% GPU.
  if (!_needsRender) return;
  _needsRender = false;
  _lastRenderedAt = _nowMs;

  // Main scene through the EffectComposer (SSAO + output) when
  // available; fall back to the raw renderer.render path when the
  // composer failed to set up (no SSAO support / old WebGL).
  if (_useComposer && composer) {
    composer.render();
  } else {
    renderer.autoClear = true;
    renderer.render(scene, camera);
  }
  // Overlay the orientation gizmo on top.  ViewHelper.render() leaves
  // the WebGL viewport pointing at its tiny corner region; restore
  // the full viewport explicitly so the next frame's main render
  // fills the whole canvas.
  if (viewHelper) {
    renderer.autoClear = false;
    viewHelper.render(renderer);
    const dpr = window.devicePixelRatio || 1;
    renderer.setViewport(0, 0, r.width * dpr, r.height * dpr);
  }
  updateReadout();
}

function updateReadout() {
  const d = camera.position.clone().sub(controls.target).normalize();
  readout.textContent =
    `view_dir = (${d.x.toFixed(3)}, ${d.y.toFixed(3)}, ${d.z.toFixed(3)})`;
}

// Build feature edges off the critical path so a 700-part Presto
// becomes interactive immediately, with edge lines fading in over the
// next second or two.  Each chunk works ~32 meshes (~80-100 ms total
// budget on a fast laptop; ~5 ms per chunk on the rAF/idle slice).
const _EDGE_CHUNK = 32;
function _buildEdgesInIdleChunks(meshes, file_id) {
  const schedule = (cb) => {
    if (typeof window.requestIdleCallback === 'function') {
      window.requestIdleCallback(cb, { timeout: 200 });
    } else {
      setTimeout(cb, 16);
    }
  };
  let cursor = 0;
  const step = () => {
    // Source may have been swapped out while we were idle; bail.
    if (!loaded.has(file_id)) return;
    const end = Math.min(cursor + _EDGE_CHUNK, meshes.length);
    for (let i = cursor; i < end; i++) {
      const obj = meshes[i];
      if (!obj || !obj.geometry || obj.userData._edgesBuilt) continue;
      try {
        const edges = new THREE.EdgesGeometry(obj.geometry, 45);
        const lines = new THREE.LineSegments(
          edges,
          new THREE.LineBasicMaterial({
            color: 0x2a3340,
            transparent: true,
            opacity: 0.55,
            linewidth: 1,
          })
        );
        lines.userData.isEdge = true;
        obj.add(lines);
        obj.userData._edgesBuilt = true;
      } catch (e) {
        console.warn('EdgesGeometry failed for', obj.name, e);
      }
    }
    cursor = end;
    requestRender();
    if (cursor < meshes.length) {
      schedule(step);
    }
  };
  schedule(step);
}

function loadSource(file_id) {
  // Hide the previously active group; show or load the new one.
  if (active) active.visible = false;
  partByName = new Map();
  requestRender();
  if (loaded.has(file_id)) {
    active = loaded.get(file_id);
    active.visible = true;
    indexParts(active);
    const upRot = window.IFU_VIEWER?.getActiveUpAxis?.();
    if (upRot) applyUpAxisOverride(upRot); else frame(active);
    return;
  }
  // Static sources have their GLB baked into the page.  Dynamic
  // (Onshape-imported) sources don't -- fall back to /api/glb/<id>
  // which meshes the server-side shape on demand.
  const bakedB64 = GLB_B64[file_id];
  const _hookGroup = (grp) => {
    // Onshape-style materials: low metalness for most parts so they
    // read like neutral CAD plastic / aluminium-equivalent surfaces.
    // The IBL environment provides ambient + reflections so we don't
    // need high roughness to hide flat shading.  EnvMapIntensity dials
    // how much the environment shows up on each material.
    // First pass: just the materials.  EdgesGeometry is expensive
    // (~1-3 ms per mesh) and on Presto-class assemblies (~700 meshes)
    // that adds 1-2 s of blocking work BEFORE the user sees anything.
    // Apply materials synchronously so the scene becomes interactive
    // immediately, then build edges in idle chunks below.
    const meshes = [];
    grp.traverse(obj => {
      if (obj.isMesh) {
        const baseColor = _bodyColorForObject(obj);
        obj.material = new THREE.MeshStandardMaterial({
          color: baseColor,
          metalness: 0.05,
          roughness: 0.72,
          envMapIntensity: 1.1,
          transparent: false,
          side: THREE.DoubleSide,
          polygonOffset: true,
          polygonOffsetFactor: 1,
          polygonOffsetUnits: 1,
        });
        obj.castShadow = true;
        obj.receiveShadow = true;
        obj.userData.baseColor = baseColor;
        meshes.push(obj);
      }
    });
    loaded.set(file_id, grp);
    scene.add(grp);
    active = grp;
    indexParts(grp);
    const upRot = window.IFU_VIEWER?.getActiveUpAxis?.();
    if (upRot) applyUpAxisOverride(upRot); else frame(grp);

    // Second pass (deferred): build feature edges in idle slices so
    // they appear progressively without blocking the first paint.
    // 32 meshes per slice keeps each tick under ~5 ms on a Presto-
    // sized assembly.  requestIdleCallback in supporting browsers;
    // setTimeout fallback elsewhere.
    _buildEdgesInIdleChunks(meshes, file_id);
  };

  const _loadFromB64 = (b64) => {
    const url = 'data:model/gltf-binary;base64,' + b64;
    const loader = new GLTFLoader();
    loader.load(url, (gltf) => _hookGroup(gltf.scene), undefined, (err) => {
      console.error('GLB load failed', err);
      readout.textContent = '(GLB load failed - see console)';
    });
  };

  if (bakedB64) { _loadFromB64(bakedB64); return; }

  // No baked mesh -- ask the server to generate one.  This can take
  // 5-30 seconds depending on assembly size, so show progress.
  readout.textContent = 'meshing ' + file_id + ' ...';
  fetch(API_BASE + '/api/glb/' + encodeURIComponent(file_id))
    .then(r => {
      if (!r.ok) {
        return r.json().then(j => {
          throw new Error(j.error || ('HTTP ' + r.status));
        });
      }
      return r.json();
    })
    .then(data => {
      if (!data.b64) throw new Error('no GLB returned');
      readout.textContent = `${file_id} : ${data.parts} parts, ${data.kb} KB`;
      _loadFromB64(data.b64);
    })
    .catch(err => {
      console.error('GLB fetch failed', err);
      readout.textContent = '(no 3D mesh: ' + (err.message || err) + ')';
    });
}

// ---- Configuration panel (3D overlay) ---------------------------------
// Shows the active source's Onshape configuration parameters as a small
// floating panel anchored to the 3D viewport.  Changing any value
// fires /api/sources/<id>/reconfigure, which re-translates the STEP
// and replaces the in-memory shape; we then evict the local GLB cache
// and reload, so the 3D pane updates in place.

const _cfgPanel  = document.getElementById('cfg-panel');
const _cfgBody   = document.getElementById('cfg-body');
const _cfgStatus = document.getElementById('cfg-status');
const _cfgHdr    = document.getElementById('cfg-header');
const _cfgColl   = document.getElementById('cfg-collapse');
let   _cfgInputs = {};          // parameter_id -> <input|select>
let   _cfgCurrentSourceId = null;  // last source we loaded into the panel
let   _cfgReloadTimer = null;
let   _cfgCollapsed = localStorage.getItem('ifu:cfg_collapsed') === '1';

function _cfgSetCollapsed(yes) {
  _cfgCollapsed = !!yes;
  _cfgBody.style.display   = _cfgCollapsed ? 'none' : 'block';
  _cfgStatus.style.display = _cfgCollapsed ? 'none' : 'block';
  _cfgColl.textContent     = _cfgCollapsed ? '+' : '−';
  localStorage.setItem('ifu:cfg_collapsed', _cfgCollapsed ? '1' : '0');
}
if (_cfgColl) _cfgColl.addEventListener('click', () =>
  _cfgSetCollapsed(!_cfgCollapsed));
if (_cfgHdr) _cfgHdr.addEventListener('dblclick', () =>
  _cfgSetCollapsed(!_cfgCollapsed));
_cfgSetCollapsed(_cfgCollapsed);

async function _cfgLoadForSource(sourceId) {
  if (!sourceId || !_cfgPanel) { if (_cfgPanel) _cfgPanel.style.display = 'none'; return; }
  _cfgCurrentSourceId = sourceId;
  _cfgInputs = {};
  _cfgBody.innerHTML = '';
  _cfgStatus.textContent = 'loading parameters...';
  _cfgPanel.style.display = 'block';
  let cfg;
  try {
    const r = await fetch(API_BASE + '/api/sources/'
                            + encodeURIComponent(sourceId) + '/configuration');
    if (!r.ok) {
      // Unknown / non-Onshape source -- hide the panel quietly
      _cfgPanel.style.display = 'none';
      return;
    }
    cfg = await r.json();
  } catch (_e) {
    _cfgPanel.style.display = 'none';
    return;
  }
  if (!cfg.has_config || !(cfg.parameters || []).length) {
    _cfgPanel.style.display = 'none';
    return;
  }
  _cfgStatus.textContent =
      cfg.parameters.length + ' parameter'
    + (cfg.parameters.length === 1 ? '' : 's')
    + ' -- changes update the 3D in place';
  for (const p of cfg.parameters) {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;flex-direction:column;gap:3px;margin-bottom:8px;';
    const lab = document.createElement('label');
    lab.textContent = p.name || p.id || '(unnamed)';
    lab.style.cssText = 'font-weight:500;color:#3f3f46;';
    row.appendChild(lab);
    let input;
    if (p.type === 'enum' && (p.options || []).length) {
      input = document.createElement('select');
      input.style.cssText = 'width:100%;padding:4px 6px;border:1px solid #d4d4d8;'
                          + 'border-radius:3px;background:#fff;font-size:12px;';
      for (const o of p.options) {
        const opt = document.createElement('option');
        opt.value = o.value; opt.textContent = o.label;
        if (o.value === p.default) opt.selected = true;
        input.appendChild(opt);
      }
    } else if (p.type === 'boolean') {
      const wrap = document.createElement('label');
      wrap.style.cssText = 'display:flex;align-items:center;gap:6px;cursor:pointer;'
                         + 'font-size:12px;color:#52525b;';
      input = document.createElement('input');
      input.type = 'checkbox';
      if (p.default === true || p.default === 'true') input.checked = true;
      wrap.appendChild(input);
      const sp = document.createElement('span');
      sp.textContent = input.checked ? 'enabled' : 'disabled';
      wrap.appendChild(sp);
      input.addEventListener('change', () => {
        sp.textContent = input.checked ? 'enabled' : 'disabled';
      });
      // Normalise value semantics
      Object.defineProperty(input, 'value', {
        get() { return input.checked ? 'true' : 'false'; },
      });
      row.appendChild(wrap);
    } else {
      input = document.createElement('input');
      input.type = 'text';
      input.style.cssText = 'width:100%;padding:4px 6px;border:1px solid #d4d4d8;'
                          + 'border-radius:3px;background:#fff;font-size:12px;'
                          + 'box-sizing:border-box;';
      if (p.default != null) input.value = String(p.default);
      if (p.unit) input.placeholder = p.unit;
    }
    if (p.type !== 'boolean') row.appendChild(input);
    _cfgInputs[p.id] = input;
    input.addEventListener('change', () =>
      _cfgScheduleReconfigure(sourceId));
    _cfgBody.appendChild(row);
  }
  if (_cfgCollapsed) _cfgSetCollapsed(true);
}

function _cfgScheduleReconfigure(sourceId) {
  if (_cfgReloadTimer) clearTimeout(_cfgReloadTimer);
  // Small debounce in case the user is tabbing through several
  // controls -- collapse a burst into one re-translation.
  _cfgReloadTimer = setTimeout(() => _cfgApply(sourceId), 250);
}

async function _cfgApply(sourceId) {
  if (sourceId !== _cfgCurrentSourceId) return;
  const values = {};
  for (const [pid, el] of Object.entries(_cfgInputs)) {
    const v = el.value;
    if (v !== undefined && v !== null && v !== '') values[pid] = v;
  }
  _cfgStatus.textContent = 'reconfiguring (Onshape -> STEP -> 3D)...';
  // Disable inputs while re-translating
  for (const el of Object.values(_cfgInputs)) el.disabled = true;
  try {
    const r = await fetch(
      API_BASE + '/api/sources/' + encodeURIComponent(sourceId)
        + '/reconfigure',
      { method: 'POST',
         headers: { 'Content-Type': 'application/json' },
         body: JSON.stringify({ configuration: values }) });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error || ('HTTP ' + r.status));
    }
    await r.json();
    // Bust the local GLB cache for this source so the next loadSource()
    // pulls the freshly meshed geometry.
    if (loaded && loaded.has && loaded.has(sourceId)) {
      const grp = loaded.get(sourceId);
      if (grp && grp.parent) grp.parent.remove(grp);
      loaded.delete(sourceId);
    }
    if (window.GLB_B64) delete window.GLB_B64[sourceId];
    // Reload the 3D mesh in place
    if (typeof loadSource === 'function') loadSource(sourceId);
    _cfgStatus.textContent = '3D updated';
    setTimeout(() => {
      if (_cfgCurrentSourceId === sourceId) {
        _cfgStatus.textContent =
            'changes update the 3D in place';
      }
    }, 2000);
  } catch (e) {
    _cfgStatus.textContent = 'reconfigure failed: ' + (e.message || e);
    (window.IFU_UI?.toast || function(){})(
      'Reconfigure failed: ' + (e.message || e), 'error');
    (window._toggleServerLog || function(){})(true);
  } finally {
    for (const el of Object.values(_cfgInputs)) el.disabled = false;
  }
}

// Wire up: when the legacy editor's file selector changes, refresh
// the configuration panel against the new source.  Initial load
// after page boot is handled by a one-shot timer because the file
// selector is populated AFTER this script runs.
if (typeof fileSel !== 'undefined' && fileSel) {
  fileSel.addEventListener('change', () =>
    _cfgLoadForSource(fileSel.value));
  setTimeout(() => _cfgLoadForSource(fileSel.value), 250);
}

window.IFU_VIEWER.reloadConfig = (sid) =>
  _cfgLoadForSource(sid || _cfgCurrentSourceId);

function indexParts(grp) {
  partByName = new Map();
  grp.traverse(obj => {
    if (obj.isMesh && obj.name) {
      // node names from trimesh come back as the geometry name; keep both
      partByName.set(obj.name, obj);
    }
    // walk parents to capture node-level names too
    if (obj.userData && obj.userData.name) {
      partByName.set(obj.userData.name, obj);
    }
  });
  // also walk gltf scene children which carry node names
  grp.children.forEach(child => {
    if (child.name) partByName.set(child.name, child);
    child.traverse(o => { if (o.name) partByName.set(o.name, o); });
  });
  // Rebuild the explode part-node index (idx -> movable part_NNN node) and
  // cache base positions for the new active group.
  if (typeof _buildPartNodeIndex === 'function') _buildPartNodeIndex();
}

function frame(grp) {
  const bbox = new THREE.Box3().setFromObject(grp);
  const size = bbox.getSize(new THREE.Vector3());
  const center = bbox.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  controls.target.copy(center);
  // approach from the stored iso preset if available, else default
  let vd = window.IFU_VIEWER?.getActiveViewDir?.() || [-0.5, -1.0, 0.7];
  const dir = new THREE.Vector3(vd[0], vd[1], vd[2]).normalize();
  camera.position.copy(center).add(dir.multiplyScalar(maxDim * 2.2));
  if (camera.isOrthographicCamera) {
    // Project the bbox 8 corners to camera-local axes, then size the
    // ortho frustum to enclose them with a 10% pad.  This matches the
    // HLR projection's natural fit on the SAME view_dir so 2D and 3D
    // pane have equivalent zoom/extent.
    const cornersWorld = [
      new THREE.Vector3(bbox.min.x, bbox.min.y, bbox.min.z),
      new THREE.Vector3(bbox.min.x, bbox.min.y, bbox.max.z),
      new THREE.Vector3(bbox.min.x, bbox.max.y, bbox.min.z),
      new THREE.Vector3(bbox.min.x, bbox.max.y, bbox.max.z),
      new THREE.Vector3(bbox.max.x, bbox.min.y, bbox.min.z),
      new THREE.Vector3(bbox.max.x, bbox.min.y, bbox.max.z),
      new THREE.Vector3(bbox.max.x, bbox.max.y, bbox.min.z),
      new THREE.Vector3(bbox.max.x, bbox.max.y, bbox.max.z),
    ];
    // Make sure camera matrices are current before we use them
    camera.lookAt(center);
    camera.updateMatrixWorld();
    let minX = +Infinity, maxX = -Infinity, minY = +Infinity, maxY = -Infinity;
    for (const c of cornersWorld) {
      const local = c.clone().applyMatrix4(camera.matrixWorldInverse);
      if (local.x < minX) minX = local.x;
      if (local.x > maxX) maxX = local.x;
      if (local.y < minY) minY = local.y;
      if (local.y > maxY) maxY = local.y;
    }
    const padX = (maxX - minX) * 0.05;
    const padY = (maxY - minY) * 0.05;
    let left = minX - padX, right = maxX + padX;
    let top = maxY + padY, bottom = minY - padY;
    // Keep aspect ratio to the canvas so the model isn't stretched
    const r = canvas.getBoundingClientRect();
    const aspect = (r.width || 1) / (r.height || 1);
    const w = right - left;
    const h = top - bottom;
    if (w / h > aspect) {
      // wider than canvas: expand vertically
      const want_h = w / aspect;
      const extra = (want_h - h) / 2;
      top += extra; bottom -= extra;
    } else {
      const want_w = h * aspect;
      const extra = (want_w - w) / 2;
      left -= extra; right += extra;
    }
    camera.left = left;
    camera.right = right;
    camera.top = top;
    camera.bottom = bottom;
    camera.near = -maxDim * 10;
    camera.far = maxDim * 10;
  } else {
    camera.near = maxDim / 100;
    camera.far = maxDim * 20;
  }
  camera.updateProjectionMatrix();
  controls.update();
  requestRender();
}

function _partIdxOf(obj) {
  // walk up the chain looking for "part_NNN" - trimesh's GLB nests the
  // mesh inside a named node a level or two up
  let cur = obj;
  while (cur && cur !== active) {
    const m = cur.name && cur.name.match(/^part_(\d+)$/);
    if (m) return parseInt(m[1]);
    cur = cur.parent;
  }
  return null;
}

// Onshape-inspired soft pastel palette.  Stays in the low-saturation
// range so the assembly reads as "CAD" rather than "primary-coloured
// toy".  All entries should look fine on the gradient backdrop with
// SSAO darkening corners -- avoid anything that goes muddy when
// shaded.  The first entry is intentionally the steel-blue Onshape
// uses by default so single-part figures look familiar.
const _BODY_PALETTE = [
  0x9dbcda,  // steel blue (Onshape default)
  0xa6c8d6,  // sky
  0xb9c8ce,  // light grey-blue
  0xb5d2bd,  // mint
  0xc5d2a8,  // pale olive
  0xd6d199,  // pale sand
  0xd6b094,  // peach
  0xd5a3b3,  // rose
  0xc8a8cd,  // lavender
  0xa9b4d3,  // periwinkle
  0xa8cbc4,  // pale teal
  0xbac4b0,  // sage
];

function _hexHash(s) {
  // Cheap deterministic hash so unidentified parts (no idx) still
  // get a stable colour based on whatever name we DO have.
  let h = 5381;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

function _bodyColorForObject(obj) {
  // 1) Onshape import / trimesh GLB: "part_NNN" -> idx -> palette
  let cur = obj;
  let nameStack = [];
  while (cur) {
    if (cur.name) nameStack.push(cur.name);
    const m = cur.name && cur.name.match(/^part_(\d+)$/);
    if (m) return _BODY_PALETTE[parseInt(m[1]) % _BODY_PALETTE.length];
    cur = cur.parent;
  }
  // 2) Fall back to hashing the deepest name we found
  const key = nameStack.join('|') || (obj.uuid || 'unknown');
  return _BODY_PALETTE[_hexHash(key) % _BODY_PALETTE.length];
}

function applyHighlights3D(set) {
  if (!active) return;
  const any = set && set.size > 0;
  active.traverse(o => {
    if (!o.isMesh) return;
    const idx = _partIdxOf(o);
    const hit = any && idx != null && set.has(idx);
    // Restore the part's PALETTE colour when not selected, instead of
    // the old hardcoded grey -- otherwise clearing a selection makes
    // every part wash to the same shade.  userData.baseColor was set
    // at material-creation time in _hookGroup().
    const baseColor = (o.userData && o.userData.baseColor != null)
                       ? o.userData.baseColor
                       : _bodyColorForObject(o);
    o.material.color.setHex(hit ? 0x00836a : baseColor);
    if (any && !hit) {
      o.material.opacity = 0.18;
      o.material.transparent = true;
      o.material.depthWrite = false;
    } else {
      o.material.opacity = 1.0;
      o.material.transparent = false;
      o.material.depthWrite = true;
    }
  });
  requestRender();
}

function snapToPresetView() {
  if (!active) return;
  frame(active);
}

// Up-axis override: rotate the loaded group so the user-picked axis lands
// on world Z.  The rotation comes from the same {axis, angle} table the
// classic script uses; the Python side reads the same tuple from SOURCES.
function applyUpAxisOverride(rot) {
  if (!active || !rot) return;
  const axis = new THREE.Vector3(rot.axis[0], rot.axis[1], rot.axis[2])
    .normalize();
  const q = new THREE.Quaternion()
    .setFromAxisAngle(axis, (rot.angle || 0) * Math.PI / 180);
  active.setRotationFromQuaternion(q);
  frame(active);
  requestRender();
}

// Replaces the old toggle button: the classic-script segmented control
// drives layout, and just tells us whether the WebGL pane is visible.
function set3DActive(on) {
  if (on) {
    init();           // idempotent
    // CSS already showed the canvas; resize after the next reflow so the
    // renderer matches the new pane width (especially when entering Split).
    requestAnimationFrame(() => {
      resize();
      const fid = window.IFU_VIEWER.getActiveFileId();
      loadSource(fid);
    });
  }
  // When off, no extra work needed -- CSS hides the canvas; we keep the
  // scene loaded so re-entering doesn't pay the GLB-parse cost again.
}

document.getElementById('btn-lock-view').addEventListener('click', () => {
  const d = camera.position.clone().sub(controls.target).normalize();
  const tup = `(${d.x.toFixed(3)}, ${d.y.toFixed(3)}, ${d.z.toFixed(3)})`;
  navigator.clipboard?.writeText(tup);
  readout.textContent = `copied ${tup}`;
  setTimeout(updateReadout, 1500);
});

document.getElementById('btn-reset-3d').addEventListener('click', () => {
  if (active) frame(active);
});

// Sync with the file picker: switching source while 3D is on screen swaps GLB.
window.IFU_VIEWER.onFileChange(() => {
  if (is3DVisible()) loadSource(window.IFU_VIEWER.getActiveFileId());
});
// Switching the 2D view preset snaps the 3D camera to that direction too,
// so the two panes stay roughly aligned in Split mode.
window.IFU_VIEWER.onViewChange(() => {
  if (is3DVisible()) snapToPresetView();
});

// Expose for the classic script's selection + orientation + layout handlers,
// plus debug/test access to the underlying three.js scene.
window.IFU_VIEWER.applyHighlights3D = applyHighlights3D;
window.IFU_VIEWER._scene = () => scene;
window.IFU_VIEWER._camera = () => camera;
window.IFU_VIEWER._renderer = () => renderer;
window.IFU_VIEWER._active = () => active;
window.IFU_VIEWER.applyUpAxisOverride = (rot) => {
  applyUpAxisOverride(rot);
};
window.IFU_VIEWER.set3DActive = set3DActive;
// Trigger a resize: used by the splitter drag handler so the 3D
// canvas / composer / ortho frustum keep up with the canvas's new
// bounding rect mid-drag.
window.IFU_VIEWER.resize3D = () => {
  if (typeof resize === 'function') resize();
};
window.IFU_VIEWER.getCurrentViewDir = () => {
  if (!camera || !controls) return null;
  const d = camera.position.clone().sub(controls.target).normalize();
  return [d.x, d.y, d.z];
};

// Per-part colour info for tests + tooling.  Returns an array of
// objects with keys idx + color_hex for every mesh in the active group.
window.IFU_VIEWER.getActivePartColors = () => {
  if (!active) return null;
  const out = [];
  active.traverse(o => {
    if (!o.isMesh) return;
    const idx = _partIdxOf(o);
    if (idx == null) return;
    const c = o.material && o.material.color;
    out.push({
      idx,
      color_hex: c ? '#' + c.getHexString() : null,
      base_hex: (o.userData && o.userData.baseColor != null)
                  ? '#' + o.userData.baseColor.toString(16).padStart(6, '0')
                  : null,
    });
  });
  return out;
};

window.IFU_VIEWER.getBodyPalette = () => _BODY_PALETTE.slice();

// Debug: dump the 3D highlight state so a capture can be compared
// against the 2D SVG.  For every mesh: its part idx, whether it's
// currently highlighted (teal), and its world-space centroid.  This is
// what lets us tell "2D part N and 3D part N are the SAME solid" from
// "they disagree" -- the GLB and the HLR SVG both number parts by
// split_solids() order, so a disagreement means the two were built
// from different shape states.
window.IFU_VIEWER.debugDump3D = () => {
  if (!active) return null;
  const out = [];
  active.traverse(o => {
    if (!o.isMesh) return;
    const idx = _partIdxOf(o);
    if (idx == null) return;
    const c = o.material && o.material.color;
    const hl = c ? ('#' + c.getHexString()).toLowerCase() === '#00836a'
                 : false;
    let cen = null;
    try {
      const box = new THREE.Box3().setFromObject(o);
      const v = new THREE.Vector3(); box.getCenter(v);
      cen = [Math.round(v.x*10)/10, Math.round(v.y*10)/10,
             Math.round(v.z*10)/10];
    } catch (_e) {}
    out.push({ idx, highlighted: hl, centroid: cen });
  });
  return out;
};

// Renderer + scene state inspector -- used by tests + the future
// graphics-quality controls.  Returns null when the 3D viewer hasn't
// initialised yet.
window.IFU_VIEWER.getRendererState = () => {
  if (!renderer || !scene) return null;
  let hasShadowPlane = false;
  scene.traverse(o => {
    if (o.userData && o.userData._shadowPlane) hasShadowPlane = true;
  });
  return {
    toneMapping: renderer.toneMapping,
    outputColorSpace: renderer.outputColorSpace,
    toneMappingExposure: renderer.toneMappingExposure,
    shadowMapEnabled: !!(renderer.shadowMap && renderer.shadowMap.enabled),
    hasEnvironment: !!scene.environment,
    hasComposer: !!composer,
    hasSSAO: !!ssaoPass,
    hasShadowPlane,
  };
};

// Camera position + target as world-space tuples.  Used by the saved-views
// feature to capture and recall exact viewpoints (no view_dir conversion).
window.IFU_VIEWER.getCameraEyeTarget = () => {
  if (!camera || !controls) return null;
  return {
    eye:    [camera.position.x, camera.position.y, camera.position.z],
    target: [controls.target.x,  controls.target.y,  controls.target.z],
  };
};

// Manually advance the depth-click cycle.  The classic-side button uses
// this when the user wants the NEXT pixel-click to drill one layer deeper
// even though their mouse may have moved slightly.
window.IFU_VIEWER.advanceClickCycle = () => {
  _lastClickRayCycle++;
  console.log('[depth-click] next click will be layer', _lastClickRayCycle);
};

// Override the classic-side stub so the screenshot exporter can force a
// fresh render into the back-buffer immediately before reading pixels.
window.renderer3d_request_present = () => {
  if (renderer && scene && camera) {
    // preserveDrawingBuffer might not be on; render explicitly into the
    // visible canvas right before the screenshot reads it
    renderer.render(scene, camera);
  }
};

window.IFU_VIEWER.snapCameraTo = (eye, target) => {
  if (!camera || !controls) return;
  camera.position.set(eye[0], eye[1], eye[2]);
  controls.target.set(target[0], target[1], target[2]);
  camera.lookAt(controls.target);
  // Re-fit the ortho frustum to the new direction WITHOUT moving the
  // camera back to the framed default.  We just want the bounds redone.
  if (active && camera.isOrthographicCamera) {
    const bbox = new THREE.Box3().setFromObject(active);
    camera.updateMatrixWorld();
    let minX = +Infinity, maxX = -Infinity,
        minY = +Infinity, maxY = -Infinity;
    const cs = [bbox.min, bbox.max];
    for (const cx of [cs[0].x, cs[1].x])
      for (const cy of [cs[0].y, cs[1].y])
        for (const cz of [cs[0].z, cs[1].z]) {
          const p = new THREE.Vector3(cx, cy, cz)
            .applyMatrix4(camera.matrixWorldInverse);
          if (p.x < minX) minX = p.x;
          if (p.x > maxX) maxX = p.x;
          if (p.y < minY) minY = p.y;
          if (p.y > maxY) maxY = p.y;
        }
    const padX = (maxX - minX) * 0.05;
    const padY = (maxY - minY) * 0.05;
    let l = minX - padX, r = maxX + padX,
        t = maxY + padY, bm = minY - padY;
    const rect = canvas.getBoundingClientRect();
    const aspect = (rect.width || 1) / (rect.height || 1);
    const w = r - l, h = t - bm;
    if (w / h > aspect) {
      const wantH = w / aspect, extra = (wantH - h) / 2;
      t += extra; bm -= extra;
    } else {
      const wantW = h * aspect, extra = (wantW - w) / 2;
      l -= extra; r += extra;
    }
    camera.left = l; camera.right = r;
    camera.top  = t; camera.bottom = bm;
    camera.updateProjectionMatrix();
  }
  controls.update();
  requestRender();
};

// Per-part 3D styling: colour, opacity per part_idx.  Each idx maps to an
// optional override; meshes with no entry stay at the default.
window.IFU_VIEWER.applyPartStyles3D = (stylesByIdx) => {
  if (!active) return;
  active.traverse(o => {
    if (!o.isMesh) return;
    const idx = _partIdxOf(o);
    if (idx == null) return;
    const st = stylesByIdx[idx];
    if (st) {
      const hex = (st.stroke || '#00836a').replace('#', '');
      const n = parseInt(hex, 16);
      o.material.color.setHex(isNaN(n) ? 0x00836a : n);
      o.material.opacity = (st.opacity != null) ? st.opacity : 1.0;
      o.material.transparent = (o.material.opacity < 1.0);
    } else {
      o.material.color.setHex(0xe8e8ea);
      o.material.opacity = 1.0;
      o.material.transparent = false;
    }
  });
  requestRender();
};

// ============================================================================
// Annotation engine: exploded views + 3D-aligned arrows + line-style preset.
// All state here is per-source and feeds the /api/render POST body so the
// server's 2D line-art reflects exactly what the user set up in 3D.
//
// Frames: an explode offset is read as (node.position - basePos) in the
// `active` group's LOCAL frame, which IS the model frame the server explodes
// in (server applies offsets pre-up_axis-rotation).  Arrows are converted to
// model-frame at placement time via active.worldToLocal so they survive an
// up-axis override.  When `active` has no rotation, local == world.
// ============================================================================

let _annotMode = 'none';        // 'none' | 'explode' | 'arrow-straight' | 'arrow-rotation'
let _partNodeByIdx = new Map(); // idx -> top-most part_NNN Object3D
let _gizmo = null;              // TransformControls for manual explode drag
let _gizmoTarget = null;        // currently gizmo-attached part node
let _arrowDefs = [];            // model-frame arrow defs (sent to server)
let _arrowGroup = null;         // THREE.Group holding the 3D arrow previews
let _currentPresetId = null;    // line-style preset id for the render POST
let _pendingAnnotState = null;  // figure state awaiting the 3D source to load
const _ARROW_TEAL = 0x00836a;

// Re-apply a restored figure's explode/arrows once `active`'s part nodes
// exist (3D loads lazily, so restore can land before the model is ready).
function _applyPendingAnnot() {
  if (!_pendingAnnotState || !active) return;
  const st = _pendingAnnotState;
  // Consume the pending state BEFORE applying.  setExplodeOffsets() ->
  // _buildPartNodeIndex() re-calls _applyPendingAnnot() (so a figure
  // restore can wait for the 3D nodes to exist); without clearing first
  // that re-entry recurses forever -> "Maximum call stack size exceeded"
  // and the editor hangs on load.
  _pendingAnnotState = null;
  if (st.explode) setExplodeOffsets(st.explode);
  if (st.arrows) setArrows(st.arrows);
}

function _buildPartNodeIndex() {
  _partNodeByIdx = new Map();
  if (!active) return;
  active.traverse(o => {
    const m = o.name && o.name.match(/^part_(\d+)$/);
    if (m) {
      const idx = parseInt(m[1]);
      if (!_partNodeByIdx.has(idx)) _partNodeByIdx.set(idx, o);
    }
  });
  _partNodeByIdx.forEach(node => {
    if (!node.userData._basePos) node.userData._basePos = node.position.clone();
  });
  // A figure restore may be waiting for these nodes to exist.
  if (_pendingAnnotState && active) _applyPendingAnnot();
}

function _ensureArrowGroup() {
  if (!_arrowGroup) {
    _arrowGroup = new THREE.Group();
    _arrowGroup.name = '__annotation_arrows__';
    _arrowGroup.userData._helper = true;   // excluded from part picking/colour
    scene.add(_arrowGroup);
  }
  return _arrowGroup;
}

// ---- Explode ---------------------------------------------------------------

function _assemblyCenterLocal() {
  // bbox centre of `active` expressed in active-local coords (== model frame).
  const box = new THREE.Box3().setFromObject(active);
  const c = box.getCenter(new THREE.Vector3());
  return active.worldToLocal(c.clone());
}

// Auto-spread: every part is pushed radially away from the assembly centre by
// `factor` * its distance from centre (so outer parts travel further -- the
// familiar Onshape "explode" feel).  factor 0 == assembled.
function setExplodeFactor(factor) {
  if (!active) return;
  _buildPartNodeIndex();
  const centre = _assemblyCenterLocal();
  _partNodeByIdx.forEach(node => {
    const base = node.userData._basePos || node.position.clone();
    // part centre in active-local frame
    const pc = new THREE.Box3().setFromObject(node)
      .getCenter(new THREE.Vector3());
    const pcLocal = active.worldToLocal(pc.clone());
    const dir = pcLocal.sub(centre);
    if (dir.lengthSq() < 1e-6) dir.set(0, 0, 1);
    node.position.copy(base.clone().add(dir.multiplyScalar(factor)));
  });
  requestRender();
}

function clearExplode() {
  if (!active) return;
  _buildPartNodeIndex();
  _partNodeByIdx.forEach(node => {
    if (node.userData._basePos) node.position.copy(node.userData._basePos);
  });
  _detachGizmo();
  requestRender();
}

// Manual per-part nudge via a translate gizmo.  Attaching the gizmo to a part
// lets the user drag it along X/Y/Z; the offset is read back from the node's
// position relative to its cached base.
function _ensureGizmo() {
  if (_gizmo) return _gizmo;
  _gizmo = new TransformControls(camera, renderer.domElement);
  _gizmo.setMode('translate');
  _gizmo.setSize(0.8);
  // Freeze orbit while dragging the gizmo, and keep rendering.
  _gizmo.addEventListener('dragging-changed', (e) => {
    controls.enabled = !e.value;
  });
  _gizmo.addEventListener('change', requestRender);
  _gizmo.addEventListener('objectChange', requestRender);
  // three r160: TransformControls is an Object3D and is added to the scene.
  const helper = _gizmo.getHelper ? _gizmo.getHelper() : _gizmo;
  helper.userData._helper = true;
  scene.add(helper);
  return _gizmo;
}

function attachExplodeGizmo(idx) {
  if (!active) return;
  _buildPartNodeIndex();
  const node = _partNodeByIdx.get(idx);
  if (!node) return;
  if (!node.userData._basePos) node.userData._basePos = node.position.clone();
  _ensureGizmo().attach(node);
  _gizmoTarget = node;
  requestRender();
}

function _detachGizmo() {
  if (_gizmo) { _gizmo.detach(); _gizmoTarget = null; requestRender(); }
}

// Read the current explode as {idx: [dx,dy,dz]} in model frame.
function getExplodeOffsets() {
  const out = {};
  _partNodeByIdx.forEach((node, idx) => {
    const base = node.userData._basePos;
    if (!base) return;
    const dx = node.position.x - base.x;
    const dy = node.position.y - base.y;
    const dz = node.position.z - base.z;
    if (Math.hypot(dx, dy, dz) > 1e-6) out[idx] = [dx, dy, dz];
  });
  return out;
}

// Restore an explode from saved figure state ({idx:[dx,dy,dz]} model frame).
function setExplodeOffsets(offsets) {
  if (!active || !offsets) return;
  _buildPartNodeIndex();
  _partNodeByIdx.forEach(node => {
    if (node.userData._basePos) node.position.copy(node.userData._basePos);
  });
  for (const [k, off] of Object.entries(offsets)) {
    const node = _partNodeByIdx.get(parseInt(k));
    if (node && node.userData._basePos && off) {
      node.position.set(
        node.userData._basePos.x + off[0],
        node.userData._basePos.y + off[1],
        node.userData._basePos.z + off[2]);
    }
  }
  requestRender();
}

// ---- Arrows ----------------------------------------------------------------

function _snapToWorldAxis(v) {
  // Snap a world-space direction to the nearest signed major axis.
  const ax = Math.abs(v.x), ay = Math.abs(v.y), az = Math.abs(v.z);
  if (ax >= ay && ax >= az) return new THREE.Vector3(Math.sign(v.x) || 1, 0, 0);
  if (ay >= ax && ay >= az) return new THREE.Vector3(0, Math.sign(v.y) || 1, 0);
  return new THREE.Vector3(0, 0, Math.sign(v.z) || 1);
}

function _modelLen() {
  const box = new THREE.Box3().setFromObject(active);
  const s = box.getSize(new THREE.Vector3());
  return Math.max(s.x, s.y, s.z) || 100;
}

// Build a straight-arrow preview (shaft + head) in WORLD space.
function _buildStraightArrowMesh(anchorW, dirW, length) {
  const g = new THREE.Group();
  g.userData._helper = true;
  const mat = new THREE.MeshStandardMaterial({
    color: _ARROW_TEAL, metalness: 0.0, roughness: 0.6,
    emissive: _ARROW_TEAL, emissiveIntensity: 0.25,
  });
  const shaftR = Math.max(0.6, length * 0.018);
  const headLen = Math.max(4, length * 0.22);
  const headR = shaftR * 3.0;
  const shaftLen = Math.max(0.001, length - headLen);
  const shaft = new THREE.Mesh(
    new THREE.CylinderGeometry(shaftR, shaftR, shaftLen, 16), mat);
  shaft.position.y = shaftLen / 2;
  const head = new THREE.Mesh(
    new THREE.ConeGeometry(headR, headLen, 20), mat);
  head.position.y = shaftLen + headLen / 2;
  g.add(shaft); g.add(head);
  // orient +Y -> dirW, place at anchor
  const q = new THREE.Quaternion().setFromUnitVectors(
    new THREE.Vector3(0, 1, 0), dirW.clone().normalize());
  g.quaternion.copy(q);
  g.position.copy(anchorW);
  return g;
}

// Build a rotation-arrow preview (torus arc + head) in WORLD space.
function _buildRotationArrowMesh(centreW, axisW, radius, sweepDeg) {
  const g = new THREE.Group();
  g.userData._helper = true;
  const mat = new THREE.MeshStandardMaterial({
    color: _ARROW_TEAL, metalness: 0.0, roughness: 0.6,
    emissive: _ARROW_TEAL, emissiveIntensity: 0.25,
  });
  const tubeR = Math.max(0.6, radius * 0.05);
  const sweep = THREE.MathUtils.degToRad(sweepDeg);
  const torus = new THREE.Mesh(
    new THREE.TorusGeometry(radius, tubeR, 12, 48, sweep), mat);
  // arrowhead at the sweep end, tangent to the arc (torus lies in local XY,
  // starts at +X; end point at angle=sweep)
  const headLen = Math.max(4, radius * 0.4);
  const headR = tubeR * 3.0;
  const head = new THREE.Mesh(new THREE.ConeGeometry(headR, headLen, 16), mat);
  const endX = radius * Math.cos(sweep), endY = radius * Math.sin(sweep);
  head.position.set(endX, endY, 0);
  // cone +Y -> tangent (-sin, cos) at the end
  const tan = new THREE.Vector3(-Math.sin(sweep), Math.cos(sweep), 0);
  head.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), tan);
  g.add(torus); g.add(head);
  // orient local +Z -> axisW, place at centre
  const q = new THREE.Quaternion().setFromUnitVectors(
    new THREE.Vector3(0, 0, 1), axisW.clone().normalize());
  g.quaternion.copy(q);
  g.position.copy(centreW);
  return g;
}

// Convert a world point/vector into active-local (model) frame.
function _worldPointToModel(pW) { return active.worldToLocal(pW.clone()); }
function _worldDirToModel(dW) {
  const inv = active.getWorldQuaternion(new THREE.Quaternion()).invert();
  return dW.clone().applyQuaternion(inv).normalize();
}

// Place an arrow from a raycast hit (called by handleCanvasClick in arrow mode).
function _placeArrowFromHit(hit) {
  const anchorW = hit.point.clone();
  const normalW = hit.face
    ? hit.face.normal.clone().transformDirection(hit.object.matrixWorld)
    : new THREE.Vector3(0, 0, 1);
  const len = _modelLen() * 0.28;
  let def, mesh;
  if (_annotMode === 'arrow-straight') {
    const dirW = _snapToWorldAxis(normalW);
    mesh = _buildStraightArrowMesh(anchorW, dirW, len);
    def = {
      type: 'straight',
      anchor: _worldPointToModel(anchorW).toArray(),
      dir: _worldDirToModel(dirW).toArray(),
      length: len,
    };
  } else {
    const axisW = _snapToWorldAxis(normalW);
    const radius = len * 0.5;
    mesh = _buildRotationArrowMesh(anchorW, axisW, radius, 270);
    def = {
      type: 'rotation',
      center: _worldPointToModel(anchorW).toArray(),
      axis: _worldDirToModel(axisW).toArray(),
      radius: radius,
      sweep: 270,
    };
  }
  def._id = 'arrow_' + (_arrowDefs.length + 1) + '_' + Math.floor(performance.now());
  mesh.userData._arrowId = def._id;
  _ensureArrowGroup().add(mesh);
  _arrowDefs.push(def);
  requestRender();
  window.dispatchEvent(new CustomEvent('ifu:arrows-changed'));
}

function clearArrows() {
  _arrowDefs = [];
  if (_arrowGroup) {
    while (_arrowGroup.children.length) _arrowGroup.remove(_arrowGroup.children[0]);
  }
  requestRender();
  window.dispatchEvent(new CustomEvent('ifu:arrows-changed'));
}

function removeArrow(id) {
  _arrowDefs = _arrowDefs.filter(a => a._id !== id);
  if (_arrowGroup) {
    const m = _arrowGroup.children.find(c => c.userData._arrowId === id);
    if (m) _arrowGroup.remove(m);
  }
  requestRender();
  window.dispatchEvent(new CustomEvent('ifu:arrows-changed'));
}

// Arrows sent to the server are stripped of preview-only keys.
function getArrows() {
  return _arrowDefs.map(a => {
    const { _id, ...rest } = a;
    return rest;
  });
}

function setArrows(defs) {
  clearArrows();
  if (!active || !Array.isArray(defs)) return;
  for (const d of defs) {
    // rebuild preview in WORLD space from model-frame def
    let mesh;
    if (d.type === 'rotation') {
      const cW = active.localToWorld(new THREE.Vector3().fromArray(d.center));
      const aW = new THREE.Vector3().fromArray(d.axis)
        .applyQuaternion(active.getWorldQuaternion(new THREE.Quaternion()));
      mesh = _buildRotationArrowMesh(cW, aW, d.radius || 20, d.sweep || 270);
    } else {
      const aW = active.localToWorld(new THREE.Vector3().fromArray(d.anchor));
      const dW = new THREE.Vector3().fromArray(d.dir)
        .applyQuaternion(active.getWorldQuaternion(new THREE.Quaternion()));
      mesh = _buildStraightArrowMesh(aW, dW, d.length || 50);
    }
    const def = { ...d, _id: d._id || ('arrow_' + Math.random().toString(36).slice(2)) };
    mesh.userData._arrowId = def._id;
    _ensureArrowGroup().add(mesh);
    _arrowDefs.push(def);
  }
  requestRender();
  window.dispatchEvent(new CustomEvent('ifu:arrows-changed'));
}

// ---- Mode + preset ---------------------------------------------------------

function setAnnotMode(mode) {
  _annotMode = mode || 'none';
  if (_annotMode !== 'explode') _detachGizmo();
  canvas.style.cursor = (_annotMode.startsWith('arrow')) ? 'crosshair' : '';
  window.dispatchEvent(new CustomEvent('ifu:annot-mode', { detail: _annotMode }));
}
function getAnnotMode() { return _annotMode; }
function setPresetId(id) { _currentPresetId = id || null; }
function getPresetId() { return _currentPresetId; }

// Decorate the render POST body with the current annotation state.  Called by
// generateLiveSVG / generateLiveSVGForCamera before they fetch.
function _decorateRenderBody(body) {
  const exp = getExplodeOffsets();
  if (Object.keys(exp).length) body.explode = exp;
  const arr = getArrows();
  if (arr.length) body.arrows = arr;
  if (_currentPresetId) body.preset_id = _currentPresetId;
  return body;
}

// Expose the annotation API to the classic script / settings UI.
Object.assign(window.IFU_VIEWER, {
  setExplodeFactor, clearExplode, attachExplodeGizmo,
  getExplodeOffsets, setExplodeOffsets,
  setAnnotMode, getAnnotMode,
  clearArrows, removeArrow, getArrows, setArrows,
  setPresetId, getPresetId,
  getAnnotationState: () => ({
    explode: getExplodeOffsets(),
    arrows: getArrows(),
    preset_id: _currentPresetId,
  }),
  restoreAnnotationState: (st) => {
    if (!st) return;
    // Preset id is independent of the 3D model -- apply immediately so the
    // next render uses it.  Explode/arrows need `active`'s part nodes, which
    // may not be loaded yet (3D is lazy) -- stash them and apply on the next
    // part-index build (see _buildPartNodeIndex), and also try now.
    setPresetId(st.preset_id || null);
    _pendingAnnotState = { explode: st.explode || {}, arrows: st.arrows || [] };
    _applyPendingAnnot();
  },
});

// --- Generate-from-3D: button in the 3D toolbar -----------------------------
// Calls the local server's /api/render with the current camera direction,
// then injects the returned SVG as a special "live" view in the 2D pane.
// If the server isn't running, the button greys out with a helpful tooltip.
const btnGen = document.getElementById('btn-generate');

// Server URL: same-origin when viewer is loaded via http://, else hop to
// the standard local server.  Works whether the user opened
// http://localhost:5000/ or a file:// build.  Promoted to window so the
// classic-script silhouette fetcher (in the other <script> block) can
// reach it too.
// Same resolution as the classic script: window.IFU_API_BASE (set by a
// config.js for a Vercel-front-end / Render-API split) wins; otherwise
// same-origin for http(s), localhost for file://.
const API_BASE = (typeof window !== 'undefined' && window.IFU_API_BASE)
  ? window.IFU_API_BASE
  : ((location.protocol === 'http:' || location.protocol === 'https:')
      ? ''
      : 'http://localhost:5000');
window.API_BASE = API_BASE;

async function probeServer() {
  try {
    const r = await fetch(API_BASE + '/api/healthz', { cache: 'no-store' });
    if (!r.ok) throw new Error('healthz ' + r.status);
    const data = await r.json();
    return data && data.ok;
  } catch (_e) {
    return false;
  }
}

async function generateLiveSVG() {
  if (!camera || !controls) return;
  // Generate now CAPTURES the current angle as a NEW variant (preserving
  // the existing figure) instead of overwriting it.  The fork carries the
  // live camera + current highlighting, then navigates to the new figure
  // which auto-renders on open.  Falls back to the in-place render below
  // when there's no view/project context to fork into (legacy editor).
  if (window._forkNewAngleVariant) {
    try {
      const forked = await window._forkNewAngleVariant();
      if (forked) return;
    } catch (_e) { /* fall through to in-place render */ }
  }
  const fid = window.IFU_VIEWER.getActiveFileId();
  // User explicitly asked for a fresh angle -- drop any previous
  // highlights so they don't carry across cameras (a body that was
  // visible at one angle may not be at another, and the cached
  // footprints belong to the old projection anyway).
  if (typeof clearHighlights === 'function') clearHighlights();
  // Send the camera as {eye, target} -- two explicit world-space points.
  // Unambiguous: HLR sets up its projection from the exact same camera
  // OrbitControls is currently driving.  No view_dir sign convention,
  // no separate focal arg, no chance of meaning the opposite side.
  const eye    = [camera.position.x, camera.position.y, camera.position.z];
  const target = [controls.target.x,  controls.target.y,  controls.target.z];
  // Send the current Up: override so the server rotates the cached shape
  // the same way the 3D view did before running HLR -- otherwise the SVG
  // comes back in the model's native (unrotated) orientation.
  const upRot = window.IFU_VIEWER.getActiveUpAxis?.();
  const body = { file_id: fid, eye, target };
  if (upRot && upRot.angle && upRot.angle !== 0) {
    body.up_axis = { axis: upRot.axis, angle: upRot.angle };
  }
  _decorateRenderBody(body);   // explode + arrows + preset

  const orig = btnGen.innerHTML;
  btnGen.disabled = true;
  btnGen.innerHTML = '&#8987; rendering ...';
  // Freeze the orbit so the 3D pane can't drift away from the angle the
  // server is rendering -- otherwise the user sees a "matching" 2D that
  // doesn't match what the 3D is now showing.
  controls.enabled = false;

  try {
    const r = await fetch(API_BASE + '/api/render', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ error: 'HTTP ' + r.status }));
      throw new Error(err.error || 'HTTP ' + r.status);
    }
    const svgText = await r.text();
    const elapsed = r.headers.get('X-Render-Seconds') || '?';
    const breakdown = r.headers.get('X-Render-Breakdown') || '';
    // injectLiveSVG stores a view_dir for the Live entry; derive it from
    // the eye/target we just sent so the dropdown's Live preset still
    // round-trips for snap-back.
    const _vdx = eye[0] - target[0], _vdy = eye[1] - target[1], _vdz = eye[2] - target[2];
    const _vdL = Math.hypot(_vdx, _vdy, _vdz) || 1;
    const view_dir = [_vdx / _vdL, _vdy / _vdL, _vdz / _vdL];
    // Cache the camera context for the silhouette endpoint (it has to
    // project into the same (u,v) space as the SVG we just received).
    window.IFU_VIEWER._setLiveCamCtx?.(fid, {
      eye, target, up_axis: body.up_axis || null,
    });
    window.IFU_VIEWER.injectLiveSVG(fid, view_dir, svgText);
    // Auto-switch to Split so the new SVG appears on the left next to the 3D
    window.IFU_VIEWER.setLayout?.('split');
    const polysHeader = r.headers.get('X-Render-Polylines');
    const polys = polysHeader ? parseInt(polysHeader, 10) : null;
    if (polys === 0) {
      // The SVG was generated but contains no visible lines.  This is
      // the exact "nothing appeared" failure mode.  Surface it loudly
      // so the user knows it's not a UI bug.
      btnGen.innerHTML = '&#9888; 0 lines';
      btnGen.title =
          'HLR produced 0 polylines for this source/view.\n'
        + 'Common causes:\n'
        + '  - source loaded but has no solid bodies (only sketches/surfaces)\n'
        + '  - camera is inside the model or at a degenerate angle\n'
        + '  - mesh_defl is too coarse for very small geometry\n'
        + 'Open the Server log (header: log) to see what the backend did.';
      (window.IFU_UI?.toast || function(){})(
        'Render returned 0 lines -- check the server log', 'error');
      (window._toggleServerLog || function(){})(true);
    } else {
      btnGen.innerHTML =
        `&#10003; ${elapsed}s${polys != null ? ` (${polys} lines)` : ''}`;
    }
    if (breakdown) {
      readout.title = `last render: ${elapsed}s -- ${breakdown}`;
      console.log(`[generate] ${elapsed}s -- ${breakdown}`
                    + (polys != null ? ` -- ${polys} polylines` : ''));
    }
  } catch (e) {
    console.error('generate failed:', e);
    btnGen.innerHTML = '&#10007; ' + (e.message || 'render failed');
    (window.IFU_UI?.toast || function(){})(
      'Render failed: ' + (e.message || e), 'error');
    (window._toggleServerLog || function(){})(true);
  } finally {
    controls.enabled = true;
    setTimeout(() => { btnGen.disabled = false; btnGen.innerHTML = orig; }, 4000);
  }
}

btnGen.addEventListener('click', generateLiveSVG);

// Programmatic render entry point: same pipeline as the user-driven
// "generate 2D" button, but you supply eye/target/up_axis explicitly
// instead of reading them from the live three.js camera.  Used by
// EditorScreen's auto-render-on-open path so a new figure inside a
// View shows the View's drawing right away -- no manual click.
async function generateLiveSVGForCamera(camInfo) {
  if (!camInfo || !camInfo.eye || !camInfo.target) return;
  const fid = window.IFU_VIEWER.getActiveFileId();
  if (!fid) return;
  const eye    = [camInfo.eye[0], camInfo.eye[1], camInfo.eye[2]];
  const target = [camInfo.target[0], camInfo.target[1], camInfo.target[2]];
  const upRot = window.IFU_VIEWER.getActiveUpAxis?.();
  const body = { file_id: fid, eye, target };
  if (upRot && upRot.angle && upRot.angle !== 0) {
    body.up_axis = { axis: upRot.axis, angle: upRot.angle };
  }
  _decorateRenderBody(body);   // explode + arrows + preset
  // Visual feedback on the (button) so the user sees the render in
  // flight even though they didn't click it.
  const orig = btnGen ? btnGen.innerHTML : '';
  if (btnGen) { btnGen.disabled = true; btnGen.innerHTML = '&#8987; rendering...'; }
  try {
    const r = await fetch(API_BASE + '/api/render', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const svgText = await r.text();
    const _vdx = eye[0] - target[0], _vdy = eye[1] - target[1], _vdz = eye[2] - target[2];
    const _vdL = Math.hypot(_vdx, _vdy, _vdz) || 1;
    const view_dir = [_vdx / _vdL, _vdy / _vdL, _vdz / _vdL];
    window.IFU_VIEWER._setLiveCamCtx?.(fid, {
      eye, target, up_axis: body.up_axis || null,
    });
    window.IFU_VIEWER.injectLiveSVG(fid, view_dir, svgText);
    window.IFU_VIEWER.setLayout?.('split');
    if (btnGen) {
      const elapsed = r.headers.get('X-Render-Seconds') || '?';
      btnGen.innerHTML = `&#10003; ${elapsed}s`;
    }
  } catch (e) {
    console.warn('[auto-render] failed:', e?.message || e);
    if (btnGen) btnGen.innerHTML = '&#10007; ' + (e.message || 'render failed');
    (window.IFU_UI?.toast || function(){})(
      'Render failed: ' + (e.message || e), 'error');
  } finally {
    if (typeof hideCanvasLoading === 'function') hideCanvasLoading();
    setTimeout(() => {
      if (btnGen) { btnGen.disabled = false; btnGen.innerHTML = orig; }
    }, 3000);
  }
}
window.generateLiveSVGForCamera = generateLiveSVGForCamera;

// Decide whether the server is reachable at load time and grey the button
// out if not (file:// or stand-alone deployment).
probeServer().then((alive) => {
  if (!alive) {
    btnGen.classList.add('unavailable');
    btnGen.disabled = true;
    btnGen.title = "Local server not reachable. Start it with:\\n"
                   + "  python serve.py\\n"
                   + "then open http://localhost:5000";
  }
  // Footprint prefetch is NO LONGER fired here -- it's slow (~2 min
  // for siderail z-buffer raster, and click-anywhere now works via
  // the client-side convex-hull layer with no server roundtrip).
  // The shade-fill flow triggers the prefetch lazily when the user
  // toggles shade on, so we only pay the rasterization cost when
  // shading is actually needed.
});
