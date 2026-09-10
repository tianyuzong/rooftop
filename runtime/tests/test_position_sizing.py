import math
import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

from app import quant_portfolio as quant
from app import strategy_evolution as strategy


class PositionSizingTests(unittest.TestCase):
    def mandate(self, count=6):
        return dict(capital=100000, max_positions=count, max_drawdown_pct=10,
                    target_return_pct=10, horizon_months=48,
                    stop_loss_pct=8, take_profit_pct=10, trailing_stop_pct=8,
                    take_profit_mode="trailing", execution=dict(strategy.DEFAULT_EXECUTION))

    def row(self, symbol="a", price=10, score=1, volatility=.2):
        return dict(symbol=symbol, reference_price=price,
                    composite_score=score, annual_volatility=volatility)

    def test_count_does_not_change_profile_concentration_limit(self):
        for profile, cap in [("aggressive", .60), ("balanced", .45), ("conservative", .35)]:
            for count in [1, 2, 6, 8]:
                with self.subTest(profile=profile, count=count):
                    params = strategy._candidate_parameters(profile, self.mandate(count), 0)
                    self.assertEqual(params["max_position_pct"], cap)
                    self.assertEqual(params["max_positions"], count)

    def test_expensive_single_candidate_can_buy_one_lot_with_six_stock_limit(self):
        row = self.row(price=412.22)
        weights = strategy._position_weights([row], 100000, 100, 6, .6)
        self.assertGreater(weights["a"], 1 / 6)
        shares = math.floor(100000 * weights["a"] / 412.22 / 100) * 100
        self.assertEqual(shares, 100)
        self.assertAlmostEqual(shares * 412.22, 41222)

    def test_score_and_volatility_produce_unequal_allocations(self):
        cases = [
            [self.row("a", score=2), self.row("b", score=1)],
            [self.row("a", volatility=.1), self.row("b", volatility=.4)],
        ]
        for rows in cases:
            weights = strategy._position_weights(rows, 100000, 100, 6, .6)
            self.assertGreater(weights["a"], weights["b"])
            self.assertAlmostEqual(sum(weights.values()), .95)
            self.assertLessEqual(max(weights.values()), .6)
            self.assertEqual(weights, strategy._position_weights(rows, 100000, 100, 2, .6))

    def test_count_limit_skips_unaffordable_candidate_and_respects_available_cash(self):
        rows = [self.row("too_expensive", 900), self.row("a", 200, 2),
                self.row("b", 100, 1), self.row("c", 10, 0)]
        weights = strategy._position_weights(rows, 100000, 100, 2, .6, .4)
        self.assertEqual(set(weights), {"a", "b"})
        self.assertLessEqual(sum(weights.values()), .4 + 1e-12)
        self.assertGreaterEqual(weights["a"], .2)
        self.assertGreaterEqual(weights["b"], .1)

    def test_whole_lots_and_total_budget_remain_bounded(self):
        rows = [self.row(str(i), price, i, .1 + i * .1)
                for i, price in enumerate([412.22, 20.5, 78.33, 1.23, 1000])]
        for capital in [1000, 10000, 100000, 1000000]:
            for cap in [.25, .35, .45, .6]:
                for limit in [1, 2, 6]:
                    weights = strategy._position_weights(rows, capital, 100, limit, cap)
                    amounts = []
                    for row in rows:
                        if row["symbol"] not in weights:
                            continue
                        weight = weights[row["symbol"]]
                        shares = math.floor(capital * weight / row["reference_price"] / 100 + 1e-9) * 100
                        self.assertGreaterEqual(shares, 100)
                        self.assertLessEqual(shares * row["reference_price"], capital * cap + 1e-7)
                        amounts.append(shares * row["reference_price"])
                    self.assertLessEqual(len(amounts), limit)
                    self.assertLessEqual(sum(amounts), capital * .95 + 1e-7)

    def test_current_recommendation_uses_independent_cap_and_variable_weights(self):
        mandate = self.mandate()
        params = strategy._candidate_parameters("aggressive", mandate, 0)
        selected = dict(profile="aggressive", label="激进", parameters=params)
        rows = [self.row("a", 412.22, 2, .1), self.row("b", 10, 1, .4)]
        data = dict(symbols=[r["symbol"] for r in rows], dates=["2026-09-09"],
                    closes={r["symbol"]: [r["reference_price"]] for r in rows},
                    data_start="2026-09-09", data_end="2026-09-09", rows=1, raw_rows=1)
        signals = {r["symbol"]: dict(eligible=True, score=r["composite_score"],
                                     annual_volatility=r["annual_volatility"]) for r in rows}
        with patch.object(quant, "_load_aligned_universe", return_value=data), \
                patch.object(quant, "_public_information_coverage", return_value={}), \
                patch.object(quant, "_feature_profile", return_value={}), \
                patch.object(quant, "_signal", side_effect=lambda _d, s, _i, _p: signals[s]):
            result = quant._current_portfolio(mandate, selected,
                                             [dict(symbol=r["symbol"], name=r["symbol"]) for r in rows],
                                             use_prediction_model=False)
        amounts = {r["symbol"]: r["amount"] for r in result["positions"]}
        self.assertEqual(amounts["a"], 41222)
        self.assertGreater(amounts["a"], amounts["b"])
        self.assertGreater(result["cash_amount"], 0)
        self.assertAlmostEqual(sum(amounts.values()) + result["cash_amount"], 100000)

    def test_legacy_published_allocation_is_recomputed_without_rewriting_history(self):
        old_result = {"recommendation": {"parameters": {"max_positions": 6, "max_position_pct": .1667}}}
        stored_json = quant._dump(old_result)
        conn = Mock()
        conn.execute.return_value.fetchone.side_effect = [
            None, {"version_key": "legacy-version", "result_json": stored_json}
        ]
        mandate = dict(id=1, mandate_key="mandate", name="test", status="ACTIVE",
                       input_json=quant._dump(self.mandate()), updated_at="now")
        new_result = {"version": {"status": "SNAPSHOT", "version_key": "new-snapshot"},
                      "recommendation": {"data_asof": "2026-09-09", "positions": [],
                                         "parameters": {"max_position_pct": .6}}}
        with patch.object(quant, "_latest_trading_day_snapshot_result", return_value=new_result):
            result = quant._published_decision(conn, mandate)
        self.assertTrue(result["allocation_policy_updated"])
        self.assertEqual(result["version"]["status"], "SNAPSHOT")
        self.assertEqual(result["result"]["recommendation"]["parameters"]["max_position_pct"], .6)
        self.assertEqual(result["superseded_active_version"], "legacy-version")
        self.assertEqual(quant._dump(old_result), stored_json)
        self.assertTrue(all(call.args[0].lstrip().startswith("SELECT") for call in conn.execute.call_args_list))

    def test_simulation_uses_variable_entry_budgets(self):
        mandate = self.mandate()
        params = strategy._candidate_parameters("aggressive", mandate, 0)
        dates = [(date(2026, 7, 1) + timedelta(days=i)).isoformat() for i in range(70)]
        bars = {symbol: [dict(trade_date=day, open=10, close=10, high=10, low=10,
                              volume=10000000, amount=100000000) for day in dates]
                for symbol in ["a", "b"]}
        data = dict(dates=dates, symbols=["a", "b"], bars=bars,
                    closes={s: [10] * len(dates) for s in bars})
        with patch.object(strategy, "_signal", side_effect=lambda _d, symbol, _i, _p:
                          dict(eligible=True, score=2 if symbol == "a" else 1, annual_volatility=.2)):
            result = strategy.simulate_portfolio(data, mandate, params, 42, 69)
        buys = {trade["symbol"]: trade for trade in result["trades"] if trade["side"] == "BUY"}
        self.assertEqual(set(buys), {"a", "b"})
        self.assertGreater(buys["a"]["shares"], buys["b"]["shares"])
        self.assertGreater(result["curve"][-1]["cash"], 0)


if __name__ == "__main__":
    unittest.main()
