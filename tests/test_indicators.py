import math, os, random, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import indicators as I

NOW = 1_000_000


def mk(closes, gran=60, jitter=0.0, seed=1):
    rnd = random.Random(seed)
    out, prev = [], closes[0]
    n = len(closes)
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) + abs(rnd.gauss(0, jitter))
        lo = min(o, c) - abs(rnd.gauss(0, jitter))
        out.append({"t": NOW - (n - 1 - i) * gran, "o": o, "h": hi, "l": lo, "c": c})
        prev = c
    return out


class T(unittest.TestCase):
    def test_rsi_extremes(self):
        self.assertEqual(I.rsi([float(i) for i in range(30)]), 100.0)
        self.assertLess(I.rsi([float(30 - i) for i in range(30)]), 1)

    def test_ema_constant(self):
        self.assertAlmostEqual(I.ema([5.0] * 40, 9), 5.0)

    def test_macd_needs_data(self):
        self.assertIsNone(I.macd([1.0] * 20))

    def test_too_few_candles(self):
        r = I.analyze(mk([1.0] * 30), now=NOW)
        self.assertEqual(r["signal"], "WAIT"); self.assertIn("INSUFFICIENT DATA", r["action"])

    def test_stale_data(self):
        r = I.analyze(mk([100 + i * 0.1 for i in range(120)]), now=NOW + 5000)
        self.assertEqual(r["signal"], "WAIT"); self.assertIn("STALE", r["action"])

    def test_bad_input_no_crash(self):
        self.assertEqual(I.analyze(None)["signal"], "WAIT")
        self.assertEqual(I.analyze([{"x": 1}] * 80, now=NOW)["signal"], "WAIT")

    def test_flat_market_waits(self):
        r = I.analyze(mk([100.0] * 120, jitter=0.01), now=NOW)
        self.assertEqual(r["signal"], "WAIT")

    def test_chop_waits(self):
        rnd = random.Random(3)
        v = [100 + math.sin(i / 2) * 0.5 + rnd.gauss(0, 0.05) for i in range(150)]
        self.assertEqual(I.analyze(mk(v, jitter=0.05), now=NOW)["signal"], "WAIT")

    def test_clean_trends_can_signal_and_never_wrong_way(self):
        rnd = random.Random(5)
        up = [100.0]
        for _ in range(149):
            up.append(up[-1] + 0.1 + rnd.gauss(0, 0.05))
        r = I.analyze(mk(up, jitter=0.02), now=NOW)
        self.assertIn(r["signal"], ("BUY", "WAIT")); self.assertNotEqual(r["signal"], "SELL")
        dn = [200 - (x - 100) for x in up]
        r = I.analyze(mk(dn, jitter=0.02), now=NOW)
        self.assertIn(r["signal"], ("SELL", "WAIT")); self.assertNotEqual(r["signal"], "BUY")

    def test_buy_can_fire_on_noisy_uptrend(self):
        rnd = random.Random(161); v = [100.0]
        for _ in range(149):
            v.append(v[-1] + 0.06 + rnd.gauss(0, 0.12))
        r = I.analyze(mk(v, jitter=0.04, seed=161), now=NOW)
        self.assertEqual(r["signal"], "BUY"); self.assertGreaterEqual(r["strength"], I.MIN_STRENGTH)

    def test_signal_rate_on_random_walk_is_low(self):
        sig = tot = 0
        for seed in range(40):
            rnd = random.Random(seed); v = [100.0]
            for _ in range(149):
                v.append(v[-1] + rnd.gauss(0, 0.1))
            tot += 1
            sig += I.analyze(mk(v, jitter=0.05, seed=seed), now=NOW)["signal"] != "WAIT"
        self.assertLess(sig / tot, 0.4)


if __name__ == "__main__":
    unittest.main()
