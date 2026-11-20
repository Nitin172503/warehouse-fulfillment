/*
 * Page-side client for the in-browser Python engine (engine-worker.js).
 * Only included in the static build; the web app checks for window.WarehouseEngine and
 * otherwise talks to the FastAPI server.
 */
(function () {
  const worker = new Worker("engine-worker.js");
  const pending = new Map(), listeners = [];
  let seq = 0, resolveReady, rejectReady;
  const ready = new Promise((res, rej) => { resolveReady = res; rejectReady = rej; });
  ready.catch(() => {});

  worker.onmessage = (e) => {
    const m = e.data;
    if (m.type === "progress") listeners.forEach((f) => f(m.text, false));
    else if (m.type === "ready") resolveReady(m);
    else if (m.type === "fatal") { listeners.forEach((f) => f(m.text, true)); rejectReady(new Error(m.text)); }
    else if (m.type === "reply") {
      const p = pending.get(m.id);
      pending.delete(m.id);
      if (!p) return;
      if (m.ok) p.resolve(m.data);
      else p.reject(Object.assign(new Error(m.detail), { status: m.status }));
    }
  };
  worker.onerror = (e) => { listeners.forEach((f) => f(e.message || "The simulation engine crashed.", true)); rejectReady(new Error(e.message)); };

  window.WarehouseEngine = {
    ready,
    onProgress(fn) { listeners.push(fn); },
    async request(path, opts = {}) {
      await ready;
      const id = ++seq;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        worker.postMessage({ type: "request", id, method: (opts.method || "GET").toUpperCase(), path, body: opts.body || null });
      });
    },
    save() { worker.postMessage({ type: "save" }); },
  };
  worker.postMessage({ type: "boot" });
})();
