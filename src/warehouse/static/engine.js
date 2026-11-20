/*
 * In-browser port of the warehouse simulation (the Python package in src/warehouse).
 * Used by the static GitHub Pages build so the web app runs with no server: every
 * visitor gets their own simulation. `WarehouseEngine.request(path, opts)` answers
 * the same routes as the FastAPI app, so the web app code is identical in both modes.
 *
 * Module map (mirrors the Python files): rng · events · layout · inventory · robots ·
 * vision · forecasting · network · agents · simulation · api
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.WarehouseEngine = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /* ============================== rng ============================== */
  class Rng {
    constructor(seed) { this.s = (seed >>> 0) || 1; }
    random() { // mulberry32
      let t = (this.s = (this.s + 0x6d2b79f5) >>> 0);
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    }
    integers(lo, hi) { return lo + Math.floor(this.random() * (hi - lo)); } // [lo, hi)
    uniform(a, b) { return a + this.random() * (b - a); }
    choice(arr) { return arr[Math.floor(this.random() * arr.length)]; }
    weighted(arr, p) { let r = this.random(), acc = 0; for (let i = 0; i < arr.length; i++) { acc += p[i]; if (r < acc) return arr[i]; } return arr[arr.length - 1]; }
    poisson(lam) {
      if (lam <= 0) return 0;
      if (lam > 30) { // normal approximation
        const u = 1 - this.random(), v = this.random();
        return Math.max(0, Math.round(lam + Math.sqrt(lam) * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v)));
      }
      const L = Math.exp(-lam); let k = 0, p = 1;
      do { k++; p *= this.random(); } while (p > L);
      return k - 1;
    }
    permutation(arr) { const a = arr.slice(); for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(this.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }
  }
  const crc = (s) => { let h = 2166136261; for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 16777619); return h >>> 0; };

  /* ============================== events ============================== */
  class EventBus {
    constructor(history = 10000) { this.tick = 0; this.log = []; this.history = history; this.counts = {}; this.subs = {}; }
    subscribe(kind, fn) { (this.subs[kind] ||= []).push(fn); }
    publish(kind, data = {}) {
      const ev = { tick: this.tick, kind, data };
      this.log.push(ev);
      if (this.log.length > this.history * 1.2) this.log.splice(0, this.log.length - this.history);
      this.counts[kind] = (this.counts[kind] || 0) + 1;
      for (const fn of [...(this.subs[kind] || []), ...(this.subs["*"] || [])]) fn(ev);
      return ev;
    }
    count(kind) { return this.counts[kind] || 0; }
  }

  /* ============================== models ============================== */
  const OrderStatus = { PENDING: "pending", ALLOCATED: "allocated", BACKORDERED: "backordered", SHIPPED: "shipped", CANCELLED: "cancelled" };
  class Order {
    constructor(order_id, lines, destination, created_tick) {
      Object.assign(this, { order_id, lines, destination, created_tick, status: OrderStatus.PENDING, allocations: [], shipped_tick: null, reroute_count: 0 });
    }
    demand() { const d = {}; for (const l of this.lines) d[l.sku_id] = (d[l.sku_id] || 0) + l.qty; return d; }
    unallocated() {
      const cov = {}; for (const a of this.allocations) if (!a.cancelled) cov[a.sku_id] = (cov[a.sku_id] || 0) + a.qty;
      const out = {}; for (const [s, q] of Object.entries(this.demand())) { const r = q - (cov[s] || 0); if (r > 0) out[s] = r; }
      return out;
    }
    isComplete() { return this.allocations.length > 0 && !Object.keys(this.unallocated()).length && this.allocations.every((a) => a.done || a.cancelled); }
    warehouses() { return [...new Set(this.allocations.filter((a) => !a.cancelled).map((a) => a.warehouse_id))].sort(); }
  }
  const newAllocation = (warehouse_id, sku_id, qty) => ({ warehouse_id, sku_id, qty, picked: 0, done: false, cancelled: false });

  /* ============================== layout ============================== */
  class Layout {
    static grid(nAisles = 4, aisleLength = 8) {
      const L = new Layout(); L.width = 3 * nAisles + 1; L.height = aisleLength + 2; L.blocked = new Set(); L.locations = {}; L.pickFaces = {}; L.dock = [0, 0];
      for (let x = 0; x < L.width; x++) {
        const side = x % 3; if (!side) continue;
        for (let y = 1; y <= aisleLength; y++) {
          const id = `${String.fromCharCode(65 + Math.floor(x / 3))}${side}-${String(y).padStart(2, "0")}`;
          L.blocked.add(x + "," + y); L.locations[id] = [x, y]; L.pickFaces[id] = side === 1 ? [x - 1, y] : [x + 1, y];
        }
      }
      return L;
    }
    passable([x, y]) { return x >= 0 && y >= 0 && x < this.width && y < this.height && !this.blocked.has(x + "," + y); }
    neighbors([x, y]) { return [[x + 1, y], [x - 1, y], [x, y + 1], [x, y - 1]].filter((c) => this.passable(c)); }
  }

  /* ============================== inventory ============================== */
  class InsufficientStock extends Error {}
  class Inventory {
    constructor(warehouse_id, bus) { this.warehouse_id = warehouse_id; this.bus = bus; this.binSku = {}; this.onHand = {}; this.reserved = {}; }
    assign(loc, sku) { if (this.binSku[loc] && this.binSku[loc] !== sku) throw new Error(`${loc} already holds ${this.binSku[loc]}`); this.binSku[loc] = sku; }
    locationsFor(sku) { return Object.keys(this.binSku).filter((l) => this.binSku[l] === sku); }
    skus() { return [...new Set(Object.values(this.binSku))].sort(); }
    oh(l) { return this.onHand[l] || 0; } rs(l) { return this.reserved[l] || 0; }
    availableAt(l) { return Math.max(0, this.oh(l) - this.rs(l)); }
    available(sku) { return this.locationsFor(sku).reduce((a, l) => a + this.availableAt(l), 0); }
    onHandTotal(sku) { return this.locationsFor(sku).reduce((a, l) => a + this.oh(l), 0); }
    receive(sku, loc, qty) { this.assign(loc, sku); this.onHand[loc] = this.oh(loc) + qty; }
    reserve(sku, qty) {
      if (qty <= 0) return [];
      if (this.available(sku) < qty) throw new InsufficientStock(`${this.warehouse_id}: ${sku} short`);
      const picks = []; let rem = qty;
      for (const l of this.locationsFor(sku).sort((a, b) => this.availableAt(b) - this.availableAt(a))) {
        const take = Math.min(rem, this.availableAt(l));
        if (take) { this.reserved[l] = this.rs(l) + take; picks.push([l, take]); rem -= take; }
        if (!rem) break;
      }
      return picks;
    }
    release(l, qty) { this.reserved[l] = Math.max(0, this.rs(l) - qty); }
    commitPick(l, reservedQty, pickedQty) { this.release(l, reservedQty); this.onHand[l] = Math.max(0, this.oh(l) - pickedQty); }
    adjust(l, counted, reason) {
      const delta = counted - this.oh(l);
      if (delta) { this.onHand[l] = counted; this.bus?.publish("inventory.adjusted", { warehouse_id: this.warehouse_id, location_id: l, sku_id: this.binSku[l], delta, reason }); }
      return delta;
    }
    snapshot() {
      return this.skus().map((s) => ({ sku_id: s, on_hand: this.onHandTotal(s), reserved: this.locationsFor(s).reduce((a, l) => a + this.rs(l), 0),
        available: this.available(s), locations: Object.fromEntries(this.locationsFor(s).map((l) => [l, this.oh(l)])) }));
    }
  }

  /* ============================== robots ============================== */
  const manhattan = (a, b) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]);
  function astar(layout, start, goal) {
    if (start[0] === goal[0] && start[1] === goal[1]) return [];
    if (!layout.passable(goal)) throw new Error("goal blocked");
    const key = (c) => c[0] + "," + c[1], gk = key(goal);
    const open = [[manhattan(start, goal), 0, start]], came = {}, cost = { [key(start)]: 0 };
    let seq = 0, found = false;
    while (open.length) {
      let bi = 0; for (let i = 1; i < open.length; i++) if (open[i][0] < open[bi][0] || (open[i][0] === open[bi][0] && open[i][1] < open[bi][1])) bi = i;
      const [, , cur] = open.splice(bi, 1)[0], ck = key(cur);
      if (ck === gk) { found = true; break; }
      for (const nb of layout.neighbors(cur)) {
        const nk = key(nb), nc = cost[ck] + 1;
        if (nc < (cost[nk] ?? Infinity)) { cost[nk] = nc; came[nk] = cur; open.push([nc + manhattan(nb, goal), ++seq, nb]); }
      }
    }
    if (!found) throw new Error("no path");
    const path = []; let node = goal;
    while (key(node) !== key(start)) { path.push(node); node = came[key(node)]; }
    return path.reverse();
  }

  class Fleet {
    constructor(layout, n, onPick, onJobDone, prefix) {
      Object.assign(this, { layout, onPick, onJobDone, queue: [] });
      this.robots = Array.from({ length: n }, (_, i) => ({ robot_id: `${prefix}${i + 1}`, pos: layout.dock.slice(), battery: 100, state: "idle", job: null, legs: [], distance: 0, busy_ticks: 0, jobs_completed: 0 }));
    }
    submit(job) { this.queue.push(job); }
    remove(job) { const i = this.queue.indexOf(job); if (i >= 0) this.queue.splice(i, 1); }
    drainQueue() { const j = this.queue; this.queue = []; return j; }
    get backlog() { return this.queue.length + this.robots.filter((r) => r.job).length; }
    planTour(start, tasks) {
      const legs = []; let pos = start; const todo = tasks.slice();
      while (todo.length) {
        let bi = 0; for (let i = 1; i < todo.length; i++) if (manhattan(pos, this.layout.pickFaces[todo[i].location_id]) < manhattan(pos, this.layout.pickFaces[todo[bi].location_id])) bi = i;
        const t = todo.splice(bi, 1)[0], face = this.layout.pickFaces[t.location_id];
        legs.push({ path: astar(this.layout, pos, face), task: t }); pos = face;
      }
      legs.push({ path: astar(this.layout, pos, this.layout.dock), task: null });
      return legs;
    }
    step(acceptNew = true) {
      for (const r of this.robots) {
        if (r.state === "idle" && !r.job) {
          if (r.battery < 25) r.state = "charging";
          else if (acceptNew && this.queue.length) { const job = this.queue.shift(); job.robot_id = r.robot_id; r.job = job; r.legs = this.planTour(r.pos, job.tasks); r.state = "moving"; }
        }
        if (r.state === "charging") { r.battery = Math.min(100, r.battery + 5); if (r.battery >= 100) r.state = "idle"; }
        else if (r.state === "moving") { r.busy_ticks++; this.advance(r); }
      }
    }
    advance(r) {
      const leg = r.legs[0];
      if (leg.path.length) { r.pos = leg.path.shift(); r.battery = Math.max(0, r.battery - 0.25); r.distance++; }
      if (leg.path.length) return;
      if (leg.task) { leg.task.picked = this.onPick(leg.task); leg.task.done = true; }
      r.legs.shift();
      if (!r.legs.length) { const job = r.job; r.job = null; r.state = "idle"; r.jobs_completed++; this.onJobDone(job); }
    }
  }

  /* ============================== vision ============================== */
  class SimulatedDetector {
    constructor(rng, noise = 0.03, occlusion = 0.05) { Object.assign(this, { rng, noise, occlusion }); }
    detect(camera_id, location_id, sku_id, trueCount) {
      let count = trueCount, confidence = this.rng.uniform(0.88, 0.99);
      if (this.rng.random() < this.occlusion) { count = Math.max(0, trueCount + this.rng.integers(-5, 6)); confidence = this.rng.uniform(0.3, 0.7); }
      else if (this.rng.random() < this.noise) count = Math.max(0, trueCount + this.rng.choice([-1, 1]));
      return { camera_id, location_id, sku_id, count, confidence };
    }
  }
  class StockReconciler {
    constructor(inv, bus, minConf = 0.8, confirmations = 2) { Object.assign(this, { inv, bus, minConf, confirmations, pending: {} }); }
    process(dets) {
      let adjusted = 0; const wh = this.inv.warehouse_id;
      for (const d of dets) {
        if (d.confidence < this.minConf) { this.bus.publish("vision.low_confidence", { warehouse_id: wh, location_id: d.location_id, confidence: +d.confidence.toFixed(2) }); continue; }
        if (d.count === 0) this.bus.publish("vision.empty_bin", { warehouse_id: wh, location_id: d.location_id, sku_id: d.sku_id });
        const system = this.inv.oh(d.location_id);
        if (d.count === system) { delete this.pending[d.location_id]; continue; }
        const prev = this.pending[d.location_id], seen = prev && prev[0] === d.count ? prev[1] + 1 : 1;
        if (seen >= this.confirmations) {
          this.inv.adjust(d.location_id, d.count, `vision:${d.camera_id}`);
          this.bus.publish("vision.discrepancy_fixed", { warehouse_id: wh, location_id: d.location_id, sku_id: d.sku_id, system, observed: d.count });
          delete this.pending[d.location_id]; adjusted++;
        } else this.pending[d.location_id] = [d.count, seen];
      }
      return adjusted;
    }
  }

  /* ============================== forecasting ============================== */
  function features(y, t) {
    const dow = [0, 0, 0, 0, 0, 0]; if (t % 7 < 6) dow[t % 7] = 1;
    let m = 0; for (let i = t - 7; i < t; i++) m += y[i];
    return [1, t / 365, ...dow, y[t - 1], y[t - 7], m / 7];
  }
  function solve(A, b) { // Gaussian elimination with partial pivoting
    const n = b.length, M = A.map((r, i) => [...r, b[i]]);
    for (let c = 0; c < n; c++) {
      let p = c; for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
      [M[c], M[p]] = [M[p], M[c]];
      for (let r = c + 1; r < n; r++) { const f = M[r][c] / M[c][c]; for (let k = c; k <= n; k++) M[r][k] -= f * M[c][k]; }
    }
    const x = new Array(n).fill(0);
    for (let r = n - 1; r >= 0; r--) { let s = M[r][n]; for (let k = r + 1; k < n; k++) s -= M[r][k] * x[k]; x[r] = s / M[r][r]; }
    return x;
  }
  const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0);
  const std = (a, ddof = 0) => { if (a.length - ddof <= 0) return 0; const m = mean(a); return Math.sqrt(a.reduce((s, v) => s + (v - m) ** 2, 0) / (a.length - ddof)); };
  class RidgeForecaster {
    constructor(alpha = 1, minHistory = 21) { this.alpha = alpha; this.minHistory = minHistory; }
    fit(history) {
      const y = history.map(Number), level = y.length ? mean(y.slice(-7)) : 0;
      if (y.length < this.minHistory) return { w: null, sigma: y.length > 1 ? std(y.slice(-14)) : Math.sqrt(Math.max(level, 1)), level };
      const X = [], tgt = []; for (let t = 7; t < y.length; t++) { X.push(features(y, t)); tgt.push(y[t]); }
      const p = X[0].length, XtX = Array.from({ length: p }, () => new Array(p).fill(0)), Xty = new Array(p).fill(0);
      for (let i = 0; i < X.length; i++) for (let a = 0; a < p; a++) { Xty[a] += X[i][a] * tgt[i]; for (let b = 0; b < p; b++) XtX[a][b] += X[i][a] * X[i][b]; }
      for (let a = 1; a < p; a++) XtX[a][a] += this.alpha; // don't shrink the intercept
      const w = solve(XtX, Xty);
      const resid = X.map((r, i) => tgt[i] - r.reduce((s, v, k) => s + v * w[k], 0));
      return { w, sigma: std(resid, 1), level };
    }
    predict(model, history, horizon) {
      if (!model.w) return new Array(horizon).fill(model.level);
      const ext = history.map(Number), n = ext.length;
      for (let t = n; t < n + horizon; t++) ext.push(Math.max(0, features(ext, t).reduce((s, v, k) => s + v * model.w[k], 0)));
      return ext.slice(n);
    }
  }
  const wape = (a, p) => { const d = a.reduce((s, v) => s + Math.abs(v), 0); return d ? a.reduce((s, v, i) => s + Math.abs(v - p[i]), 0) / d : 0; };
  function backtest(history, horizon = 7) {
    const f = new RidgeForecaster(), train = history.slice(0, -horizon), test = history.slice(-horizon);
    const pred = f.predict(f.fit(train), train, horizon);
    const naive = Array.from({ length: horizon }, (_, h) => train[train.length - 7 + (h % 7)]);
    return { wape_model: wape(test, pred), wape_seasonal_naive: wape(test, naive) };
  }
  class ForecastService {
    constructor() { this.f = new RidgeForecaster(); this.histories = {}; this.models = {}; }
    record(k, v) { (this.histories[k] ||= []).push(Number(v)); }
    refit() { for (const [k, h] of Object.entries(this.histories)) this.models[k] = this.f.fit(h); }
    model(k) { return this.models[k] || this.f.fit(this.histories[k] || []); }
    forecast(k, h = 7) { return this.f.predict(this.model(k), this.histories[k] || [], h); }
    sigma(k) { return this.model(k).sigma; }
    reorderPoint(k, lead, z = 1.65, share = 1) { return share * (this.forecast(k, lead).reduce((a, b) => a + b, 0) + z * this.sigma(k) * Math.sqrt(lead)); }
  }

  /* ============================== network ============================== */
  class Warehouse {
    constructor(id, position, bus, nRobots, detector) {
      Object.assign(this, { warehouse_id: id, position, bus, online: true, layout: Layout.grid(), physical: {}, jobSeq: 0 });
      this.inventory = new Inventory(id, bus);
      this.fleet = new Fleet(this.layout, nRobots, (t) => this.onPick(t), (j) => this.onJobDone(j), `${id}-R`);
      const byAisle = {}; for (const l of Object.keys(this.layout.locations).sort()) (byAisle[l[0]] ||= []).push(l);
      this.cameras = Object.entries(byAisle).map(([a, locs]) => ({ camera_id: `${id}-CAM-${a}`, locs }));
      this.detector = detector; this.reconciler = new StockReconciler(this.inventory, bus);
    }
    ph(l) { return this.physical[l] || 0; }
    slot(sku, loc, qty) { this.inventory.receive(sku, loc, qty); this.physical[loc] = this.ph(loc) + qty; }
    receive(sku, qty) { const locs = this.inventory.locationsFor(sku); const loc = locs.reduce((b, l) => (this.inventory.oh(l) < this.inventory.oh(b) ? l : b), locs[0]); this.slot(sku, loc, qty); }
    get backlog() { return this.fleet.backlog; }
    dispatch(order_id, tasks) {
      const job = { job_id: `${this.warehouse_id}-J${String(++this.jobSeq).padStart(5, "0")}`, order_id, warehouse_id: this.warehouse_id, tasks, created_tick: this.bus.tick, robot_id: null };
      this.fleet.submit(job); this.bus.publish("job.queued", { warehouse_id: this.warehouse_id, job_id: job.job_id, order_id }); return job;
    }
    cancelJob(job) {
      for (const t of job.tasks) { this.inventory.release(t.location_id, t.qty); if (t.allocation) t.allocation.cancelled = true; }
      this.bus.publish("job.cancelled", { warehouse_id: this.warehouse_id, job_id: job.job_id, order_id: job.order_id });
    }
    onPick(t) {
      const actual = Math.min(this.ph(t.location_id), t.qty);
      this.physical[t.location_id] = this.ph(t.location_id) - actual;
      this.inventory.commitPick(t.location_id, t.qty, actual);
      if (t.allocation) t.allocation.picked += actual;
      if (actual < t.qty) {
        const short = t.qty - actual; if (t.allocation) t.allocation.qty -= short;
        this.inventory.adjust(t.location_id, this.ph(t.location_id), "short_pick");
        this.bus.publish("pick.short", { warehouse_id: this.warehouse_id, order_id: t.order_id, sku_id: t.sku_id, location_id: t.location_id, short });
      }
      return actual;
    }
    onJobDone(job) { for (const t of job.tasks) if (t.allocation) t.allocation.done = true; this.bus.publish("job.done", { warehouse_id: this.warehouse_id, job_id: job.job_id, order_id: job.order_id, robot_id: job.robot_id }); }
    scanShelves() {
      const dets = [];
      for (const cam of this.cameras) for (const l of cam.locs) if (this.inventory.binSku[l]) dets.push(this.detector.detect(cam.camera_id, l, this.inventory.binSku[l], this.ph(l)));
      return this.reconciler.process(dets);
    }
    setOnline(online, reason = "") { if (online === this.online) return; this.online = online; this.bus.publish(online ? "warehouse.restored" : "warehouse.outage", { warehouse_id: this.warehouse_id, reason }); }
    step() { this.fleet.step(this.online); }
  }

  /* ============================== agents ============================== */
  class DecisionLog {
    constructor(bus, max = 5000) { this.bus = bus; this.entries = []; this.max = max; }
    record(agent, action, reason, order_id = null, details = {}) {
      const d = { tick: this.bus.tick, agent, action, reason, order_id, details };
      this.entries.push(d); if (this.entries.length > this.max * 1.2) this.entries.splice(0, this.entries.length - this.max);
      this.bus.publish("agent.decision", d); return d;
    }
    recent(n = 50) { return this.entries.slice(-n); }
  }
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

  class FulfillmentAgent {
    constructor(whs, orders, bus, log) {
      Object.assign(this, { whs, orders, bus, log, shipCost: 0.05, backlogPenalty: 0.5, splitPenalty: 2, maxQueue: 8, maxReroutes: 3, retryEvery: 30, backorders: new Set(), reroutes: [] });
      bus.subscribe("pick.short", (e) => { const d = e.data; this.reroutes.push([d.order_id, [], `short pick: ${d.sku_id} at ${d.warehouse_id}/${d.location_id} missing ${d.short} units`]); });
      bus.subscribe("warehouse.outage", (e) => this.onOutage(e));
      bus.subscribe("job.done", (e) => { const o = this.orders[e.data.order_id]; if (o) this.checkComplete(o); });
    }
    cost(w, dest) { return dist(w.position, dest) * this.shipCost + w.backlog * this.backlogPenalty; }
    plan(need, dest, exclude = []) {
      let pool = Object.values(this.whs).filter((w) => w.online && !exclude.includes(w.warehouse_id));
      const costs = Object.fromEntries(pool.map((w) => [w.warehouse_id, this.cost(w, dest)]));
      const full = pool.filter((w) => Object.entries(need).every(([s, q]) => w.inventory.available(s) >= q));
      if (full.length) { const best = full.reduce((b, w) => (costs[w.warehouse_id] < costs[b.warehouse_id] ? w : b)); return [{ [best.warehouse_id]: { ...need } }, {}]; }
      const remaining = { ...need }, plan = {};
      while (Object.keys(remaining).length && pool.length) {
        const cov = (w) => Object.entries(remaining).reduce((a, [s, q]) => a + Math.min(q, w.inventory.available(s)), 0);
        const scored = pool.filter((w) => cov(w) > 0).map((w) => [cov(w) / (costs[w.warehouse_id] + this.splitPenalty), w]);
        if (!scored.length) break;
        const best = scored.reduce((b, x) => (x[0] > b[0] ? x : b))[1], take = {};
        for (const [s, q] of Object.entries(remaining)) { const t = Math.min(q, best.inventory.available(s)); if (t > 0) take[s] = t; }
        plan[best.warehouse_id] = take;
        for (const [s, q] of Object.entries(take)) { remaining[s] -= q; if (!remaining[s]) delete remaining[s]; }
        pool = pool.filter((w) => w !== best);
      }
      return [plan, remaining];
    }
    route(order, exclude = [], reason = "new order") {
      const need = order.unallocated(); if (!Object.keys(need).length) return {};
      const [plan, leftover] = this.plan(need, order.destination, exclude);
      for (const [wid, lines] of Object.entries(plan)) this.commit(order, this.whs[wid], lines);
      const was = this.backorders.has(order.order_id), hasLeft = Object.keys(leftover).length > 0;
      if (hasLeft) { order.status = OrderStatus.BACKORDERED; this.backorders.add(order.order_id); }
      else { this.backorders.delete(order.order_id); order.status = OrderStatus.ALLOCATED; }
      const planned = Object.keys(plan).length > 0;
      if (planned || (hasLeft && !was)) {
        const action = reason === "new order" ? "route" : "reroute";
        if (action === "reroute" && planned) { order.reroute_count++; this.bus.publish("order.rerouted", { order_id: order.order_id, to: Object.keys(plan), reason }); }
        this.log.record("fulfillment", action, reason, order.order_id, { plan, backordered: leftover });
      }
      return plan;
    }
    commit(order, w, lines) {
      const tasks = [];
      for (const [sku, qty] of Object.entries(lines)) {
        const a = newAllocation(w.warehouse_id, sku, qty); order.allocations.push(a);
        for (const [loc, q] of w.inventory.reserve(sku, qty)) tasks.push({ order_id: order.order_id, sku_id: sku, location_id: loc, qty: q, allocation: a, picked: 0, done: false });
        this.bus.publish("allocation.created", { order_id: order.order_id, warehouse_id: w.warehouse_id, sku_id: sku, qty });
      }
      w.dispatch(order.order_id, tasks);
    }
    onOutage(e) {
      const w = this.whs[e.data.warehouse_id], jobs = w.fleet.drainQueue();
      for (const j of jobs) { w.cancelJob(j); this.reroutes.push([j.order_id, [w.warehouse_id], `${w.warehouse_id} outage`]); }
      this.log.record("fulfillment", "evacuate", `${w.warehouse_id} went offline (${e.data.reason || "unknown"})`, null, { cancelled_jobs: jobs.length });
    }
    checkComplete(o) {
      if (o.status !== OrderStatus.SHIPPED && o.isComplete()) {
        o.status = OrderStatus.SHIPPED; o.shipped_tick = this.bus.tick; this.backorders.delete(o.order_id);
        this.bus.publish("order.shipped", { order_id: o.order_id, warehouses: o.warehouses(), cycle_ticks: o.shipped_tick - o.created_tick });
      }
    }
    step() {
      while (this.reroutes.length) {
        const [id, ex, reason] = this.reroutes.shift(), o = this.orders[id];
        if (o && o.status !== OrderStatus.SHIPPED && o.status !== OrderStatus.CANCELLED) this.route(o, ex, reason);
      }
      this.rebalance();
      if (this.bus.tick % this.retryEvery === 0) for (const id of [...this.backorders].sort()) this.route(this.orders[id], [], "backorder retry: stock became available");
    }
    rebalance() {
      const online = Object.values(this.whs).filter((w) => w.online); if (online.length < 2) return;
      for (const w of online) {
        const excess = w.fleet.queue.length - this.maxQueue; if (excess <= 0) continue;
        for (const job of w.fleet.queue.slice(-excess).reverse()) {
          const o = this.orders[job.order_id]; if (o.reroute_count >= this.maxReroutes) continue;
          const need = {}; for (const t of job.tasks) need[t.sku_id] = (need[t.sku_id] || 0) + t.qty;
          const [, left] = this.plan(need, o.destination, [w.warehouse_id]); if (Object.keys(left).length) continue;
          w.fleet.remove(job); w.cancelJob(job);
          this.route(o, [w.warehouse_id], `load balancing: ${w.warehouse_id} has ${w.fleet.queue.length + 1} queued jobs`);
        }
      }
    }
  }

  class ReplenishmentAgent {
    constructor(whs, catalog, fc, bus, log) { Object.assign(this, { whs, catalog, fc, bus, log, z: 1.65, coverDays: 7, onOrder: {}, inbound: [], hist: {}, seq: 0 }); }
    k(w, s) { return w + "|" + s; }
    recordDay(alloc) { for (const w in this.whs) for (const s in this.catalog) { const h = (this.hist[this.k(w, s)] ||= []); h.push(alloc[this.k(w, s)] || 0); if (h.length > 14) h.shift(); } }
    share(wid, sku) {
      const online = Object.values(this.whs).filter((w) => w.online), sum = (w) => (this.hist[this.k(w, sku)] || []).reduce((a, b) => a + b, 0);
      const total = online.reduce((a, w) => a + sum(w.warehouse_id), 0);
      return total ? sum(wid) / total : 1 / Math.max(1, online.length);
    }
    plan(day) {
      for (const w of Object.values(this.whs)) {
        if (!w.online) continue;
        for (const sku of w.inventory.skus()) {
          const lead = this.catalog[sku].lead_time_days, share = this.share(w.warehouse_id, sku), rop = this.fc.reorderPoint(sku, lead, this.z, share);
          const key = this.k(w.warehouse_id, sku), position = w.inventory.available(sku) + (this.onOrder[key] || 0);
          if (position >= rop) continue;
          const qty = Math.ceil(rop + this.fc.forecast(sku, this.coverDays).reduce((a, b) => a + b, 0) * share - position); if (qty <= 0) continue;
          const po = { po_id: `PO-${String(++this.seq).padStart(5, "0")}`, warehouse_id: w.warehouse_id, sku_id: sku, qty, ordered_day: day, arrival_day: day + lead };
          this.inbound.push(po); this.onOrder[key] = (this.onOrder[key] || 0) + qty;
          this.bus.publish("po.created", { ...po });
          this.log.record("replenishment", "purchase", `position ${position} < reorder point ${rop.toFixed(1)} (lead ${lead}d, share ${Math.round(share * 100)}%)`, null, { po_id: po.po_id, warehouse_id: w.warehouse_id, sku_id: sku, qty });
        }
      }
    }
    receiveDue(day) {
      const due = this.inbound.filter((p) => p.arrival_day <= day); this.inbound = this.inbound.filter((p) => p.arrival_day > day);
      for (const p of due) { this.whs[p.warehouse_id].receive(p.sku_id, p.qty); this.onOrder[this.k(p.warehouse_id, p.sku_id)] -= p.qty; this.bus.publish("po.received", { po_id: p.po_id, warehouse_id: p.warehouse_id, sku_id: p.sku_id, qty: p.qty }); }
    }
  }

  /* ============================== simulation ============================== */
  const DOW = [1.0, 1.05, 1.1, 1.05, 1.25, 0.85, 0.7];
  const NAMES = ["USB-C cable", "Wireless mouse", "Desk lamp", "Water bottle", "Notebook A5", "Phone case", "Headphones", "Coffee mug", "Backpack", "Power bank", "Keyboard", "Monitor stand", "Yoga mat", "Sunglasses", "Board game", "Plant pot"];
  const SITES = { "WH-NORTH": [20, 80], "WH-CENTRAL": [50, 45], "WH-SOUTH": [80, 15] };

  class Simulation {
    constructor(cfg = {}) {
      this.cfg = cfg = { seed: 7, n_skus: 12, ticks_per_day: 480, orders_per_day: 60, robots_per_warehouse: 3, bins_per_sku: 2, initial_cover_days: 6, scan_every_ticks: 60, shrinkage_events_per_day: 3, history_days: 56, outage: ["WH-CENTRAL", 4, 6], ...cfg };
      this.rng = new Rng(cfg.seed); this.bus = new EventBus(); this.tick = 0; this.day = 0; this.orders = {}; this.orderSeq = 0;
      this.catalog = {};
      for (let i = 0; i < cfg.n_skus; i++) { const id = `SKU-${String(i + 1).padStart(3, "0")}`; this.catalog[id] = { sku_id: id, name: NAMES[i], lead_time_days: this.rng.integers(2, 5), unit_cost: 10 }; }
      this.skuIds = Object.keys(this.catalog);
      const pop = this.skuIds.map((_, i) => 1 / Math.pow(i + 1, 0.8)), tot = pop.reduce((a, b) => a + b, 0); this.popularity = pop.map((p) => p / tot);
      this.whs = {}; Object.entries(SITES).forEach(([id, pos], i) => (this.whs[id] = new Warehouse(id, pos, this.bus, cfg.robots_per_warehouse, new SimulatedDetector(new Rng(cfg.seed * 1000 + crc(id) + i)))));
      this.seedStock();
      this.decisions = new DecisionLog(this.bus); this.fc = new ForecastService(); this.seedHistory();
      this.fulfillment = new FulfillmentAgent(this.whs, this.orders, this.bus, this.decisions);
      this.replenishment = new ReplenishmentAgent(this.whs, this.catalog, this.fc, this.bus, this.decisions);
      this.todayDemand = {}; this.todayAlloc = {}; this.nextFc = {}; this.fcErr = 0; this.naiveErr = 0; this.fcActual = 0; this.daily = [];
      this.bus.subscribe("allocation.created", (e) => { const k = e.data.warehouse_id + "|" + e.data.sku_id; this.todayAlloc[k] = (this.todayAlloc[k] || 0) + e.data.qty; });
    }
    expected(sku, day = null) { return this.cfg.orders_per_day * (day == null ? 1 : DOW[((day % 7) + 7) % 7]) * 4 * this.popularity[this.skuIds.indexOf(sku)]; }
    seedStock() {
      const n = Object.keys(this.whs).length;
      for (const w of Object.values(this.whs)) {
        const locs = this.rng.permutation(Object.keys(w.layout.locations).sort());
        for (const sku of this.skuIds) {
          const perBin = Math.max(5, Math.ceil((this.expected(sku) / n) * this.cfg.initial_cover_days / this.cfg.bins_per_sku));
          for (let b = 0; b < this.cfg.bins_per_sku; b++) w.slot(sku, locs.pop(), perBin);
        }
      }
    }
    seedHistory() { for (let d = -this.cfg.history_days; d < 0; d++) for (const s of this.skuIds) this.fc.record(s, this.rng.poisson(this.expected(s, d))); this.fc.refit(); }
    submitOrder(lines, destination) {
      for (const [s, q] of lines) { if (!this.catalog[s]) throw new HttpError(422, `unknown sku '${s}'`); if (!(q > 0)) throw new HttpError(422, "qty must be positive"); }
      const o = new Order(`ORD-${String(++this.orderSeq).padStart(6, "0")}`, lines.map(([sku_id, qty]) => ({ sku_id, qty })), destination, this.tick);
      this.orders[o.order_id] = o;
      for (const [s, q] of lines) this.todayDemand[s] = (this.todayDemand[s] || 0) + q;
      this.bus.publish("order.created", { order_id: o.order_id, lines: lines.length });
      this.fulfillment.route(o); return o;
    }
    randomOrder() {
      const n = this.rng.integers(1, 4), merged = {};
      for (let i = 0; i < n; i++) { const s = this.rng.weighted(this.skuIds, this.popularity); merged[s] = (merged[s] || 0) + this.rng.integers(1, 4); }
      return this.submitOrder(Object.entries(merged), [this.rng.integers(0, 101), this.rng.integers(0, 101)]);
    }
    step() {
      const c = this.cfg; this.bus.tick = this.tick;
      if (this.tick % c.ticks_per_day === 0) this.startOfDay();
      const n = this.rng.poisson((c.orders_per_day * DOW[this.day % 7]) / c.ticks_per_day); for (let i = 0; i < n; i++) this.randomOrder();
      this.fulfillment.step();
      for (const w of Object.values(this.whs)) w.step();
      if (this.tick % c.scan_every_ticks === 0) for (const w of Object.values(this.whs)) if (w.online) w.scanShelves();
      this.tick++;
      if (this.tick % c.ticks_per_day === 0) this.endOfDay();
    }
    runTicks(n) { for (let i = 0; i < n; i++) this.step(); }
    startOfDay() {
      if (this.cfg.outage) { const [w, s, e] = this.cfg.outage; if (this.day === s) this.whs[w].setOnline(false, "simulated power failure"); else if (this.day === e) this.whs[w].setOnline(true, "power restored"); }
      this.replenishment.receiveDue(this.day);
    }
    endOfDay() {
      for (const s of this.skuIds) {
        const actual = this.todayDemand[s] || 0, h = this.fc.histories[s];
        if (s in this.nextFc) { this.fcErr += Math.abs(actual - this.nextFc[s]); this.naiveErr += Math.abs(actual - h[h.length - 7]); this.fcActual += actual; }
        this.fc.record(s, actual);
      }
      this.fc.refit();
      this.nextFc = Object.fromEntries(this.skuIds.map((s) => [s, this.fc.forecast(s, 1)[0]]));
      this.replenishment.recordDay(this.todayAlloc); this.replenishment.plan(this.day + 1); this.shrinkage();
      const snap = this.metrics(); snap.day = this.day; this.daily.push(snap);
      this.bus.publish("day.closed", { day: this.day }); this.todayDemand = {}; this.todayAlloc = {}; this.day++;
    }
    shrinkage() {
      for (const w of Object.values(this.whs)) {
        const n = this.rng.poisson(this.cfg.shrinkage_events_per_day);
        for (let i = 0; i < n; i++) {
          const stocked = Object.keys(w.inventory.binSku).sort().filter((l) => w.ph(l) > 0); if (!stocked.length) break;
          const loc = stocked[this.rng.integers(0, stocked.length)], lost = Math.min(w.ph(loc), this.rng.integers(1, 6));
          w.physical[loc] -= lost; this.bus.publish("sim.shrinkage", { warehouse_id: w.warehouse_id, location_id: loc, lost });
        }
      }
    }
    metrics() {
      const orders = Object.values(this.orders), shipped = orders.filter((o) => o.status === OrderStatus.SHIPPED);
      const cyc = shipped.map((o) => o.shipped_tick - o.created_tick).sort((a, b) => a - b);
      const pct = (a, p) => { if (!a.length) return null; const r = (a.length - 1) * p, lo = Math.floor(r), hi = Math.ceil(r); return a[lo] + (a[hi] - a[lo]) * (r - lo); };
      const robots = Object.values(this.whs).flatMap((w) => w.fleet.robots), c = (k) => this.bus.count(k);
      const r1 = (v) => (v == null ? null : Math.round(v * 10) / 10), r3 = (v) => Math.round(v * 1000) / 1000;
      return {
        tick: this.tick, day: this.day, orders: orders.length, shipped: shipped.length, open_backorders: this.fulfillment.backorders.size,
        in_progress: orders.filter((o) => o.status === OrderStatus.ALLOCATED).length,
        avg_cycle_ticks: cyc.length ? r1(mean(cyc)) : null, p95_cycle_ticks: r1(pct(cyc, 0.95)),
        split_shipments: shipped.filter((o) => o.warehouses().length > 1).length,
        reroutes: c("order.rerouted"), short_picks: c("pick.short"), vision_fixes: c("vision.discrepancy_fixed"), vision_low_confidence: c("vision.low_confidence"),
        shrinkage_events: c("sim.shrinkage"), purchase_orders: c("po.created"),
        robot_utilization: r3(robots.reduce((a, r) => a + r.busy_ticks, 0) / Math.max(1, this.tick * robots.length)),
        robot_distance: robots.reduce((a, r) => a + r.distance, 0),
        forecast_wape: this.fcActual ? r3(this.fcErr / this.fcActual) : null, naive_wape: this.fcActual ? r3(this.naiveErr / this.fcActual) : null,
        warehouses_online: Object.keys(this.whs).filter((w) => this.whs[w].online),
      };
    }
  }

  /* ============================== api (same routes as warehouse/api.py) ============================== */
  class HttpError extends Error { constructor(status, detail) { super(detail); this.status = status; } }
  const MAX_DAYS = 120;
  const plain = (o) => JSON.parse(JSON.stringify(o));
  const orderOut = (o) => ({ order_id: o.order_id, status: o.status, destination: [...o.destination], lines: plain(o.lines), allocations: plain(o.allocations),
    unallocated: o.unallocated(), warehouses: o.warehouses(), created_tick: o.created_tick, shipped_tick: o.shipped_tick, reroute_count: o.reroute_count });
  const NOISY = new Set(["allocation.created", "job.queued", "agent.decision", "order.created", "job.done", "vision.low_confidence", "day.closed"]);

  let sim = new Simulation();
  const wh = (id) => { const w = sim.whs[id]; if (!w) throw new HttpError(404, `unknown warehouse ${id}`); return w; };
  const summary = (w) => ({ warehouse_id: w.warehouse_id, position: [...w.position], online: w.online, backlog: w.backlog, queued_jobs: w.fleet.queue.length,
    robots: w.fleet.robots.map((r) => ({ robot_id: r.robot_id, state: r.state, pos: [...r.pos], battery: Math.round(r.battery * 10) / 10, job: r.job?.job_id ?? null, jobs_completed: r.jobs_completed, distance: r.distance })) });

  const routes = [
    ["GET", /^\/health$/, () => ({ ok: true, tick: sim.tick, day: sim.day, engine: "browser" })],
    ["GET", /^\/catalog$/, () => Object.values(sim.catalog)],
    ["GET", /^\/warehouses$/, () => Object.values(sim.whs).map(summary)],
    ["GET", /^\/warehouses\/([^/]+)\/inventory$/, (m) => wh(m[1]).inventory.snapshot()],
    ["POST", /^\/warehouses\/([^/]+)\/outage$/, (m) => { wh(m[1]).setOnline(false, "manual"); sim.fulfillment.step(); return summary(wh(m[1])); }],
    ["POST", /^\/warehouses\/([^/]+)\/restore$/, (m) => { wh(m[1]).setOnline(true, "manual"); return summary(wh(m[1])); }],
    ["POST", /^\/warehouses\/([^/]+)\/scan$/, (m) => ({ adjustments: wh(m[1]).scanShelves() })],
    ["POST", /^\/orders$/, (m, q, body) => {
      if (!body?.lines?.length || !Array.isArray(body.destination)) throw new HttpError(422, "lines and destination are required");
      return orderOut(sim.submitOrder(body.lines.map((l) => [l.sku_id, l.qty]), body.destination));
    }],
    ["GET", /^\/orders$/, (m, q) => {
      const list = Object.values(sim.orders).filter((o) => !q.get("status") || o.status === q.get("status"));
      return list.slice(-(+q.get("limit") || 50)).reverse().map(orderOut);
    }],
    ["GET", /^\/orders\/([^/]+)$/, (m) => { const o = sim.orders[m[1]]; if (!o) throw new HttpError(404, `unknown order ${m[1]}`); return orderOut(o); }],
    ["GET", /^\/forecast\/([^/]+)$/, (m, q) => {
      const sku = m[1]; if (!sim.catalog[sku]) throw new HttpError(404, `unknown sku ${sku}`);
      const h = sim.fc.histories[sku], horizon = +q.get("horizon") || 14, hist = +q.get("history") || 14;
      return { sku_id: sku, history_tail: h.slice(-hist), forecast: sim.fc.forecast(sku, horizon).map((x) => Math.round(x * 100) / 100), sigma: Math.round(sim.fc.sigma(sku) * 100) / 100, backtest_7d: backtest(h, 7) };
    }],
    ["GET", /^\/inventory\/overview$/, () => Object.values(sim.catalog).map((sku) => {
      const fc7 = sim.fc.forecast(sku.sku_id, 7).reduce((a, b) => a + b, 0), per = {};
      for (const w of Object.values(sim.whs)) per[w.warehouse_id] = { on_hand: w.inventory.onHandTotal(sku.sku_id), available: w.inventory.available(sku.sku_id), on_order: sim.replenishment.onOrder[w.warehouse_id + "|" + sku.sku_id] || 0, online: w.online };
      const available = Object.values(per).filter((v) => v.online).reduce((a, v) => a + v.available, 0), daily = fc7 / 7;
      return { sku_id: sku.sku_id, name: sku.name, lead_time_days: sku.lead_time_days, warehouses: per, available,
        on_order: Object.values(per).reduce((a, v) => a + v.on_order, 0), forecast_7d: Math.round(fc7 * 10) / 10, days_of_cover: daily ? Math.round((available / daily) * 10) / 10 : null };
    })],
    ["GET", /^\/purchase-orders$/, () => plain([...sim.replenishment.inbound].sort((a, b) => a.arrival_day - b.arrival_day))],
    ["GET", /^\/decisions$/, (m, q) => plain(sim.decisions.recent(+q.get("limit") || 50))],
    ["GET", /^\/events$/, (m, q) => plain(sim.bus.log.filter((e) => !q.get("kind") || e.kind === q.get("kind")).slice(-(+q.get("limit") || 100)))],
    ["GET", /^\/metrics$/, () => sim.metrics()],
    ["GET", /^\/metrics\/daily$/, () => sim.daily],
    ["POST", /^\/simulation\/step$/, (m, q, body) => {
      if (sim.day >= MAX_DAYS) throw new HttpError(409, `The simulation reached day ${MAX_DAYS}. Reset to start over.`);
      sim.runTicks(Math.min(Math.max(1, +body?.ticks || 60), 48000, MAX_DAYS * sim.cfg.ticks_per_day - sim.tick)); return sim.metrics();
    }],
    ["POST", /^\/simulation\/reset$/, (m, q, body) => { sim = new Simulation({ seed: body?.seed ?? 7, ...(body?.outage === false ? { outage: null } : {}) }); return sim.metrics(); }],
    ["GET", /^\/dashboard\/state$/, () => ({
      tick: sim.tick, day: sim.day, ticks_per_day: sim.cfg.ticks_per_day, metrics: sim.metrics(), daily: sim.daily,
      warehouses: Object.values(sim.whs).map((w) => ({
        warehouse_id: w.warehouse_id, online: w.online, backlog: w.backlog, queued_jobs: w.fleet.queue.length, width: w.layout.width, height: w.layout.height, dock: [...w.layout.dock],
        bins: Object.entries(w.layout.locations).map(([loc, [x, y]]) => { const s = w.inventory.binSku[loc]; return { loc, x, y, sku: s ?? null, on_hand: s ? w.inventory.oh(loc) : 0, reserved: s ? w.inventory.rs(loc) : 0, physical: s ? w.ph(loc) : 0 }; }),
        robots: w.fleet.robots.map((r) => ({ id: r.robot_id, pos: [...r.pos], state: r.state, battery: Math.round(r.battery), order: r.job?.order_id ?? null,
          route: r.legs.flatMap((l) => l.path.map((c) => [...c])), stops: r.legs.filter((l) => l.task).map((l) => [...w.layout.pickFaces[l.task.location_id]]) })),
      })),
      skus: Object.values(sim.catalog).map((s) => ({ sku_id: s.sku_id, name: s.name })),
      decisions: plain(sim.decisions.recent(40)).reverse(),
      events: plain(sim.bus.log.filter((e) => !NOISY.has(e.kind)).slice(-40)).reverse(),
    })],
  ];

  function request(path, opts = {}) {
    const url = new URL(path, "http://local"), method = (opts.method || "GET").toUpperCase();
    const body = opts.body ? JSON.parse(opts.body) : null;
    for (const [m, re, fn] of routes) { if (m !== method) continue; const match = url.pathname.match(re); if (match) return fn(match, url.searchParams, body); }
    throw new HttpError(404, `no route ${method} ${url.pathname}`);
  }

  return { request, Simulation, RidgeForecaster, backtest, astar, Layout, HttpError, get sim() { return sim; } };
});
