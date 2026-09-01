import math
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from app import strategy_evolution
from app.db import connect, initialize


def synthetic_data(rows=560):
    dates = [(date(2024, 1, 1) + timedelta(days=index)).isoformat()
             for index in range(rows)]
    bars = {}
    for symbol, start, drift, phase in (
        ("000001", 100.0, 0.0008, 0.0),
        ("000002", 80.0, 0.0005, 1.8),
        ("000003", 60.0, 0.0003, 3.2),
    ):
        series = []
        previous = start
        for index, day in enumerate(dates):
            close = start * ((1 + drift) ** index) * (1 + 0.018 * math.sin(index / 17 + phase))
            open_price = previous * (1 + 0.001 * math.sin(index / 7 + phase))
            series.append({
                "trade_date": day, "open": open_price,
                "high": max(open_price, close) * 1.01,
                "low": min(open_price, close) * 0.99,
                "close": close, "volume": 10_000_000, "amount": close * 10_000_000,
            })
            previous = close
        bars[symbol] = series
    return {
        "dates": dates, "bars": bars,
        "closes": {symbol: [float(row["close"]) for row in series]
                   for symbol, series in bars.items()},
        "symbols": list(bars), "data_start": dates[0], "data_end": dates[-1],
        "rows": rows,
    }


def normalized_mandate(iterations=2):
    return {
        "name": "测试组合", "capital": 100000.0, "horizon_months": 12,
        "target_return_pct": 10.0, "max_drawdown_pct": 20.0,
        "sectors": ["测试"],
        "universe": [
            {"input": symbol, "symbol": symbol, "name": symbol}
            for symbol in ("000001", "000002", "000003")
        ],
        "max_positions": 2, "take_profit_mode": "trailing",
        "max_iterations": iterations,
        "execution": dict(strategy_evolution.DEFAULT_EXECUTION),
        "target_semantics": "soft_objective_not_guarantee",
        "risk_semantics": "hard_maximum_peak_to_trough_drawdown",
    }


class StrategyEvolutionTests(unittest.TestCase):
    def test_half_year_folds_step_by_126_days_and_end_at_latest_bar(self):
        data = synthetic_data(rows=756)
        validation, holdout = strategy_evolution._folds(data, normalized_mandate())
        self.assertEqual(len(validation), 3)
        self.assertTrue(all(end - start + 1 == 126 for start, end in validation))
        self.assertEqual(holdout[1] - holdout[0] + 1, 126)
        self.assertEqual(holdout[1], 755)
        self.assertEqual(validation[-1][1] + 1, holdout[0])

    def test_legacy_experiment_cannot_bypass_new_non_regression_gate(self):
        legacy = {"strategies": [{
            "validation": {"feasible": True},
            "holdout": {"metrics": {"risk_pass": True}},
        }] * 3}
        self.assertFalse(strategy_evolution._activation_eligible(legacy))

    def test_normalize_mandate_freezes_risk_and_execution_contract(self):
        with patch("app.stock_compare.resolve_stock",
                   side_effect=lambda value: {"input": value, "symbol": value, "name": value}):
            mandate = strategy_evolution.normalize_mandate({
                "stocks": ["000001", "000002"], "capital": 50000,
                "horizon_months": 18, "target_return_pct": 25,
                "max_drawdown_pct": 15, "max_positions": 1,
            })
        self.assertEqual(mandate["target_semantics"], "soft_objective_not_guarantee")
        self.assertEqual(mandate["risk_semantics"], "hard_maximum_peak_to_trough_drawdown")
        self.assertEqual(mandate["execution"]["sell_stamp_tax_rate"], 0.0005)
        self.assertTrue(mandate["execution"]["t_plus_one"])

    def test_simulator_blocks_limit_up_entry_and_accounts_for_costs(self):
        data = synthetic_data()
        mandate = normalized_mandate()
        params = strategy_evolution._candidate_parameters("aggressive", mandate, 0)
        start = 220
        previous_close = data["bars"]["000001"][start - 1]["close"]
        data["bars"]["000001"][start]["open"] = previous_close * 1.10
        simulation = strategy_evolution.simulate_portfolio(data, mandate, params, start, 360)
        blocked = [item for item in simulation["trades"]
                   if item.get("reason") == "LIMIT_UP_APPROXIMATION"]
        self.assertTrue(blocked)
        self.assertGreater(simulation["metrics"]["transaction_costs"], 0)
        self.assertIn("risk_pass", simulation["metrics"])
        self.assertFalse(simulation.get("order_execution", False))

    def test_saved_preferences_change_the_backend_signal_score(self):
        data = synthetic_data()
        base = strategy_evolution._candidate_parameters(
            "balanced", normalized_mandate(), 0
        )
        trend = dict(base, preference_weights={
            "trend": 1.0, "fundamental": 0.0, "probability": 0.0,
            "liquidity": 0.0, "stability": 0.0,
        }, style_multipliers={})
        stable = dict(base, preference_weights={
            "trend": 0.0, "fundamental": 0.0, "probability": 0.0,
            "liquidity": 0.0, "stability": 1.0,
        }, style_multipliers={})
        trend_signal = strategy_evolution._signal(data, "000001", 500, trend)
        stable_signal = strategy_evolution._signal(data, "000001", 500, stable)
        self.assertNotEqual(trend_signal["score"], stable_signal["score"])
        self.assertEqual(trend_signal["preference_weights"]["trend"], 1.0)

    def test_evolution_persists_candidates_walk_forward_and_holdout(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            lake = Path(folder) / "lake"
            initialize(db_path)
            with patch.multiple(
                strategy_evolution,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
                DATA_LAKE=lake,
                _load_aligned_universe=lambda _mandate: synthetic_data(),
            ):
                result = strategy_evolution.run_strategy_evolution(
                    normalized_mandate(iterations=2), normalized=True)
                self.assertEqual(len(result["strategies"]), 3)
                self.assertTrue(Path(result["artifact_path"]).exists())
                with closing(connect(db_path)) as conn:
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM strategy_evolution_candidates").fetchone()[0], 6)
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM strategy_simulations WHERE phase='FINAL_HOLDOUT'"
                    ).fetchone()[0], 3)
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM strategy_simulations WHERE phase='BASELINE_HOLDOUT'"
                    ).fetchone()[0], 3)
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM strategy_simulations WHERE phase LIKE 'WALK_FORWARD_%'"
                    ).fetchone()[0], 6)
                self.assertTrue(all("non_regression_pass" in item
                                    for item in result["strategies"]))
                self.assertEqual(result["data"]["rolling_window"]["requested_years"], 3)
                self.assertEqual(len(result["data"]["validation_windows"]), 1)
                self.assertEqual(
                    result["data"]["half_year_walk_forward"]["period_trading_days"], 126
                )
                self.assertTrue(
                    result["data"]["half_year_walk_forward"][
                        "selection_uses_completed_periods_only"
                    ]
                )
                history = result["strategies"][0]["walk_forward_history"]
                self.assertEqual(len(history), 2)
                self.assertEqual(history[0]["selected_iteration"], 1)
                self.assertEqual(history[0]["selection"]["completed_periods"], 0)
                self.assertEqual(history[-1]["role"], "FINAL_UNTOUCHED_HOLDOUT")
                self.assertTrue(history[-1]["opened_once_after_parameter_selection"])
                self.assertTrue(
                    result["strategies"][0]["validation"]["recursive_walk_forward"][
                        "no_future_data_in_selection"
                    ]
                )
                self.assertIn("top_k_hit_rates", result["prediction_audit"])
                self.assertEqual(set(result["prediction_audit"]["top_k_hit_rates"]),
                                 {"1d", "5d", "20d"})
                self.assertIn("per_sector", result["prediction_audit"])
                self.assertIn("per_market_regime", result["prediction_audit"])
                self.assertIn("excess_return", result["strategies"][0]["holdout"]["metrics"])
                self.assertIn("profit_loss_ratio", result["strategies"][0]["holdout"]["metrics"])

                with self.assertRaisesRegex(PermissionError, "批准人"):
                    strategy_evolution.activate_strategy_experiment(
                        result["experiment_key"], "")
                if result["activation_eligible"]:
                    version = strategy_evolution.activate_strategy_experiment(
                        result["experiment_key"], "单元测试批准人")
                    self.assertEqual(version["status"], "ACTIVE")
                    self.assertEqual(len(version["strategies"]), 3)


if __name__ == "__main__":
    unittest.main()
