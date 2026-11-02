import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import edge


class T(unittest.TestCase):
    def test_breakeven_from_real_quote(self):
        self.assertEqual(edge.breakeven_pct(1.0, 1.95), 51.28)
        self.assertIsNone(edge.breakeven_pct(1.0, 0.9))
        self.assertIsNone(edge.breakeven_pct("x", 2))

    def test_payout_floor(self):
        self.assertTrue(edge.payout_ok(1.0, 1.95, 80)[0])
        ok, msg = edge.payout_ok(1.0, 1.5, 80)
        self.assertFalse(ok)
        self.assertIn("floor", msg)
        self.assertTrue(edge.payout_ok(1.0, 1.5, 0)[0])
        self.assertFalse(edge.payout_ok(1.0, None, 0)[0])

    def test_wilson_is_below_observed_and_tightens(self):
        self.assertLess(edge.wilson_lower(60, 100), .6)
        self.assertGreater(edge.wilson_lower(600, 1000), edge.wilson_lower(60, 100))
        self.assertEqual(edge.wilson_lower(0, 0), 0.0)

    def test_report_drawdown_and_streak(self):
        t = [{"symbol": "R_10", "contract": "even", "win": w, "profit": 0.95 if w else -1, "be": 51.3}
             for w in [1, 1, 0, 0, 0, 1]]
        r = edge.report(t)["all"]
        self.assertEqual(r["worst_streak"], 3)
        self.assertEqual(r["max_drawdown"], 3.0)
        self.assertEqual(r["verdict"], "TOO_FEW")

    def test_report_losing_and_edge(self):
        lose = [{"symbol": "a", "contract": "c", "win": i % 2 == 0 and i % 4 != 0, "profit": 0, "be": 51.3} for i in range(200)]
        self.assertEqual(edge.report(lose)["all"]["verdict"], "LOSING")
        good = [{"symbol": "a", "contract": "c", "win": i % 10 < 7, "profit": 0, "be": 51.3} for i in range(400)]
        self.assertEqual(edge.report(good)["all"]["verdict"], "EDGE")

    def test_bad_rows_ignored(self):
        self.assertEqual(edge.report("nope")["all"]["n"], 0)
        self.assertEqual(edge.report([{"win": 1, "profit": "x"}, 5])["all"]["n"], 0)


if __name__ == "__main__":
    unittest.main()
