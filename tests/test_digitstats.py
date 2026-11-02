import os, random, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import digitstats as ds


class T(unittest.TestCase):
    def test_last_digit_keeps_trailing_zero(self):
        self.assertEqual(ds.last_digit(1234.5, 2), 0)
        self.assertEqual(ds.last_digit(99.87, 2), 7)

    def test_too_few_ticks_learning(self):
        r = ds.analyze([1] * 100)
        self.assertFalse(r["ok"])
        self.assertEqual(r["verdict"], "LEARNING")

    def test_uniform_cycle_no_bias(self):
        r = ds.analyze([i % 10 for i in range(1000)])
        self.assertEqual(r["verdict"], "NONE")
        self.assertFalse(r["uneven"])

    def test_even_heavy_detected(self):
        rnd = random.Random(1)
        d = [rnd.choice([0, 2, 4, 6, 8]) if rnd.random() < .7 else rnd.choice([1, 3, 5, 7, 9])
             for _ in range(1000)]
        r = ds.analyze(d)
        self.assertEqual(r["verdict"], "BIAS")
        self.assertEqual(r["leads"][0]["side"], "even")

    def test_fading_lead_waits(self):
        d = [0, 2, 4, 6, 8] * 140 + [1, 3, 5, 7, 9] * 8   # old even skew, odd now
        r = ds.analyze(d)
        self.assertIn(r["verdict"], ("FADING", "NONE"))
        self.assertNotEqual(r["verdict"], "BIAS")

    def test_zscore(self):
        self.assertAlmostEqual(ds.zscore(550, 1000, .5), 3.162, 2)
        self.assertEqual(ds.zscore(1, 0, .5), 0.0)

    def test_adjusted_z_is_stricter(self):
        self.assertGreater(ds.z_adj(), 2.5)
        self.assertLess(ds.z_adj(1), 1.7)

    def test_random_like_data_rarely_biased(self):
        bias = 0
        for seed in range(40):
            rnd = random.Random(seed)
            if ds.analyze([rnd.randrange(10) for _ in range(1000)])["verdict"] == "BIAS":
                bias += 1
        self.assertLessEqual(bias, 4)

    def test_need_win_above_fair(self):
        self.assertEqual(ds.need_win(0.5), 52.6)
        self.assertIsNone(ds.need_win(0))

    def test_real_verdict(self):
        self.assertEqual(ds.real_verdict(30, 50, 52.6)["state"], "TOO_FEW")
        self.assertEqual(ds.real_verdict(40, 200, 52.6)["state"], "BELOW")
        self.assertEqual(ds.real_verdict(105, 200, 52.6)["state"], "UNPROVEN")
        self.assertEqual(ds.real_verdict(130, 200, 52.6)["state"], "ABOVE")


if __name__ == "__main__":
    unittest.main()
