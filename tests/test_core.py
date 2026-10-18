import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from agentrouter import config, predict, selector, stats
from agentrouter.db import DB
from agentrouter.probe import ProbeResult


def cfg():
    return copy.deepcopy(config.DEFAULTS)


def daily_outage_intervals(model, days, now, start_h=14, end_h=16):
    """Model is down every day between start_h and end_h local time, up otherwise."""
    out = []
    day0 = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    for d in range(days + 1):
        base = day0 + timedelta(days=d)
        segs = [(base, base.replace(hour=start_h), 1), (base.replace(hour=start_h), base.replace(hour=end_h), 0),
                (base.replace(hour=end_h), base + timedelta(days=1), 1)]
        for a, b, up in segs:
            a, b = a.timestamp(), min(b.timestamp(), now)
            if a < b:
                out.append({"model": model, "up": up, "start_ts": a, "end_ts": b, "open": 0})
    return out


class PredictTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now().replace(hour=13, minute=30, second=0, microsecond=0).timestamp()
        self.ivs = daily_outage_intervals("a", 21, self.now)
        self.profiles = predict.build(self.ivs, cfg(), self.now)

    def test_learns_daily_outage(self):
        p = self.profiles["a"]
        at = lambda h: datetime.fromtimestamp(self.now).replace(hour=h, minute=30).timestamp()
        self.assertLess(p.p_at(at(15)), 0.2)
        self.assertGreater(p.p_at(at(10)), 0.9)

    def test_window_sees_upcoming_outage(self):
        self.assertLess(predict.window_score(self.profiles["a"], self.now, True, 30), 0.5)
        early = self.now - 3 * 3600
        self.assertGreater(predict.window_score(self.profiles["a"], early, True, 30), 0.9)


class SelectorTest(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg()
        self.cfg["priority"] = ["a", "b"]
        self.now = datetime.now().replace(hour=13, minute=30, second=0, microsecond=0).timestamp()
        ivs = daily_outage_intervals("a", 21, self.now) + [
            {"model": "b", "up": 1, "start_ts": self.now - 21 * 86400, "end_ts": self.now, "open": 1}]
        self.profiles = predict.build(ivs, self.cfg, self.now)

    def views(self, a_up=True, b_up=True, b_since=None):
        since = self.now - 86400 if b_since is None else b_since
        return [selector.ModelView("a", "chat", a_up, self.now - 3600, self.profiles["a"]),
                selector.ModelView("b", "chat", b_up, since, self.profiles["b"])]

    def test_switches_ahead_of_predicted_outage(self):
        d = selector.decide("a", self.views(), self.cfg, self.now, last_switch=0)
        self.assertEqual((d.model, d.reason), ("b", "predicted"))

    def test_stays_on_preferred_model_when_no_outage_expected(self):
        early = self.now - 3 * 3600
        self.assertIsNone(selector.decide("a", self.views(), self.cfg, early, last_switch=0))

    def test_immediate_switch_when_down_even_in_cooldown(self):
        d = selector.decide("a", self.views(a_up=False), self.cfg, self.now, last_switch=self.now - 10)
        self.assertEqual((d.model, d.reason), ("b", "down"))

    def test_cooldown_blocks_proactive(self):
        self.assertIsNone(selector.decide("a", self.views(), self.cfg, self.now, last_switch=self.now - 10))

    def test_recently_recovered_model_must_be_stable(self):
        self.assertIsNone(selector.decide("a", self.views(b_since=self.now - 60), self.cfg, self.now, last_switch=0))

    def test_pin(self):
        d = selector.decide("a", self.views(), self.cfg, self.now - 3 * 3600, last_switch=0, pinned="b")
        self.assertEqual((d.model, d.reason), ("b", "pinned"))
        self.assertIsNone(selector.decide("b", self.views(), self.cfg, self.now, last_switch=0, pinned="b"))

    def test_nothing_up(self):
        self.assertIsNone(selector.decide("a", self.views(False, False), self.cfg, self.now, last_switch=0))


class DBStatsTest(unittest.TestCase):
    def test_intervals_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d) / "s.db")
            gap = 1000
            for ts, up in [(0, True), (60, True), (120, False), (180, False), (240, True), (5000, True)]:
                db.record_status("m", up, ts, gap)
            ivs = db.intervals("m")
            self.assertEqual([(i["up"], i["start_ts"], i["end_ts"], i["open"]) for i in ivs],
                             [(1, 0, 120, 0), (0, 120, 240, 0), (1, 240, 240, 0), (1, 5000, 5000, 1)])
            s = stats.summarize(ivs, since=0, now=5000)
            self.assertEqual((s["up_seconds"], s["down_seconds"], s["outages"]), (120, 120, 1))
            self.assertAlmostEqual(s["availability"], 0.5)
            db.add_probe(1.0, ProbeResult("m", "chat", "ok", 10))
            self.assertEqual(db.last_probe("m")["status"], "ok")

    def test_parse_period(self):
        self.assertEqual(stats.parse_period("2d"), 172800)
        self.assertEqual(stats.fmt_duration(3725), "1h02m")


if __name__ == "__main__":
    unittest.main()
