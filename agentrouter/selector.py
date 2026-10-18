"""Chooses the model a target should use, including switching ahead of predicted outages."""
from dataclasses import dataclass

from . import predict


@dataclass
class ModelView:
    id: str
    endpoint: str
    up: bool | None          # confirmed status, None if never probed
    up_since: float | None   # start of the current confirmed status
    profile: object = None


@dataclass
class Decision:
    model: str
    reason: str              # down | predicted | better | pinned | initial
    scores: dict


def priority_weight(model: str, priority: list[str]) -> float:
    if model in priority:
        return max(0.3, 1.0 - 0.15 * priority.index(model))
    return 0.25


def score(m: ModelView, priority, now, lookahead) -> float:
    if not m.up:
        return 0.0
    return priority_weight(m.id, priority) * predict.window_score(m.profile, now, m.up, lookahead)


def decide(current: str | None, models: list[ModelView], cfg: dict, now: float, last_switch: float,
           pinned: str | None = None) -> Decision | None:
    """models must already be filtered to those the target can use."""
    sc = cfg["selector"]
    prio, look = cfg["priority"], sc["lookahead_min"]
    by_id = {m.id: m for m in models}
    scores = {m.id: score(m, prio, now, look) for m in models}

    if pinned:
        return Decision(pinned, "pinned", scores) if pinned != current and pinned in by_id else None

    live = [m for m in models if m.up]
    if not live:
        return None
    best = max(live, key=lambda m: (scores[m.id], -prio.index(m.id) if m.id in prio else -len(prio)))
    cur = by_id.get(current)

    if cur is None:
        return Decision(best.id, "initial", scores) if best.id != current else None
    if cur.up is False:
        return Decision(best.id, "down", scores) if best.id != current else None
    if now - last_switch < sc["cooldown"]:
        return None

    stable = [m for m in live if m.id == current or (m.up_since is not None and now - m.up_since >= sc["min_stable"])]
    best = max(stable, key=lambda m: scores[m.id])
    if best.id == current or scores[best.id] <= scores[current] * (1 + sc["margin"]):
        return None
    cur_window = predict.window_score(cur.profile, now, cur.up, look)
    return Decision(best.id, "predicted" if cur_window < sc["proactive_threshold"] else "better", scores)
