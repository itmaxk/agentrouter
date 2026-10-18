"""agentrouter command line."""
import argparse
import json
import logging
import sys
import threading
import time

from . import auth, config, probe, report
from .daemon import Daemon, apply_switch, make_writers
from .db import DB
from .stats import fmt_duration, parse_period

BAR = " ▁▂▃▄▅▆▇█"


def _pct(x):
    return "-" if x is None else f"{x * 100:5.1f}%"


def _ts(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _state(up):
    return {True: "UP", False: "DOWN", None: "?"}[up]


def _bar(p):
    return "·" if p is None else BAR[min(8, max(0, round(p * 8)))]


def cmd_run(args, cfg, db):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not args.no_web:
        from . import web
        threading.Thread(target=web.serve, args=(cfg, args.db), daemon=True).start()
    Daemon(cfg, db, dry_run=args.dry_run, simulate_down=args.simulate_down or ()).run()


def cmd_web(args, cfg, db):
    from . import web
    web.serve(cfg, args.db)


def cmd_status(args, cfg, db):
    s = report.status(db, cfg)
    if args.json:
        return print(json.dumps(s, indent=2, default=str))
    for name, t in s["targets"].items():
        print(f"{name:9} -> {t.get('model') or '(not on agentrouter)'}{'  ERROR ' + t['error'] if t.get('error') else ''}")
    if s["pinned"]:
        print(f"pinned    -> {s['pinned']}")
    print()
    print(f"{'MODEL':22} {'API':9} {'STATE':5} {'FOR':>8} {'24h':>7} {'NEXT30m':>8} {'LAST PROBE'}")
    for m in s["models"]:
        lp = m["last_probe"]
        last = f"{_ts(lp['ts'])} {lp['status']} {lp['latency_ms']}ms {lp['error'][:50]}" if lp else "-"
        print(f"{m['id']:22} {m['endpoint']:9} {_state(m['up']):5} {fmt_duration(m['for_seconds']):>8} "
              f"{_pct(m['availability_24h']):>7} {_pct(m['next_30m']):>8} {last}")
    if s["last_error"]:
        print(f"\nlast error {_ts(s['last_error']['ts'])}: {s['last_error']['error'][:200]}")
    if s["switches"]:
        print("\nrecent switches:")
        for sw in s["switches"][:5]:
            print(f"  {_ts(sw['ts'])} {sw['target']:9} {sw['from_model']} -> {sw['to_model']} ({sw['reason']})"
                  f"{'' if sw['applied'] else ' [dry-run]'}")


def cmd_stats(args, cfg, db):
    rows = report.stats_table(db, parse_period(args.period))
    if args.json:
        return print(json.dumps(rows, indent=2))
    print(f"period {args.period}")
    print(f"{'MODEL':22} {'AVAIL':>7} {'UP':>8} {'DOWN':>8} {'OUTAGES':>7} {'AVG UP':>8} {'AVG DOWN':>8} {'MAX DOWN':>8}")
    for r in sorted(rows, key=lambda r: -(r["availability"] or -1)):
        print(f"{r['id']:22} {_pct(r['availability']):>7} {fmt_duration(r['up_seconds']):>8} "
              f"{fmt_duration(r['down_seconds']):>8} {r['outages']:>7} {fmt_duration(r['avg_up_seconds']):>8} "
              f"{fmt_duration(r['avg_down_seconds']):>8} {fmt_duration(r['longest_down_seconds']):>8}")


def cmd_history(args, cfg, db):
    rows = report.history(db, args.model, parse_period(args.since))
    if args.json:
        return print(json.dumps(rows, indent=2))
    for r in rows:
        print(f"{r['model']:22} {_state(bool(r['up'])):5} {_ts(r['start_ts'])} → "
              f"{'now        ' if r['open'] else _ts(r['end_ts'])}  {fmt_duration(r['end_ts'] - r['start_ts'])}")


def cmd_predict(args, cfg, db):
    pr = report.predictions(db, cfg, args.hours)
    if args.json:
        return print(json.dumps(pr, indent=2))
    first = next(iter(pr.values()), None)
    if not first:
        return print("no data yet")
    hours = [time.localtime(h["ts"]).tm_hour for h in first["hourly"]]
    print(f"{'':22} " + "".join(str(h // 10) for h in hours))
    print(f"{'next %dh (local hour)' % args.hours:22} " + "".join(str(h % 10) for h in hours))
    for mid, p in pr.items():
        print(f"{mid:22} " + "".join(_bar(h["p"]) for h in p["hourly"]) + f"  ({p['samples']:.0f} samples)")
    print("\n█ likely up   ▁ likely down   · no data")


def cmd_check(args, cfg, db):
    models = [{"id": m} for m in args.models] if args.models else probe.list_models(cfg)
    known = {m["id"]: m["endpoint"] for m in db.models(listed_only=False)}
    for m in models:
        r = probe.probe_auto(cfg, m["id"], known.get(m["id"], "chat"))
        print(f"{m['id']:22} {r.endpoint:9} {r.status:10} HTTP {r.http_code:<3} {r.latency_ms:>6}ms {r.error[:100]}")


def cmd_switch(args, cfg, db):
    known = {m["id"]: m["endpoint"] for m in db.models(listed_only=False)}
    endpoint = known.get(args.model, "chat")
    for name, w in make_writers(cfg).items():
        if args.target and name != args.target:
            continue
        if endpoint not in cfg["targets"][name]["endpoints"]:
            print(f"{name}: skipped, {args.model} needs the {endpoint} API")
            continue
        changed = apply_switch(db, cfg, name, w, args.model, endpoint, "manual", args.dry_run)
        print(f"{name}: {'switched to' if changed else 'already on'} {args.model}")


def cmd_pin(args, cfg, db):
    db.set("pinned", args.model)
    print(f"pinned {args.model}; the daemon will apply it on its next pass")


def cmd_unpin(args, cfg, db):
    db.set("pinned", None)
    print("unpinned; automatic selection resumed")


def cmd_key(args, cfg, db):
    print(auth.api_key())


def cmd_init(args, cfg, db):
    print(("wrote " if config.write_default() else "exists: ") + str(config.CONFIG_PATH))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agentrouter", description=__doc__)
    ap.add_argument("--db", help="state database path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run the background daemon (with dashboard)")
    p.add_argument("--dry-run", action="store_true", help="decide and log switches without editing configs")
    p.add_argument("--no-web", action="store_true")
    p.add_argument("--simulate-down", nargs="*", metavar="MODEL", help="treat these models as down (testing)")
    p.set_defaults(fn=cmd_run)
    sub.add_parser("web", help="serve only the dashboard").set_defaults(fn=cmd_web)

    p = sub.add_parser("status", help="current models and per-model state")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("stats", help="availability statistics")
    p.add_argument("--period", default="7d")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_stats)
    p = sub.add_parser("history", help="up/down intervals")
    p.add_argument("model", nargs="?")
    p.add_argument("--since", default="2d")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_history)
    p = sub.add_parser("predict", help="learned availability forecast")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_predict)
    p = sub.add_parser("check", help="probe models once, right now")
    p.add_argument("models", nargs="*")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("switch", help="point targets at a model now")
    p.add_argument("model")
    p.add_argument("--target", choices=["opencode", "hermes"])
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_switch)
    p = sub.add_parser("pin", help="keep targets on a model, disabling auto-switching")
    p.add_argument("model")
    p.set_defaults(fn=cmd_pin)
    sub.add_parser("unpin", help="resume auto-switching").set_defaults(fn=cmd_unpin)
    sub.add_parser("key", help="print the AgentRouter API key (Hermes key_cmd)").set_defaults(fn=cmd_key)
    sub.add_parser("init", help="write a default config file").set_defaults(fn=cmd_init)

    args = ap.parse_args(argv)
    cfg = config.load()
    if args.cmd == "key":
        return cmd_key(args, cfg, None)
    db = DB(args.db)
    try:
        return args.fn(args, cfg, db)
    except (RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
