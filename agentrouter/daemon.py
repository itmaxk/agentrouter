"""Background loop: discover models, probe on an adaptive schedule, keep targets on the best model."""
import logging
import signal
import threading
import time

from . import predict, probe, selector
from .db import DB
from .writers.hermes import HermesWriter
from .writers.opencode import OpencodeWriter

log = logging.getLogger("agentrouter")


def make_writers(cfg) -> dict:
    out = {}
    t = cfg["targets"]
    if t["opencode"].get("enabled"):
        out["opencode"] = OpencodeWriter(t["opencode"])
    if t["hermes"].get("enabled"):
        out["hermes"] = HermesWriter(t["hermes"], cfg)
    return out


def model_views(db: DB, cfg, now, endpoints=None) -> list[selector.ModelView]:
    profiles = predict.build(db.intervals(since=now - cfg["predict"]["history_days"] * 86400), cfg, now)
    views = []
    for m in db.models():
        if endpoints and m["endpoint"] not in endpoints:
            continue
        iv = db.open_interval(m["id"])
        views.append(selector.ModelView(m["id"], m["endpoint"], bool(iv["up"]) if iv else None,
                                        iv["start_ts"] if iv else None, profiles.get(m["id"])))
    return views


def apply_switch(db: DB, cfg, target: str, writer, model: str, endpoint: str, reason: str, dry_run: bool,
                 before: str | None = None):
    before = before if before is not None else writer.current()
    if writer.name == "hermes":
        changed = writer.apply(model, endpoint, dry_run=dry_run)
    else:
        changed = writer.apply(model, dry_run=dry_run)
    if changed or before != model:
        db.add_switch(time.time(), target, before, model, reason, applied=not dry_run)
        log.info("%s: %s -> %s (%s)%s", target, before, model, reason, " [dry-run]" if dry_run else "")
    return changed


class Daemon:
    def __init__(self, cfg, db: DB, dry_run=False, simulate_down=()):
        self.cfg, self.db, self.dry_run = cfg, db, dry_run
        self.simulate_down = set(simulate_down)
        self.writers = make_writers(cfg)
        self.stop = threading.Event()
        self.next_due: dict[str, float] = {}
        self.pending: dict[str, tuple[bool, int]] = {}   # unconfirmed flip: (up, count)
        self.next_discover = 0.0
        self.virtual: dict[str, str] = {}   # dry-run: what each target would be on now
        self.virtual_ts: dict[str, float] = {}
        self.paused = False
        iv = cfg["intervals"]
        self.max_gap = 2 * iv["other"] + 2 * iv["timeout"]

    # --- discovery ---
    def discover(self, now):
        try:
            models = probe.list_models(self.cfg)
        except Exception as e:
            log.warning("model discovery failed: %s", e)
            self.db.set("last_error", {"ts": now, "error": f"discovery: {e}"})
            self.next_discover = now + 120
            return
        for m in models:
            self.db.upsert_model(m["id"], m["endpoint_types"], now)
            self.next_due.setdefault(m["id"], now)
        self.db.mark_unlisted([m["id"] for m in models])
        self.next_discover = now + self.cfg["intervals"]["discover"]
        log.info("discovered %d models: %s", len(models), ", ".join(m["id"] for m in models))

    # --- probing ---
    def current(self, name, writer):
        if name in self.virtual:
            return self.virtual[name]
        try:
            return writer.current()
        except Exception:
            return None

    def in_use(self) -> set:
        return {c for n, w in self.writers.items() if (c := self.current(n, w))}

    def interval_for(self, model, used, ranked) -> float:
        iv = self.cfg["intervals"]
        if model in used:
            return iv["current"]
        if model in ranked[: self.cfg["candidates"] + len(used)]:
            return iv["candidate"]
        return iv["other"]

    def probe_one(self, mrow, now):
        mid = mrow["id"]
        if mid in self.simulate_down:
            res = probe.ProbeResult(mid, mrow["endpoint"], probe.DOWN, 0, 0, "simulated outage")
        else:
            res = probe.probe_auto(self.cfg, mid, mrow["endpoint"])
        if res.endpoint != mrow["endpoint"] and res.status != probe.WRONG_ENDPOINT:
            self.db.set_endpoint(mid, res.endpoint)
        ts = time.time()
        self.db.add_probe(ts, res)
        if res.status in probe.NEUTRAL_STATUSES:
            log.warning("%s: %s %s", mid, res.status, res.error)
            self.db.set("last_error", {"ts": ts, "error": f"{mid}: {res.error}"})
            return None
        open_iv = self.db.open_interval(mid)
        confirmed = bool(open_iv["up"]) if open_iv and ts - open_iv["end_ts"] <= self.max_gap else None
        if confirmed is None or res.up == confirmed:
            self.pending.pop(mid, None)
            self.db.record_status(mid, res.up, ts, self.max_gap)
            return None
        up, n = self.pending.get(mid, (res.up, 0))
        n = n + 1 if up == res.up else 1
        if n >= 2:
            self.pending.pop(mid, None)
            self.db.record_status(mid, res.up, ts, self.max_gap)
            log.info("%s is now %s (%s)", mid, "UP" if res.up else "DOWN", res.error or f"{res.latency_ms}ms")
            return res.up
        # keep the open interval alive while waiting for confirmation
        self.pending[mid] = (res.up, n)
        self.db.record_status(mid, confirmed, ts, self.max_gap)
        self.next_due[mid] = ts + self.cfg["intervals"]["confirm"]
        return "pending"

    # --- selection ---
    def evaluate(self, now):
        pinned = self.db.get("pinned")
        for name, w in self.writers.items():
            try:
                if not w.managed():
                    continue
                endpoints = self.cfg["targets"][name]["endpoints"]
                views = model_views(self.db, self.cfg, now, endpoints)
                cur = self.current(name, w)
                last = self.db.last_switch_ts(name) if not self.dry_run else self.virtual_ts.get(name, 0.0)
                d = selector.decide(cur, views, self.cfg, now, last, pinned)
                if d:
                    ep = next(v.endpoint for v in views if v.id == d.model)
                    apply_switch(self.db, self.cfg, name, w, d.model, ep, d.reason, self.dry_run, before=cur)
                    if self.dry_run:
                        self.virtual[name], self.virtual_ts[name] = d.model, now
            except Exception:
                log.exception("evaluating %s failed", name)

    def tick(self):
        now = time.time()
        paused = bool(self.db.get("checks_paused"))
        if paused != self.paused:
            self.paused = paused
            self.pending.clear()
            log.info("checks %s", "paused" if paused else "resumed")
        if paused:
            return
        if now >= self.next_discover:
            self.discover(now)
        used = self.in_use()
        ranked = [v.id for v in sorted(model_views(self.db, self.cfg, now),
                                       key=lambda v: -selector.score(v, self.cfg["priority"], now,
                                                                     self.cfg["selector"]["lookahead_min"]))]
        for m in self.db.models():
            if self.stop.is_set() or self.db.get("checks_paused"):
                return
            if self.next_due.get(m["id"], 0) > now:
                continue
            self.next_due[m["id"]] = now + self.interval_for(m["id"], used, ranked)
            self.probe_one(m, now)
        self.evaluate(time.time())

    def run(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        log.info("agentrouter daemon started%s", " (dry-run)" if self.dry_run else "")
        last_prune = 0.0
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            if time.time() - last_prune > 86400:
                self.db.prune(self.cfg["probe_retention_days"])
                last_prune = time.time()
            due = min(list(self.next_due.values()) + [self.next_discover], default=time.time() + 30)
            self.stop.wait(max(1.0, min(30.0, due - time.time())))
        log.info("agentrouter daemon stopped")
