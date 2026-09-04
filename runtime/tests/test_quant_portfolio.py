import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import quant_portfolio, strategy_evolution
from app.db import connect, initialize


class QuantPortfolioTests(unittest.TestCase):
    def test_rule_snapshot_history_policy_keeps_formal_threshold_intact(self):
        self.assertEqual(quant_portfolio.RULE_SNAPSHOT_MINIMUM_HISTORY_DAYS, 252)
        self.assertEqual(quant_portfolio.RULE_SNAPSHOT_STANDARD_HISTORY_DAYS, 420)

    def test_methodology_exposes_half_year_recursive_cross_validation(self):
        rolling = quant_portfolio.recommendation_methodology_payload()["rolling_backtest"]
        self.assertEqual(rolling["period_trading_days"], 126)
        self.assertTrue(rolling["final_period_is_untouched"])
        self.assertIn("每126个交易日", rolling["split"])

    def test_risk_profile_defaults_to_balanced_but_auto_remains_compatible(self):
        request = self._request()
        request.pop("risk_profile")
        self.assertEqual(quant_portfolio.normalize_quant_request(request)["risk_profile"], "balanced")
        self.assertEqual(
            quant_portfolio.normalize_quant_request(self._request(risk_profile="auto"))["risk_profile"],
            "auto",
        )

    @staticmethod
    def _request(**overrides):
        request = {
            "name": "盘后组合", "capital": 100000, "horizon_months": 12,
            "target_return_pct": 20, "max_drawdown_pct": 15,
            "stop_loss_pct": 8, "take_profit_pct": 20,
            "trailing_stop_pct": 8, "sectors": "白酒,新能源",
            "max_positions": 2, "max_candidates": 12,
            "max_iterations": 10, "risk_profile": "auto",
            "take_profit_mode": "trailing",
        }
        request.update(overrides)
        return request

    @staticmethod
    def _published_result(run_key, label, data_asof):
        return {
            "status": "SUCCESS", "run_key": run_key,
            "data": {"end": data_asof},
            "recommendation": {
                "decision_label": label, "profile_label": "保守",
                "positions": [], "expectation": {"p50": 0.05},
                "data_asof": data_asof,
            },
        }

    @staticmethod
    def _strategy(profile, p50, risk_pass=True, non_regression=True,
                  drawdown=-0.1, sharpe=0.5):
        return {
            "profile": profile,
            "expectation": {"p50": p50},
            "non_regression_pass": non_regression,
            "holdout": {"metrics": {
                "risk_pass": risk_pass,
                "max_drawdown": drawdown,
                "sharpe_ratio": sharpe,
            }},
        }

    def test_auto_strategy_uses_closest_feasible_return_when_target_is_missed(self):
        strategies = [
            self._strategy("aggressive", 0.16, drawdown=-0.14),
            self._strategy("balanced", 0.11, drawdown=-0.08),
            self._strategy("conservative", 0.04, drawdown=-0.03),
        ]
        selected, decision = quant_portfolio._choose_strategy(strategies, "auto", 0.20)
        self.assertEqual(selected["profile"], "aggressive")
        self.assertEqual(decision["status"], "CLOSEST_FEASIBLE")
        self.assertFalse(decision["target_met"])
        self.assertAlmostEqual(decision["target_gap"], -0.04)

    def test_auto_strategy_does_not_call_risk_breach_a_feasible_recommendation(self):
        strategies = [
            self._strategy("aggressive", 0.19, risk_pass=False),
            self._strategy("balanced", 0.12, risk_pass=False),
            self._strategy("conservative", 0.03, risk_pass=False),
        ]
        selected, decision = quant_portfolio._choose_strategy(strategies, "auto", 0.20)
        self.assertEqual(selected["profile"], "aggressive")
        self.assertEqual(decision["status"], "NO_RISK_FEASIBLE")
        self.assertFalse(decision["risk_constraints_met"])
        self.assertFalse(decision["publish_gate_passed"])

    def test_non_regression_failure_is_visible_as_research_target(self):
        strategies = [
            self._strategy(
                "aggressive", 0.12, risk_pass=True, non_regression=False,
                drawdown=-0.14,
            ),
        ]
        selected, decision = quant_portfolio._choose_strategy(
            strategies, "aggressive", 0.02,
        )
        self.assertEqual(selected["profile"], "aggressive")
        self.assertEqual(decision["status"], "RESEARCH_TARGET_MET")
        self.assertTrue(decision["risk_constraints_met"])
        self.assertFalse(decision["non_regression_met"])
        self.assertFalse(decision["publish_gate_passed"])
        self.assertTrue(decision["potential_target_met"])
        self.assertFalse(decision["target_met"])

    def test_request_freezes_user_risk_limits(self):
        request = quant_portfolio.normalize_quant_request({
            "capital": 100000, "horizon_months": 12,
            "target_return_pct": 20, "max_drawdown_pct": 15,
            "stop_loss_pct": 7, "take_profit_pct": 24,
            "trailing_stop_pct": 6, "sectors": "白酒",
            "max_positions": 2, "max_candidates": 8,
        })
        self.assertEqual(request["stop_loss_pct"], 7)
        self.assertEqual(request["take_profit_pct"], 24)
        self.assertEqual(request["trailing_stop_pct"], 6)
        self.assertFalse(request["order_execution"])

    def test_custom_preferences_are_normalized_versioned_and_part_of_identity(self):
        first = quant_portfolio.normalize_quant_request(self._request(
            risk_profile="balanced", backtest_window_years=3,
            strategy_style="quality_growth",
            preference_weights={"trend": 10, "fundamental": 50,
                                "probability": 20, "liquidity": 5, "stability": 15},
        ))
        second = quant_portfolio.normalize_quant_request(self._request(
            risk_profile="balanced", backtest_window_years=3,
            strategy_style="quality_growth",
            preference_weights={"trend": 30, "fundamental": 30,
                                "probability": 20, "liquidity": 5, "stability": 15},
        ))
        self.assertAlmostEqual(sum(first["preference_weights"].values()), 1.0)
        self.assertEqual(first["backtest_window_years"], 3)
        self.assertEqual(first["strategy_style"], "quality_growth")
        self.assertNotEqual(first["preference_version"], second["preference_version"])
        self.assertNotEqual(
            quant_portfolio._mandate_identity(first),
            quant_portfolio._mandate_identity(second),
        )

    def test_registering_constraints_does_not_start_refresh_or_backtest(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            initialize(db_path)
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
            ):
                result = quant_portfolio.register_quant_mandate(self._request())
            with closing(connect(db_path)) as conn:
                mandate_count = conn.execute("SELECT COUNT(*) FROM quant_mandates").fetchone()[0]
                run_count = conn.execute("SELECT COUNT(*) FROM quant_portfolio_runs").fetchone()[0]
            self.assertEqual(result["execution"], "READ_ACTIVE_OR_INFER_LATEST_TRADING_DAY")
            self.assertEqual(result["post_close_refresh"], "DEFERRED_TO_POST_CLOSE")
            self.assertFalse(result["refresh_started"])
            self.assertFalse(result["backtest_started"])
            self.assertEqual(result["decision"]["status"], "PENDING_FIRST_POST_CLOSE")
            self.assertEqual(mandate_count, 1)
            self.assertEqual(run_count, 0)

    def test_first_mandate_uses_latest_trading_day_snapshot_without_a_run(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            initialize(db_path)
            snapshot = self._published_result("snapshot-inference-test", "快照推荐", "2026-08-28")
            snapshot["inference"] = {
                "kind": "LATEST_TRADING_DAY_SNAPSHOT",
                "model_version": "prediction-test",
                "model_training_end": "2026-08-28",
                "refresh_data": False,
                "backtest_executed": False,
            }
            snapshot["version"] = {
                "version_key": "snapshot-20260828-test",
                "status": "SNAPSHOT", "score": 0.1, "gate": {},
                "run_id": None, "created_at": "now", "activated_at": "now",
            }
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
            ), patch.object(
                quant_portfolio, "_latest_trading_day_snapshot_result",
                return_value=snapshot,
            ):
                result = quant_portfolio.register_quant_mandate(self._request())
            with closing(connect(db_path)) as conn:
                run_count = conn.execute("SELECT COUNT(*) FROM quant_portfolio_runs").fetchone()[0]
                version_count = conn.execute(
                    "SELECT COUNT(*) FROM quant_portfolio_versions"
                ).fetchone()[0]
            self.assertEqual(result["decision"]["status"], "AVAILABLE")
            self.assertEqual(result["decision"]["version"]["status"], "SNAPSHOT")
            self.assertTrue(result["snapshot_inference"])
            self.assertFalse(result["refresh_started"])
            self.assertFalse(result["backtest_started"])
            self.assertEqual(run_count, 0)
            self.assertEqual(version_count, 0)

    def test_new_daily_cache_replaces_stale_evaluated_snapshot_without_active_version(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            initialize(db_path)
            evaluated = self._published_result(
                "snapshot-inference-test", "已评测快照", "2026-08-28"
            )
            evaluated["version"] = {
                "version_key": "snapshot-20260828-test",
                "status": "SNAPSHOT", "score": 0.1, "gate": {},
                "run_id": None, "created_at": "old", "activated_at": "old",
            }
            live = self._published_result(
                "rule-snapshot-test", "当日规则快照", "2026-09-01"
            )
            live["version"] = {
                "version_key": "rule-snapshot-20260901-test",
                "status": "RULE_SNAPSHOT", "score": None, "gate": {},
                "run_id": None, "created_at": "now", "activated_at": None,
            }
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
            ), patch.object(
                quant_portfolio, "_latest_trading_day_snapshot_result",
                return_value=evaluated,
            ), patch.object(
                quant_portfolio, "_cached_candidate_asof",
                return_value="2026-09-01",
            ), patch.object(
                quant_portfolio, "_cached_rule_snapshot_result",
                return_value=live,
            ):
                result = quant_portfolio.register_quant_mandate(self._request())
            decision = result["decision"]
            self.assertEqual(decision["data_asof"], "2026-09-01")
            self.assertEqual(decision["version"]["status"], "RULE_SNAPSHOT")
            self.assertEqual(
                decision["serving_policy"],
                "LATEST_CACHED_RULE_SNAPSHOT_WHILE_MODEL_STALE",
            )

    def test_first_mandate_falls_back_to_immediate_rule_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            initialize(db_path)
            snapshot = self._published_result(
                "rule-snapshot-test", "即时多因子结果", "2026-08-28"
            )
            snapshot["inference"] = {
                "kind": "CACHED_RULE_SNAPSHOT",
                "model_version": None,
                "refresh_data": False,
                "backtest_executed": False,
            }
            snapshot["recommendation"].update({
                "positions": [],
                "research_recommendations": [],
                "research_watchlist": [{
                    "symbol": "600519", "name": "贵州茅台",
                    "shares": None, "weight": None,
                    "observation_only": True,
                }],
                "expectation": {"status": "UNAVAILABLE_NOT_VALIDATED"},
            })
            snapshot["version"] = {
                "version_key": "rule-snapshot-20260828-test",
                "status": "RULE_SNAPSHOT", "score": None,
                "gate": {"formal_signal_allowed": False},
                "run_id": None, "created_at": "now", "activated_at": None,
            }
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
            ), patch.object(
                quant_portfolio, "_latest_trading_day_snapshot_result",
                side_effect=RuntimeError("尚无活动预测模型"),
            ), patch.object(
                quant_portfolio, "_cached_rule_snapshot_result",
                return_value=snapshot,
            ):
                result = quant_portfolio.register_quant_mandate(self._request())
            self.assertEqual(result["decision"]["status"], "AVAILABLE")
            self.assertEqual(result["decision"]["version"]["status"], "RULE_SNAPSHOT")
            self.assertTrue(result["snapshot_inference"])
            recommendation = result["decision"]["result"]["recommendation"]
            self.assertEqual(recommendation["positions"], [])
            self.assertIsNone(recommendation["research_watchlist"][0]["shares"])
            self.assertEqual(
                recommendation["expectation"]["status"],
                "UNAVAILABLE_NOT_VALIDATED",
            )

    def test_rule_snapshot_watchlist_never_invents_position_sizing(self):
        ranking = [{
            "symbol": "600519", "name": "贵州茅台", "eligible": True,
            "reference_price": 1299.52, "composite_score": 0.42,
            "momentum": 0.03, "trend": 0.01, "annual_volatility": 0.2,
            "fundamental_score": 0.7, "fundamental_coverage": 0.8,
            "rejection_reasons": [],
        }]
        result = quant_portfolio._rule_snapshot_watchlist(ranking, 1)
        self.assertEqual(result[0]["status"], "RULE_PASS")
        self.assertIsNone(result[0]["shares"])
        self.assertIsNone(result[0]["weight"])
        self.assertTrue(result[0]["observation_only"])

    def test_rule_snapshot_marks_missing_fundamentals_as_data_gap(self):
        ranking = [{
            "symbol": "600519", "name": "贵州茅台", "eligible": True,
            "reference_price": 1299.52, "composite_score": 0.42,
            "momentum": 0.03, "trend": 0.01, "annual_volatility": 0.2,
            "fundamental_score": None, "fundamental_coverage": 0.0,
            "rejection_reasons": [],
        }]
        result = quant_portfolio._rule_snapshot_watchlist(ranking, 1)
        self.assertEqual(result[0]["status"], "DATA_GAP")
        self.assertIn("财务数据不足", result[0]["blockers"][0])

    def test_rule_snapshot_allocation_is_lot_aware_and_excludes_sell_risk(self):
        request = quant_portfolio.normalize_quant_request(
            self._request(max_positions=2, risk_profile="balanced")
        )

        def candidate(symbol, price, score, action, direction):
            return {
                "symbol": symbol, "name": f"股票{symbol}",
                "reference_price": price, "composite_score": score,
                "annual_volatility": 0.25,
                "timeframe_forecast": {
                    "validation_status": "WALK_FORWARD_CALIBRATED_PARTIAL",
                    "summary": {
                        "status": "AVAILABLE", "action": action,
                        "direction_score": direction,
                    },
                    "forecast_curve": [
                        {"trading_day": 0, "p10": price, "p25": price,
                         "p50": price, "p75": price, "p90": price},
                        {"trading_day": 252, "p10": price * .8,
                         "p25": price * .95, "p50": price * 1.1,
                         "p75": price * 1.2, "p90": price * 1.35},
                    ],
                },
            }

        watchlist = [
            candidate("1", 100, .9, "BUY_WATCH", .3),
            candidate("2", 50, .7, "HOLD_WATCH", .1),
            candidate("3", 20, 1.0, "SELL_REVIEW", -.5),
        ]
        result = quant_portfolio._rule_snapshot_allocations(
            watchlist, request, {"max_position_pct": .45},
        )
        self.assertEqual({item["symbol"] for item in result}, {"1", "2"})
        self.assertTrue(all(item["shares"] % 100 == 0 for item in result))
        self.assertTrue(all(item["amount"] == item["shares"] * item["reference_price"]
                            for item in result))
        self.assertLessEqual(sum(item["amount"] for item in result), request["capital"])
        self.assertTrue(all(item["research_allocation"] for item in result))
        self.assertTrue(all(not item["formal_position"] for item in result))

    def test_rule_snapshot_allocation_excludes_uncalibrated_forecast(self):
        request = quant_portfolio.normalize_quant_request(
            self._request(max_positions=1, risk_profile="balanced")
        )
        watchlist = [{
            "symbol": "1", "name": "未校准股票", "reference_price": 50,
            "composite_score": .9, "annual_volatility": .2,
            "timeframe_forecast": {
                "validation_status": "UNAVAILABLE_NOT_CALIBRATED",
                "summary": {"status": "AVAILABLE", "action": "BUY_WATCH",
                            "direction_score": .5},
                "forecast_curve": [
                    {"trading_day": 0}, {"trading_day": 21},
                ],
            },
        }]
        result = quant_portfolio._rule_snapshot_allocations(
            watchlist, request, {"max_position_pct": .45},
        )
        self.assertEqual(result, [])

    def test_rule_snapshot_allocation_accepts_labeled_baseline_scenario(self):
        request = quant_portfolio.normalize_quant_request(
            self._request(max_positions=1, risk_profile="balanced")
        )
        watchlist = [{
            "symbol": "1", "name": "基准情景股票", "reference_price": 50,
            "composite_score": .9, "annual_volatility": .2,
            "timeframe_forecast": {
                "validation_status": "BASELINE_SCENARIO_ONLY",
                "summary": {"status": "UNAVAILABLE", "action": "HOLD_WATCH",
                            "direction_score": 0},
                "forecast_curve": [
                    {"trading_day": 0, "p10": 50, "p25": 50, "p50": 50,
                     "p75": 50, "p90": 50, "basis": "CURRENT_PRICE"},
                    {"trading_day": 252, "p10": 30, "p25": 42, "p50": 55,
                     "p75": 70, "p90": 85,
                     "basis": "HISTORICAL_BASELINE_REFERENCE"},
                ],
            },
        }]
        result = quant_portfolio._rule_snapshot_allocations(
            watchlist, request, {"max_position_pct": .45},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "1")
        self.assertGreater(result[0]["amount"], 0)

    def test_rule_snapshot_portfolio_forecast_aggregates_amounts_and_cash(self):
        allocations = [
            {
                "reference_price": 100, "amount": 20000,
                "timeframe_forecast": {
                    "validation_status": "WALK_FORWARD_CALIBRATED_PARTIAL",
                    "forecast_curve": [
                    {"trading_day": 0, "p10": 100, "p25": 100,
                     "p50": 100, "p75": 100, "p90": 100},
                    {"trading_day": 21, "p10": 90, "p25": 95,
                     "p50": 110, "p75": 115, "p90": 120},
                ]},
            },
            {
                "reference_price": 50, "amount": 30000,
                "timeframe_forecast": {
                    "validation_status": "WALK_FORWARD_CALIBRATED_PARTIAL",
                    "forecast_curve": [
                    {"trading_day": 0, "p10": 50, "p25": 50,
                     "p50": 50, "p75": 50, "p90": 50},
                    {"trading_day": 21, "p10": 45, "p25": 47.5,
                     "p50": 55, "p75": 57.5, "p90": 60},
                ]},
            },
        ]
        result = quant_portfolio._rule_snapshot_portfolio_forecast(
            allocations, 100000,
        )
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["invested_amount"], 50000)
        self.assertEqual(result["cash_amount"], 50000)
        self.assertEqual(result["curve"][0]["p50"], 100000)
        self.assertEqual(result["endpoint"]["p50"], 105000)
        self.assertAlmostEqual(result["endpoint_returns"]["p50"], .05)
        self.assertEqual(result["correlation"]["status"], "UNAVAILABLE")

    def test_rule_snapshot_portfolio_forecast_preserves_historical_dependence(self):
        allocations = [
            {
                "symbol": "1", "reference_price": 100, "amount": 20000,
                "timeframe_forecast": {
                    "data_asof": "2026-08-28",
                    "validation_status": "WALK_FORWARD_CALIBRATED_PARTIAL",
                    "requested_horizon_trading_days": 504,
                    "forecast_curve": [
                        {"trading_day": 0, "p10": 100, "p25": 100,
                         "p50": 100, "p75": 100, "p90": 100},
                        {"trading_day": 21, "p10": 90, "p25": 95,
                         "p50": 105, "p75": 115, "p90": 125},
                    ],
                },
            },
            {
                "symbol": "2", "reference_price": 50, "amount": 30000,
                "timeframe_forecast": {
                    "data_asof": "2026-08-28",
                    "validation_status": "WALK_FORWARD_CALIBRATED_PARTIAL",
                    "requested_horizon_trading_days": 504,
                    "forecast_curve": [
                        {"trading_day": 0, "p10": 50, "p25": 50,
                         "p50": 50, "p75": 50, "p90": 50},
                        {"trading_day": 21, "p10": 40, "p25": 45,
                         "p50": 52, "p75": 58, "p90": 65},
                    ],
                },
            },
        ]
        copula = {
            "status": "AVAILABLE", "common_days": 120,
            "method": "test_empirical_rank_copula",
            "rows": [[.1, .1], [.25, .25], [.75, .75], [.9, .9]],
        }
        with patch.object(quant_portfolio, "_historical_copula", return_value=copula):
            result = quant_portfolio._rule_snapshot_portfolio_forecast(
                allocations, 100000, 10,
            )
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["correlation"]["status"], "AVAILABLE")
        self.assertEqual(result["correlation"]["common_daily_samples"], 120)
        self.assertEqual(result["correlation"]["simulation_count"], 2000)
        self.assertEqual(result["horizon_status"], "PARTIAL")
        self.assertEqual(result["horizon_trading_days"], 21)
        self.assertEqual(result["requested_horizon_trading_days"], 504)
        self.assertEqual(result["endpoint_probabilities"]["sample_count"], 2000)
        self.assertIsNotNone(result["endpoint_probabilities"]["profit"])
        self.assertIsNotNone(result["endpoint_probabilities"]["target"])
        self.assertEqual(result["endpoint_probabilities"]["target_return_pct"], 10)
        endpoint = result["endpoint"]
        self.assertLessEqual(endpoint["p10"], endpoint["p50"])
        self.assertLessEqual(endpoint["p50"], endpoint["p90"])

    def test_portfolio_forecast_keeps_uncalibrated_amount_static(self):
        allocations = [
            {
                "symbol": "good", "name": "已校准", "reference_price": 100,
                "amount": 20000, "timeframe_forecast": {
                    "validation_status": "WALK_FORWARD_CALIBRATED_PARTIAL",
                    "requested_horizon_trading_days": 504,
                    "forecast_curve": [
                        {"trading_day": 0, "p10": 100, "p25": 100,
                         "p50": 100, "p75": 100, "p90": 100},
                        {"trading_day": 21, "p10": 90, "p25": 95,
                         "p50": 110, "p75": 115, "p90": 120},
                    ],
                },
            },
            {
                "symbol": "bad", "name": "未校准", "reference_price": 50,
                "amount": 30000, "timeframe_forecast": {
                    "validation_status": "UNAVAILABLE_NOT_CALIBRATED",
                    "horizon_reason": "没有期限通过门禁",
                    "forecast_curve": [{"trading_day": 0}],
                },
            },
        ]
        with patch.object(
            quant_portfolio, "_historical_copula",
            return_value={"status": "UNAVAILABLE", "rows": [], "common_days": 0},
        ):
            result = quant_portfolio._rule_snapshot_portfolio_forecast(
                allocations, 100000,
            )
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["invested_amount"], 50000)
        self.assertEqual(result["modeled_invested_amount"], 20000)
        self.assertEqual(result["cash_amount"], 50000)
        self.assertEqual(result["static_amount"], 80000)
        self.assertEqual(result["curve"][0]["p50"], 100000)
        self.assertEqual(result["endpoint"]["p50"], 102000)
        self.assertEqual(result["excluded_allocations"][0]["symbol"], "bad")

    def test_portfolio_forecast_labels_historical_baseline_scenario(self):
        allocations = [{
            "symbol": "base", "name": "历史基准", "reference_price": 50,
            "amount": 30000, "timeframe_forecast": {
                "validation_status": "BASELINE_SCENARIO_ONLY",
                "requested_horizon_trading_days": 252,
                "validated_horizon_trading_days": 0,
                "forecast_curve": [
                    {"trading_day": 0, "p10": 50, "p25": 50, "p50": 50,
                     "p75": 50, "p90": 50, "basis": "CURRENT_PRICE"},
                    {"trading_day": 252, "p10": 30, "p25": 42, "p50": 55,
                     "p75": 70, "p90": 85,
                     "basis": "HISTORICAL_BASELINE_REFERENCE"},
                ],
            },
        }]
        with patch.object(
            quant_portfolio, "_historical_copula",
            return_value={
                "status": "AVAILABLE", "rows": [[.1], [.25], [.5], [.75], [.9]],
                "common_days": 120, "method": "test",
            },
        ):
            result = quant_portfolio._rule_snapshot_portfolio_forecast(
                allocations, 100000,
            )
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["horizon_status"], "REFERENCE_FULL")
        self.assertEqual(result["validated_horizon_trading_days"], 0)
        self.assertEqual(result["baseline_allocations"][0]["symbol"], "base")
        self.assertIn("HISTORICAL_BASELINE", result["validation_status"])
        self.assertIsNone(result["endpoint_probabilities"]["target"])
        self.assertEqual(
            result["endpoint_probabilities"]["reliability_status"],
            "INSUFFICIENT_INDEPENDENT_HISTORY",
        )

    def test_timeframe_forecasts_are_attached_to_snapshot_recommendations(self):
        items = [{"symbol": "600519", "name": "贵州茅台"}]
        forecast = {
            "summary": {
                "action": "BUY_WATCH", "action_label": "偏多观察",
                "sell_review": {"status": "NOT_TRIGGERED"},
            },
            "forecast_curve": [],
        }
        connection = MagicMock()
        with patch.object(
            quant_portfolio, "connect", return_value=connection,
        ), patch.object(
            quant_portfolio, "build_timeframe_forecast", return_value=forecast,
        ) as builder:
            result = quant_portfolio._attach_timeframe_forecasts(
                items, "2026-08-28", 24, "aggressive",
            )
        builder.assert_called_once_with(
            connection, "600519", "2026-08-28", 24, "aggressive",
        )
        connection.close.assert_called_once()
        self.assertIs(result[0]["timeframe_forecast"], forecast)
        self.assertEqual(result[0]["action_label"], "偏多观察")
        self.assertEqual(result[0]["sell_conclusion"]["status"], "NOT_TRIGGERED")

    def test_legacy_published_result_gets_forecast_fields_on_read(self):
        result = {
            "request": {
                "capital": 100000, "horizon_months": 24,
                "target_return_pct": 30, "risk_profile": "aggressive",
            },
            "data": {"end": "2026-09-01"},
            "recommendation": {
                "profile": "aggressive", "data_asof": "2026-09-01",
                "positions": [],
                "research_recommendations": [{
                    "symbol": "603127", "name": "昭衍新药",
                    "reference_price": 50, "amount": 20000,
                }],
                "research_watchlist": [{
                    "symbol": "603127", "name": "昭衍新药",
                    "reference_price": 50,
                }],
            },
        }
        forecast = {
            "validation_status": "WALK_FORWARD_CALIBRATED_PARTIAL",
            "requested_horizon_trading_days": 504,
            "validated_horizon_trading_days": 21,
            "summary": {
                "action": "BUY_WATCH", "action_label": "偏多观察",
                "sell_review": {"status": "NOT_TRIGGERED"},
            },
            "forecast_curve": [
                {"trading_day": 0, "p10": 50, "p25": 50, "p50": 50,
                 "p75": 50, "p90": 50},
                {"trading_day": 21, "p10": 45, "p25": 48, "p50": 55,
                 "p75": 58, "p90": 62},
            ],
        }

        def attach(items, data_asof, horizon_months, profile):
            self.assertEqual((data_asof, horizon_months, profile),
                             ("2026-09-01", 24, "aggressive"))
            items[0]["timeframe_forecast"] = forecast
            items[0]["action_signal"] = "BUY_WATCH"
            items[0]["action_label"] = "偏多观察"
            items[0]["sell_conclusion"] = {"status": "NOT_TRIGGERED"}
            return items

        with patch.object(
            quant_portfolio, "_attach_timeframe_forecasts", side_effect=attach,
        ), patch.object(
            quant_portfolio, "_historical_copula",
            return_value={"status": "UNAVAILABLE", "rows": [], "common_days": 0},
        ):
            enriched = quant_portfolio._ensure_recommendation_forecasts(result)

        recommendation = enriched["recommendation"]
        self.assertEqual(recommendation["forecast_allocations"][0]["symbol"], "603127")
        self.assertEqual(recommendation["research_watchlist"][0]["action_label"],
                         "偏多观察")
        self.assertEqual(recommendation["portfolio_forecast"]["status"], "AVAILABLE")
        self.assertEqual(recommendation["portfolio_forecast"]["endpoint"]["p50"],
                         102000)

    def test_equivalent_constraint_registration_reuses_mandate(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            initialize(db_path)
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
            ):
                first = quant_portfolio.register_quant_mandate(
                    self._request(name="第一名称", refresh_data=True)
                )
                second = quant_portfolio.register_quant_mandate(
                    self._request(name="第二名称", sectors="新能源,白酒",
                                  refresh_data=False, collect_sentiment=False)
                )
            with closing(connect(db_path)) as conn:
                mandate_count = conn.execute("SELECT COUNT(*) FROM quant_mandates").fetchone()[0]
            self.assertEqual(first["status"], "REGISTERED")
            self.assertEqual(second["status"], "EXISTING")
            self.assertEqual(first["mandate_key"], second["mandate_key"])
            self.assertEqual(mandate_count, 1)

    def test_resolving_unregistered_constraints_is_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            initialize(db_path)
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
            ):
                result = quant_portfolio.resolve_quant_decision(self._request())
            with closing(connect(db_path)) as conn:
                mandate_count = conn.execute("SELECT COUNT(*) FROM quant_mandates").fetchone()[0]
                run_count = conn.execute("SELECT COUNT(*) FROM quant_portfolio_runs").fetchone()[0]
            self.assertEqual(result["status"], "UNREGISTERED")
            self.assertEqual(mandate_count, 0)
            self.assertEqual(run_count, 0)

    def test_published_decision_stays_on_old_active_until_atomic_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            initialize(db_path)
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
            ):
                registered = quant_portfolio.register_quant_mandate(self._request())
                mandate_id = registered["decision"]["mandate"]["id"]
                old_result = self._published_result("old-run", "旧策略", "2026-08-28")
                new_result = self._published_result("new-run", "新策略", "2026-08-29")
                with closing(connect(db_path)) as conn:
                    old_run_id = conn.execute(
                        """INSERT INTO quant_portfolio_runs
                           (run_key,mandate_id,trigger_kind,status,result_json,data_asof,
                            started_at,finished_at) VALUES(?,?,'daily_post_close','SUCCESS',?,?,?,?)""",
                        ("old-run", mandate_id, quant_portfolio._dump(old_result),
                         "2026-08-28", "old-start", "old-finish"),
                    ).lastrowid
                    conn.execute(
                        """INSERT INTO quant_portfolio_versions
                           (version_key,mandate_id,run_id,status,score,gate_json,result_json,
                            created_at,activated_at) VALUES(?,?,?,'ACTIVE',1,'{}',?,?,?)""",
                        ("old-version", mandate_id, old_run_id,
                         quant_portfolio._dump(old_result), "old-created", "old-active"),
                    )
                    new_run_id = conn.execute(
                        """INSERT INTO quant_portfolio_runs
                           (run_key,mandate_id,trigger_kind,status,started_at)
                           VALUES(?,?,'daily_post_close','RUNNING',?)""",
                        ("new-run", mandate_id, "new-start"),
                    ).lastrowid
                    conn.commit()

                during_update = quant_portfolio.resolve_quant_decision(self._request())
                self.assertEqual(during_update["status"], "AVAILABLE")
                self.assertEqual(during_update["version"]["version_key"], "old-version")
                self.assertEqual(during_update["result"]["recommendation"]["decision_label"], "旧策略")
                self.assertTrue(during_update["update_pending"])
                self.assertTrue(during_update["serving_previous_version"])

                with closing(connect(db_path)) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "UPDATE quant_portfolio_versions SET status='ARCHIVED' WHERE version_key=?",
                        ("old-version",),
                    )
                    conn.execute(
                        """INSERT INTO quant_portfolio_versions
                           (version_key,mandate_id,run_id,parent_version,status,score,gate_json,
                            result_json,created_at,activated_at)
                           VALUES(?,?,?,'old-version','ACTIVE',2,'{}',?,?,?)""",
                        ("new-version", mandate_id, new_run_id,
                         quant_portfolio._dump(new_result), "new-created", "new-active"),
                    )
                    conn.execute(
                        """UPDATE quant_portfolio_runs SET status='SUCCESS',result_json=?,
                           data_asof=?,finished_at=? WHERE id=?""",
                        (quant_portfolio._dump(new_result), "2026-08-29", "new-finish", new_run_id),
                    )
                    with closing(connect(db_path)) as reader:
                        mandate_row = quant_portfolio._find_active_mandate(
                            reader, quant_portfolio.normalize_quant_request(self._request())
                        )
                        before_commit = quant_portfolio._published_decision(reader, mandate_row)
                    self.assertEqual(before_commit["version"]["version_key"], "old-version")
                    conn.commit()

                after_publish = quant_portfolio.resolve_quant_decision(self._request())
            self.assertEqual(after_publish["version"]["version_key"], "new-version")
            self.assertEqual(after_publish["result"]["recommendation"]["decision_label"], "新策略")
            self.assertFalse(after_publish["update_pending"])

    def test_newer_cached_bar_supersedes_stale_active_model_on_page(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            initialize(db_path)
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
            ):
                registered = quant_portfolio.register_quant_mandate(self._request())
                mandate_id = registered["decision"]["mandate"]["id"]
                old_result = self._published_result(
                    "old-run", "旧活动模型", "2026-08-28"
                )
                live_result = self._published_result(
                    "rule-snapshot", "当日规则快照", "2026-09-01"
                )
                live_result["version"] = {
                    "version_key": "rule-snapshot-20260901-test",
                    "status": "RULE_SNAPSHOT", "score": None,
                    "gate": {"formal_signal_allowed": False},
                    "run_id": None, "created_at": "now", "activated_at": None,
                }
                with closing(connect(db_path)) as conn:
                    run_id = conn.execute(
                        """INSERT INTO quant_portfolio_runs
                           (run_key,mandate_id,trigger_kind,status,result_json,data_asof,
                            started_at,finished_at) VALUES(?,?,'daily_post_close','SUCCESS',?,?,?,?)""",
                        ("old-run", mandate_id, quant_portfolio._dump(old_result),
                         "2026-08-28", "old-start", "old-finish"),
                    ).lastrowid
                    conn.execute(
                        """INSERT INTO quant_portfolio_versions
                           (version_key,mandate_id,run_id,status,score,gate_json,result_json,
                            created_at,activated_at) VALUES(?,?,?,'ACTIVE',1,'{}',?,?,?)""",
                        ("old-version", mandate_id, run_id,
                         quant_portfolio._dump(old_result), "old-created", "old-active"),
                    )
                    conn.commit()
                with patch.object(
                    quant_portfolio, "_cached_candidate_asof",
                    return_value="2026-09-01",
                ), patch.object(
                    quant_portfolio, "_cached_rule_snapshot_result",
                    return_value=live_result,
                ):
                    decision = quant_portfolio.resolve_quant_decision(self._request())
            self.assertEqual(decision["data_asof"], "2026-09-01")
            self.assertEqual(decision["version"]["status"], "RULE_SNAPSHOT")
            self.assertEqual(decision["superseded_active_version"], "old-version")
            self.assertTrue(decision["snapshot_inference"])
            self.assertFalse(decision["serving_previous_version"])
            self.assertEqual(
                decision["serving_policy"],
                "LATEST_CACHED_RULE_SNAPSHOT_WHILE_ACTIVE_MODEL_STALE",
            )

    def test_strategy_candidates_cannot_relax_user_stops(self):
        mandate = {
            "max_drawdown_pct": 15, "max_positions": 2,
            "take_profit_mode": "trailing", "stop_loss_pct": 7,
            "take_profit_pct": 24, "trailing_stop_pct": 6,
        }
        for iteration in range(10):
            params = strategy_evolution._candidate_parameters("balanced", mandate, iteration)
            self.assertEqual(params["stop_loss"], 0.07)
            self.assertEqual(params["take_profit"], 0.24)
            self.assertEqual(params["trailing_stop"], 0.06)

    def test_expected_return_distribution_is_deterministic(self):
        curve = []
        equity = 100000.0
        for index in range(180):
            equity *= 1.001 if index % 7 else 0.996
            curve.append({"date": f"2026-{index // 28 + 1:02d}-{index % 28 + 1:02d}",
                          "equity": equity})
        first = quant_portfolio.expected_return_distribution(curve, 12, 0.2, "same-seed")
        second = quant_portfolio.expected_return_distribution(curve, 12, 0.2, "same-seed")
        self.assertEqual(first, second)
        self.assertLessEqual(first["p10"], first["p50"])
        self.assertLessEqual(first["p50"], first["p90"])
        self.assertEqual(len(first["forecast_curve"]), 253)
        self.assertEqual(first["forecast_curve"][0]["trading_day"], 0)
        self.assertEqual(first["forecast_curve"][-1]["trading_day"], 252)
        self.assertEqual(first["forecast_curve"][-1]["p10"], first["p10"])
        self.assertEqual(first["forecast_curve"][-1]["p50"], first["p50"])
        self.assertEqual(first["forecast_curve"][-1]["p90"], first["p90"])
        for point in first["forecast_curve"]:
            self.assertLessEqual(point["p10"], point["p25"])
            self.assertLessEqual(point["p25"], point["p50"])
            self.assertLessEqual(point["p50"], point["p75"])
            self.assertLessEqual(point["p75"], point["p90"])
        self.assertFalse(first["guarantee"])

    def test_expected_return_distribution_reports_no_forecast_when_history_is_short(self):
        result = quant_portfolio.expected_return_distribution(
            [{"date": f"2026-08-{index + 1:02d}", "equity": 100000 + index}
             for index in range(10)],
            12, 0.2, "short-history",
        )
        self.assertEqual(result["forecast_curve"], [])
        self.assertEqual(result["horizon_trading_days"], 0)
        self.assertFalse(result["guarantee"])

    def test_expected_return_probability_uses_current_target(self):
        curve = []
        equity = 100000.0
        for index in range(180):
            equity *= 1.001 if index % 7 else 0.996
            curve.append({"date": f"2026-{index // 28 + 1:02d}-{index % 28 + 1:02d}",
                          "equity": equity})
        low_target = quant_portfolio.expected_return_distribution(
            curve, 12, 0.02, "same-reference-seed",
        )
        high_target = quant_portfolio.expected_return_distribution(
            curve, 12, 0.20, "same-reference-seed",
        )
        self.assertEqual(low_target["p50"], high_target["p50"])
        self.assertGreaterEqual(
            low_target["probability_target"], high_target["probability_target"]
        )

    def test_research_watchlist_prefers_positive_model_signals(self):
        ranking = [
            {"symbol": "1", "name": "甲", "predicted_return": -0.01,
             "probability_up": 0.4, "rejection_reasons": ["趋势未通过"]},
            {"symbol": "2", "name": "乙", "predicted_return": 0.002,
             "probability_up": 0.55, "rejection_reasons": ["基本面未通过"]},
            {"symbol": "3", "name": "丙", "predicted_return": 0.001,
             "probability_up": 0.51, "rejection_reasons": []},
        ]
        watchlist = quant_portfolio._research_watchlist(ranking, 2)
        self.assertEqual([item["symbol"] for item in watchlist], ["2", "3"])
        self.assertTrue(all(item["observation_only"] for item in watchlist))

    def test_trial_recommendations_use_two_of_three_and_lot_affordability(self):
        ranking = [
            {"symbol": "300390", "name": "天华新能", "reference_price": 64.12,
             "probability_up": 0.478, "predicted_return": -0.001,
             "composite_score": 0.19, "fundamental_score": 0.57,
             "rejection_reasons": ["在线模型上涨概率未通过"], "eligible": False},
            {"symbol": "300750", "name": "宁德时代", "reference_price": 368.5,
             "probability_up": 0.522, "predicted_return": 0.001,
             "composite_score": 0.08, "fundamental_score": 0.52,
             "rejection_reasons": ["趋势或动量未通过"], "eligible": False},
            {"symbol": "688503", "name": "聚和材料", "reference_price": 77.85,
             "probability_up": 0.586, "predicted_return": 0.003,
             "composite_score": 0.03, "fundamental_score": -0.52,
             "rejection_reasons": ["基本面综合分未达到该风险档位门槛"],
             "eligible": False},
        ]
        mandate = {
            "capital": 100000, "max_positions": 2,
            "stop_loss_pct": 8, "take_profit_pct": 20,
            "execution": {"lot_size": 100},
        }
        strategy = {
            "profile": "aggressive",
            "parameters": {"prediction_floor": 0.50, "max_positions": 2,
                           "stop_loss": 0.08, "take_profit": 0.20},
        }
        recommendations = quant_portfolio._research_recommendations(
            ranking, mandate, strategy,
        )
        self.assertEqual(
            [item["symbol"] for item in recommendations], ["300390", "688503"]
        )
        self.assertEqual([item["shares"] for item in recommendations], [300, 200])
        self.assertTrue(all(item["factor_pass_count"] == 2 for item in recommendations))
        self.assertTrue(all(item["formal_position"] is False for item in recommendations))
        self.assertFalse(ranking[1]["research_affordable"])

    def test_candidate_display_order_promotes_recommendations(self):
        ranking = [
            {"symbol": "1", "composite_score": 0.90, "eligible": False},
            {"symbol": "2", "composite_score": 0.20, "eligible": False,
             "research_recommended": True},
            {"symbol": "3", "composite_score": 0.10, "eligible": True},
            {"symbol": "4", "composite_score": 0.70, "eligible": False,
             "research_recommended": True},
        ]
        ordered = quant_portfolio._display_candidate_ranking(
            ranking, [{"symbol": "3"}],
        )
        self.assertEqual([item["symbol"] for item in ordered], ["3", "4", "2", "1"])
        self.assertEqual([item["symbol"] for item in ranking], ["1", "2", "3", "4"])

        no_formal = quant_portfolio._display_candidate_ranking(ranking, [])
        self.assertEqual([item["symbol"] for item in no_formal], ["4", "2", "3", "1"])

    def test_tdx_desktop_metadata_populates_sector_membership(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / "T0002" / "hq_cache"
            cache.mkdir(parents=True)

            def write_tnf(path, code, name):
                record = bytearray(360)
                record[:6] = code.encode("ascii")
                encoded = name.encode("gbk")
                record[31:31 + len(encoded)] = encoded
                path.write_bytes(bytes(50) + bytes(record))

            write_tnf(cache / "shs.tnf", "600519", "贵州茅台")
            write_tnf(cache / "szs.tnf", "000858", "五粮液")
            (cache / "tdxhy.cfg").write_text(
                "1|600519|T030501|||\n0|000858|T030501|||\n", encoding="utf-8")
            (root / "incon.dat").write_text(
                "#TDXNHY\nT03|日常消费\nT0305|酿酒\nT030501|白酒\n######\n",
                encoding="gbk")
            db_path = root / "quant.db"
            initialize(db_path)
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
                _tdx_home=lambda: root,
            ):
                result = quant_portfolio.sync_a_share_universe(force=True)
                self.assertEqual(result["assets"], 2)
                resolved = quant_portfolio._resolve_sectors(["白酒"])
                candidates = quant_portfolio._sector_candidates(resolved["codes"])
                self.assertEqual({item["symbol"] for item in candidates},
                                 {"600519", "000858"})
                with closing(connect(db_path)) as conn:
                    conn.execute(
                        """INSERT INTO a_share_sector_memberships
                           (symbol,sector_code,sector_name,sector_level,source,source_asof,updated_at)
                           VALUES('600519','T04','航空',1,'test','2026-08-31','now')"""
                    )
                    conn.commit()
                military = quant_portfolio._resolve_sectors(["军工"])
                self.assertEqual(military["names"], ["航空"])

    def test_service_restart_marks_running_quant_work_as_interrupted(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "quant.db"
            initialize(db_path)
            with closing(connect(db_path)) as conn:
                mandate_id = conn.execute(
                    """INSERT INTO quant_mandates
                       (mandate_key,name,status,input_json,created_at,updated_at)
                       VALUES('mandate-test','测试组合','ACTIVE','{}','now','now')"""
                ).lastrowid
                conn.execute(
                    """INSERT INTO quant_portfolio_runs
                       (run_key,mandate_id,trigger_kind,status,started_at)
                       VALUES('running-test',?,'test','RUNNING','2000-01-01T00:00:00+00:00')""",
                    (mandate_id,),
                )
                conn.execute(
                    """INSERT INTO quant_portfolio_runs
                       (run_key,mandate_id,trigger_kind,status,started_at,finished_at)
                       VALUES('success-test',?,'test','SUCCESS','now','now')""",
                    (mandate_id,),
                )
                conn.commit()
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
            ):
                recovered = quant_portfolio.recover_interrupted_quant_runs()
            self.assertEqual(recovered, 1)
            with closing(connect(db_path)) as conn:
                running = conn.execute(
                    "SELECT status,error,finished_at FROM quant_portfolio_runs WHERE run_key='running-test'"
                ).fetchone()
                success = conn.execute(
                    "SELECT status FROM quant_portfolio_runs WHERE run_key='success-test'"
                ).fetchone()
            self.assertEqual(running["status"], "INTERRUPTED")
            self.assertIn("重新运行", running["error"])
            self.assertIsNotNone(running["finished_at"])
            self.assertEqual(success["status"], "SUCCESS")

    def test_recovery_preserves_fresh_run_from_other_service(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            initialize(db_path)
            with closing(connect(db_path)) as conn:
                mandate_id = conn.execute(
                    """INSERT INTO quant_mandates
                       (mandate_key,name,status,input_json,created_at,updated_at)
                       VALUES('fresh-mandate','fresh','ACTIVE','{}','now','now')"""
                ).lastrowid
                conn.execute(
                    """INSERT INTO quant_portfolio_runs
                       (run_key,mandate_id,trigger_kind,status,started_at)
                       VALUES('fresh-running',?,'test','RUNNING',?)""",
                    (mandate_id, quant_portfolio._now()),
                )
                conn.commit()
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
            ):
                recovered = quant_portfolio.recover_interrupted_quant_runs()
            self.assertEqual(recovered, 0)
            with closing(connect(db_path)) as conn:
                status = conn.execute(
                    "SELECT status FROM quant_portfolio_runs WHERE run_key='fresh-running'"
                ).fetchone()["status"]
            self.assertEqual(status, "RUNNING")

    def test_daily_refresh_deduplicates_identical_active_mandates(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            initialize(db_path)
            with closing(connect(db_path)) as conn:
                conn.executemany(
                    """INSERT INTO quant_mandates
                       (mandate_key,name,status,input_json,created_at,updated_at)
                       VALUES(?,?,'ACTIVE',?,'now','now')""",
                    [
                        ("old-same", "same", '{"capital":100000,"sectors":["白酒"]}'),
                        ("new-same", "same", '{"capital":100000,"sectors":["白酒"]}'),
                        ("different", "different", '{"capital":200000,"sectors":["白酒"]}'),
                    ],
                )
                conn.commit()
            seen = []
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
                run_quant_portfolio=lambda request, **kwargs: (
                    seen.append(kwargs["mandate_id"]) or
                    {"run_key": str(kwargs["mandate_id"]), "version": {}}
                ),
            ):
                result = quant_portfolio.refresh_active_quant_portfolios()
            self.assertEqual(result["mandates"], 2)
            self.assertEqual(seen, [3, 2])

    def test_daily_refresh_normalizes_legacy_mandates_and_has_no_twenty_item_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            initialize(db_path)
            with closing(connect(db_path)) as conn:
                conn.executemany(
                    """INSERT INTO quant_mandates
                       (mandate_key,name,status,input_json,created_at,updated_at)
                       VALUES(?,?,'ACTIVE',?,'now','now')""",
                    [
                        (f"legacy-{index}", f"legacy-{index}",
                         json.dumps({"capital": 100000 + index, "sectors": ["白酒"]}))
                        for index in range(21)
                    ],
                )
                conn.commit()
            seen = []
            with patch.multiple(
                quant_portfolio,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
                run_quant_portfolio=lambda request, **kwargs: (
                    seen.append((kwargs["mandate_id"], request["backtest_window_years"])) or
                    {"run_key": str(kwargs["mandate_id"]), "version": {}}
                ),
            ):
                result = quant_portfolio.refresh_active_quant_portfolios()
            self.assertEqual(result["mandates"], 21)
            self.assertEqual(len(seen), 21)
            self.assertTrue(all(window == 3 for _, window in seen))


if __name__ == "__main__":
    unittest.main()
