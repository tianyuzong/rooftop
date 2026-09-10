import unittest
from unittest.mock import patch
from app.strategy_evolution import apply_screening_relaxation, _signal
from app.quant_portfolio import normalize_quant_request


class ScreeningRelaxationTests(unittest.TestCase):
    def params(self, level):
        return apply_screening_relaxation({"fast_window": 3, "slow_window": 10,
            "momentum_window": 10, "profile": "balanced", "prediction_floor": .50,
            "fundamental_minimum_score": -.1, "fundamental_minimum_coverage": .45,
            "risk_budget": .05, "stop_loss": .05, "max_position_pct": .45}, level)

    def test_idempotent_thresholds_leave_money_and_risk_limits_unchanged(self):
        p = self.params(50)
        self.assertEqual(p["prediction_floor"], .425)
        self.assertEqual(apply_screening_relaxation(p, 50), p)
        self.assertEqual(apply_screening_relaxation(p, 0)["prediction_floor"], .50)
        self.assertEqual((p["risk_budget"], p["stop_loss"], p["max_position_pct"]), (.05, .05, .45))
        for value in (-1, 101, float("nan")):
            with self.assertRaises(ValueError): self.params(value)

    def test_relaxes_weak_signals_without_allowing_missing_financial_evidence(self):
        closes = [100 - i * .2 for i in range(30)]
        data = {"closes": {"600001": closes}, "dates": list(map(str, range(30))),
                "prediction_scores": {"600001": {"29": .44}},
                "fundamental_timelines": {"has_data": True}}
        financial = {"eligible": False, "score": -.15, "coverage": .8,
                     "minimum_score": -.1, "minimum_coverage": .45}
        with patch("app.fundamentals.fundamental_snapshot", return_value=financial):
            self.assertFalse(_signal(data, "600001", 30, self.params(0))["eligible"])
            self.assertTrue(_signal(data, "600001", 30, self.params(50))["eligible"])
            self.assertTrue(_signal(data, "600001", 30, self.params(100))["eligible"])
            financial["coverage"] = .1
            self.assertFalse(_signal(data, "600001", 30, self.params(100))["eligible"])
            financial.update(coverage=.8, score=None)
            self.assertFalse(_signal(data, "600001", 30, self.params(100))["eligible"])

    def test_request_preserves_explicit_level_and_old_default(self):
        source = {"stocks": ["600001", "600002"]}
        self.assertEqual(normalize_quant_request(source)["screening_relaxation"], 0)
        self.assertEqual(normalize_quant_request(dict(source, screening_relaxation=80))["screening_relaxation"], 80)
