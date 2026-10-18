"""Availability statistics derived from up/down intervals."""
import statistics
import time


def summarize(intervals: list[dict], since: float, now: float | None = None) -> dict:
    """intervals: one model's rows ordered by start_ts."""
    now = now or time.time()
    up_t = down_t = 0.0
    ups, downs = [], []
    outages = 0
    for iv in intervals:
        start, end = max(iv["start_ts"], since), min(iv["end_ts"], now)
        if end < start:
            continue
        d = end - start
        if iv["up"]:
            up_t += d
            ups.append(d)
        else:
            down_t += d
            downs.append(d)
            if iv["start_ts"] >= since:
                outages += 1
    observed = up_t + down_t
    last = intervals[-1] if intervals else None
    return {
        "availability": up_t / observed if observed else None,
        "up_seconds": up_t,
        "down_seconds": down_t,
        "observed_seconds": observed,
        "outages": outages,
        "avg_up_seconds": statistics.mean(ups) if ups else 0.0,
        "avg_down_seconds": statistics.mean(downs) if downs else 0.0,
        "longest_down_seconds": max(downs, default=0.0),
        "current_up": bool(last["up"]) if last and last["open"] else None,
        "current_since": last["start_ts"] if last and last["open"] else None,
        "current_seconds": (now - last["start_ts"]) if last and last["open"] else None,
    }


def fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "-"
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d{h:02d}h"
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def parse_period(text: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    text = text.strip().lower()
    if text and text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text) * 3600
