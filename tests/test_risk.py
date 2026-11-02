import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from risk import Gate


class T(unittest.TestCase):
    def test_emergency_blocks(self):
        self.assertIn("EMERGENCY", Gate(emergency=True).check())

    def test_daily_limit(self):
        g = Gate(max_trades_day=2)
        for _ in range(2):
            self.assertIsNone(g.check()); g.passed()
        self.assertIn("DAILY TRADE LIMIT", g.check())

    def test_day_rollover_resets(self):
        d = ["a"]
        g = Gate(max_trades_day=1, today=lambda: d[0])
        g.passed()
        self.assertIsNotNone(g.check())
        d[0] = "b"
        self.assertIsNone(g.check())

    def test_cooldown(self):
        t = [100.0]
        g = Gate(cooldown=30, clock=lambda: t[0])
        g.passed()
        self.assertIn("COOLDOWN", g.check())
        t[0] = 131.0
        self.assertIsNone(g.check())

    def test_off_means_off(self):
        g = Gate(max_trades_day=0, cooldown=0)
        for _ in range(100):
            self.assertIsNone(g.check()); g.passed()

    def test_failed_old_rules_not_counted(self):
        g = Gate(max_trades_day=1)
        self.assertIsNone(g.check())
        self.assertIsNone(g.check())


if __name__ == "__main__":
    unittest.main()
