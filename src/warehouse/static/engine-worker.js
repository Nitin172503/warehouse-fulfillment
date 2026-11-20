/*
 * Web Worker that runs the real Python simulation (src/warehouse) in the browser via
 * Pyodide, for the static GitHub Pages build. It loads the package files listed in
 * py/manifest.json, creates a warehouse.service.Service, and answers requests with
 * Service.dispatch_json — the same code path FastAPI uses on the server.
 *
 * Progress is snapshotted (pickled) into IndexedDB so a refresh resumes where you were.
 */
const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/";
importScripts(PYODIDE + "pyodide.js");

let py = null, svc = null, version = null, dirty = false, saving = false;

/* ---------- IndexedDB (per-browser progress) ---------- */
const DB = "warehouse-sim", STORE = "snapshots", KEY = "current";
function db() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function idb(mode, fn) {
  const d = await db();
  return new Promise((resolve, reject) => {
    const req = fn(d.transaction(STORE, mode).objectStore(STORE));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
const loadSnapshot = () => idb("readonly", (s) => s.get(KEY));
const storeSnapshot = (v) => idb("readwrite", (s) => s.put(v, KEY));
const clearSnapshot = () => idb("readwrite", (s) => s.delete(KEY));

const progress = (text) => postMessage({ type: "progress", text });

async function boot() {
  try {
    progress("Downloading the Python runtime…");
    py = await loadPyodide({ indexURL: PYODIDE });
    progress("Loading NumPy…");
    await py.loadPackage("numpy");
    progress("Loading the simulation…");
    const manifest = await (await fetch("py/manifest.json", { cache: "no-cache" })).json();
    version = manifest.version;
    py.FS.mkdirTree("/lib/warehouse");
    await Promise.all(manifest.files.map(async (f) => {
      const res = await fetch(`py/warehouse/${f}?v=${version}`);
      if (!res.ok) throw new Error(`could not load ${f} (${res.status})`);
      py.FS.writeFile(`/lib/warehouse/${f}`, await res.text());
    }));
    py.runPython("import sys\nsys.path.insert(0, '/lib')\nfrom warehouse.service import Service");

    let restored = false;
    try {
      const snap = await loadSnapshot();
      if (snap && snap.version === version) {
        progress("Restoring your saved progress…");
        py.globals.set("_snap", snap.bytes);
        py.runPython("svc = Service.restore(_snap.to_py())\ndel _snap");
        restored = true;
      }
    } catch (e) {
      console.warn("Saved progress could not be restored; starting fresh.", e);
    }
    if (!restored) py.runPython("svc = Service()");
    svc = py.globals.get("svc");
    postMessage({ type: "ready", restored });
    setInterval(() => save(false), 5000);
  } catch (e) {
    postMessage({ type: "fatal", text: String(e && e.message || e) });
  }
}

async function save(force) {
  if (!svc || saving || (!dirty && !force)) return;
  saving = true; dirty = false;
  try {
    const b = svc.snapshot();
    const bytes = b.toJs();
    b.destroy();
    await storeSnapshot({ version, bytes, saved: Date.now() });
  } catch (e) {
    dirty = true;
    console.warn("Could not save progress", e);
  } finally {
    saving = false;
  }
}

onmessage = async (e) => {
  const m = e.data;
  if (m.type === "boot") return boot();
  if (m.type === "save") return save(true);
  if (m.type === "forget") { try { await clearSnapshot(); } catch {} return; }
  if (m.type === "request") {
    let out;
    try {
      out = JSON.parse(svc.dispatch_json(m.method, m.path, m.body));
    } catch (err) {
      out = { ok: false, status: 500, detail: String(err && err.message || err) };
    }
    if (m.method !== "GET") dirty = true;
    postMessage({ type: "reply", id: m.id, ...out });
  }
};
