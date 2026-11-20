// Smoke test for the in-browser engine (static site). Run: node tests/engine_check.cjs
const E = require(process.argv[2] || require("path").join(__dirname, "../src/warehouse/static/engine.js"));
const t0 = Date.now();
for (let d = 0; d < 10; d++) E.request("/simulation/step", { method: "POST", body: JSON.stringify({ ticks: 480 }) });
const m = E.request("/metrics");
console.log("10 days:", m.orders, "orders,", m.shipped, "shipped,", Date.now() - t0, "ms");
if (m.shipped / m.orders < 0.95) { console.log("FILL RATE TOO LOW"); process.exit(1); }
// invariants
for (const w of Object.values(E.sim.whs)) {
  const open = [...w.fleet.queue.flatMap(j => j.tasks), ...w.fleet.robots.filter(r => r.job).flatMap(r => r.job.tasks.filter(t => !t.done))];
  for (const loc of Object.keys(w.inventory.binSku)) {
    const exp = open.filter(t => t.location_id === loc).reduce((a, t) => a + t.qty, 0);
    if (w.inventory.rs(loc) !== exp || w.inventory.oh(loc) < 0 || w.ph(loc) < 0) { console.log("INVARIANT FAIL", w.warehouse_id, loc); process.exit(1); }
  }
}
for (const o of Object.values(E.sim.orders)) if (o.status === "shipped") {
  const picked = o.allocations.filter(a => !a.cancelled).reduce((a, x) => a + x.picked, 0), want = Object.values(o.demand()).reduce((a, b) => a + b, 0);
  if (picked !== want) { console.log("PICK MISMATCH", o.order_id); process.exit(1); }
}
const s = E.request("/dashboard/state"); console.log("state ok", s.warehouses.length, s.decisions.length, s.events.length);
const o = E.request("/orders", { method: "POST", body: JSON.stringify({ lines: [{ sku_id: "SKU-001", qty: 2 }], destination: [10, 10] }) });
console.log("order", o.order_id, o.status, o.warehouses);
console.log("forecast", JSON.stringify(E.request("/forecast/SKU-001?horizon=3&history=3")));
console.log("inv", JSON.stringify(E.request("/inventory/overview")[0]));
try { E.request("/orders", { method: "POST", body: JSON.stringify({ lines: [{ sku_id: "NOPE", qty: 1 }], destination: [0, 0] }) }); } catch (e) { console.log("422 ok:", e.status, e.message); }
// forecaster sanity vs naive on seasonal data
const R = new E.RidgeForecaster(); let y = []; let s2 = 1; const rnd = () => (s2 = (s2 * 16807) % 2147483647) / 2147483647;
for (let d = 0; d < 120; d++) { const lam = 20 * [1, 1.05, 1.1, 1.05, 1.25, 0.85, 0.7][d % 7] * (1 + d / 400); let k = 0, p = 1, L = Math.exp(-lam); do { k++; p *= rnd(); } while (p > L); y.push(k - 1); }
console.log("backtest", JSON.stringify(E.backtest(y, 14)));
