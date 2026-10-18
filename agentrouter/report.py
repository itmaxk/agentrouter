"""Read-side helpers shared by the CLI and the web dashboard."""
import time

from . import predict, stats
from .daemon import make_writers, model_views


def status(db, cfg, now=None) -> dict:
    now = now or time.time()
    since_day = now - 86400
    models = []
    for v in model_views(db, cfg, now):
        last = db.last_probe(v.id)
        s = stats.summarize(db.intervals(v.id, since_day), since_day, now)
        models.append({
            "id": v.id, "endpoint": v.endpoint, "up": v.up, "since": v.up_since,
            "for_seconds": (now - v.up_since) if v.up_since else None,
            "availability_24h": s["availability"],
            "next_30m": predict.window_score(v.profile, now, v.up, cfg["selector"]["lookahead_min"]),
            "last_probe": last,
        })
    targets = {}
    for name, w in make_writers(cfg).items():
        try:
            targets[name] = {"model": w.current(), "path": str(w.path)}
        except Exception as e:
            targets[name] = {"model": None, "error": str(e)}
    return {"now": now, "models": models, "targets": targets, "pinned": db.get("pinned"),
            "last_error": db.get("last_error"), "switches": db.switches(10)}


def stats_table(db, period_sec, now=None) -> list[dict]:
    now = now or time.time()
    since = now - period_sec
    return [{"id": m["id"], **stats.summarize(db.intervals(m["id"], since), since, now)}
            for m in db.models(listed_only=False)]


def history(db, model=None, period_sec=7 * 86400, now=None) -> list[dict]:
    now = now or time.time()
    return db.intervals(model, now - period_sec)


def predictions(db, cfg, hours=24, now=None) -> dict:
    """Hourly forecast for the next `hours` and the learned 7x24 hour-of-week map per model."""
    now = now or time.time()
    out = {}
    hour0 = now - now % 3600
    for v in model_views(db, cfg, now):
        p = v.profile
        out[v.id] = {
            "samples": p.samples() if p else 0,
            "hourly": [{"ts": hour0 + h * 3600, "p": p.p_at(hour0 + h * 3600 + 1800) if p else None}
                       for h in range(hours)],
            "week": [[(p.p_at(_week_ts(now, d, h)) if p else None) for h in range(24)] for d in range(7)],
        }
    return out


def _week_ts(now, weekday, hour):
    """A timestamp in the current week's (weekday, hour) slot; only its bucket matters."""
    lt = time.localtime(now)
    midnight = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    return midnight + (weekday - lt.tm_wday) * 86400 + hour * 3600 + 1800
