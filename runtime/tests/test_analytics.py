import unittest

from app.analytics import calculate_risk_lines, evaluate_discipline, market_risk, policy_for


class RiskModelTests(unittest.TestCase):
    def test_cn_etf_policy_is_market_specific(self):
        policy = policy_for("CN", "ETF")
        self.assertEqual(policy.stop_loss_pct, 0.08)
        self.assertEqual(policy.take_profit_pct, 0.15)

    def test_trailing_stop_tightens_fixed_stop(self):
        lines = calculate_risk_lines(100, 106, 120, "US", "EQUITY")
        self.assertEqual(lines["stop_loss"], 110.4)
        self.assertEqual(lines["take_profit"], 120.0)

    def test_red_line_has_priority(self):
        result = evaluate_discipline(90, {"stop_loss": 92, "take_profit": 120, "buy_watch": 96})
        self.assertEqual(result["action"], "SELL_REVIEW")

    def test_green_line_is_research_not_automatic_order(self):
        result = evaluate_discipline(95, {"stop_loss": 90, "take_profit": 120, "buy_watch": 96})
        self.assertEqual(result["action"], "BUY_RESEARCH")

    def test_market_risk_drawdown(self):
        risk = market_risk([100, 110, 99, 102])
        self.assertAlmostEqual(risk["max_drawdown"], -0.1)


if __name__ == "__main__":
    unittest.main()
