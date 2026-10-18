"""Learns when each model tends to be up, by hour of day and hour of week.

Intervals are sampled every `step_sec`; each sample adds a weight (decaying with age) to its
hour-of-day and hour-of-week bucket. Estimates are smoothed hierarchically:
overall rate -> hour-of-day -> hour-of-week, so sparse weekly buckets lean on the daily pattern.
"""
import math
import time
from datetime import datetime

PSEUDO = 6.0  # pseudo-samples (~30 min of observations) pulling a bucket toward its parent
# Beta(3, 1) prior: a model with no history is assumed mostly available, so a fresh model
# doesn't look like an imminent outage before any pattern has been learned
PRIOR_UP, PRIOR_N = 3.0, 4.0
UNKNOWN_P = PRIOR_UP / PRIOR_N


def bucket_of(ts: float) -> tuple[int, int]:
    t = datetime.fromtimestamp(ts)
    return t.hour, t.weekday() * 24 + t.hour


class Profile:
    def __init__(self):
        self.hod = [[0.0, 0.0] for _ in range(24)]    # [up_w, total_w]
        self.how = [[0.0, 0.0] for _ in range(168)]
        self.up_w = self.total_w = 0.0

    def add(self, ts, up, w):
        h, wh = bucket_of(ts)
        u = w if up else 0.0
        for b in (self.hod[h], self.how[wh]):
            b[0] += u
            b[1] += w
        self.up_w += u
        self.total_w += w

    @property
    def prior(self) -> float:
        return (self.up_w + PRIOR_UP) / (self.total_w + PRIOR_N)

    def p_at(self, ts: float) -> float:
        h, wh = bucket_of(ts)
        u, n = self.hod[h]
        p_hod = (u + PSEUDO * self.prior) / (n + PSEUDO)
        u, n = self.how[wh]
        return (u + PSEUDO * p_hod) / (n + PSEUDO)

    def samples(self) -> float:
        return self.total_w


def build(intervals: list[dict], cfg: dict, now: float | None = None) -> dict[str, Profile]:
    now = now or time.time()
    pc = cfg["predict"]
    step, half_life = pc["step_sec"], pc["half_life_days"] * 86400
    horizon = now - pc["history_days"] * 86400
    profiles: dict[str, Profile] = {}
    for iv in intervals:
        prof = profiles.setdefault(iv["model"], Profile())
        start, end = max(iv["start_ts"], horizon), min(iv["end_ts"], now)
        if end < start:
            continue
        # align samples to the global grid so adjacent intervals don't double count
        t = math.ceil(start / step) * step
        if t > end:  # interval shorter than a step still counts, proportionally
            prof.add(start, iv["up"], ((end - start) / step) * 0.5 ** ((now - start) / half_life))
            continue
        while t <= end:
            prof.add(t, iv["up"], 0.5 ** ((now - t) / half_life))
            t += step
    return profiles


def forecast(prof: Profile | None, now: float, current_up: bool | None, minutes: int, step_min: int = 5,
             tau_min: float = 15.0) -> list[float]:
    """Availability probabilities for now, now+step, ..., now+minutes.

    The live status dominates the near term and fades into the learned pattern with time constant tau."""
    out = []
    for k in range(0, minutes + 1, step_min):
        ts = now + k * 60
        hist = prof.p_at(ts) if prof else UNKNOWN_P
        if current_up is None:
            out.append(hist)
            continue
        w = math.exp(-k / tau_min)
        out.append(w * (1.0 if current_up else 0.0) + (1 - w) * hist)
    return out


def window_score(prof, now, current_up, minutes) -> float:
    """Worst expected availability over the lookahead window: catches outages about to start."""
    return min(forecast(prof, now, current_up, minutes))
