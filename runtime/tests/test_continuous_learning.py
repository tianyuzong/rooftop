import json
import math
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from app import continuous_learning
from app.db import connect, initialize


class ContinuousLearningTests(unittest.TestCase):
    def test_cached_close_requires_every_trading_minute_for_every_stock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "close.db"
            initialize(path)
            self._seed_bars(path, rows=30)
            minutes = [*range(571, 691), *range(781, 901)]
            with closing(connect(path)) as conn:
                source_id = conn.execute("SELECT id FROM data_sources WHERE code='tdx_public'").fetchone()[0]
                for symbol in ("600519", "000858"):
                    conn.executemany(
                        "INSERT INTO minute_bars(asset_symbol,bar_time,interval_minutes,open,high,low,close,source_id,captured_at,raw_path) "
                        "VALUES(?,?,1,1,1,1,1,?,'fixture','fixture')",
                        [(symbol, f"2025-01-31T{minute // 60:02d}:{minute % 60:02d}:00+08:00", source_id)
                         for minute in minutes],
                    )
                conn.commit()
            with self._patch_db(path):
                self.assertTrue(continuous_learning.market_close_coverage(["600519", "000858"], "2025-01-31")["complete"])
                with closing(connect(path)) as conn:
                    conn.execute("DELETE FROM minute_bars WHERE asset_symbol='000858' AND bar_time='2025-01-31T10:15:00+08:00'")
                    conn.commit()
                coverage = continuous_learning.market_close_coverage(["600519", "000858", "300750"], "2025-01-31")
                self.assertFalse(coverage["complete"])
                self.assertEqual(coverage["missing_minute_symbols"], ["000858", "300750"])
                self.assertEqual(coverage["minute_counts"], {"600519": 240, "000858": 239, "300750": 0})

    def test_freshness_requires_every_requested_stock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "coverage.db"
            initialize(path)
            self._seed_bars(path, rows=30)
            with closing(connect(path)) as conn:
                last = conn.execute("SELECT MAX(trade_date) FROM market_daily_bars").fetchone()[0]
                conn.execute("DELETE FROM market_daily_bars WHERE asset_symbol='000858' AND trade_date=?", (last,))
                conn.commit()
            with self._patch_db(path):
                coverage = continuous_learning.market_data_coverage(["600519", "000858", "999999"], last)
                self.assertFalse(coverage["complete"])
                self.assertEqual(coverage["missing_symbols"], ["999999"])
                self.assertEqual(coverage["stale_symbols"], ["000858"])
                self.assertIsNone(coverage["data_asof"])
                self.assertLess(continuous_learning._latest_data_date(["600519", "000858"]), last)

    def _online_fixture(self, path):
        initialize(path)
        with self._patch_db(path):
            active = continuous_learning._ensure_active_model()
        weights = {name: 0.0 for name in continuous_learning.FEATURE_NAMES}
        weights["bias"] = 0.2
        with closing(connect(path)) as conn:
            conn.execute(
                "UPDATE prediction_model_versions SET training_start='2026-01-01',training_end='2026-08-28',"
                "coefficients_json=?,metrics_json=? WHERE version_key=?",
                (json.dumps(weights), json.dumps({"training_sample_count": 500}), active["version_key"]),
            )
            conn.commit()
        samples = []
        for index in range(5):
            target = date(2026, 8, 31) + timedelta(days=index)
            for symbol in ("600519", "000858", "300750"):
                features = {name: 0.0 for name in continuous_learning.FEATURE_NAMES}
                features["bias"] = 1.0
                samples.append({"symbol": symbol, "signal_date": (target - timedelta(days=1)).isoformat(),
                                "target_date": target.isoformat(), "features": features, "actual_return": 0.01})
        return active, weights, samples

    def test_daily_learning_advances_checkpoint_without_relearning_old_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "online.db"
            active, weights, samples = self._online_fixture(path)
            with self._patch_db(path), patch.object(
                continuous_learning, "build_walk_forward_samples", return_value=samples
            ), patch.object(continuous_learning, "_latest_data_date", return_value="2026-09-04"):
                result = continuous_learning.run_six_month_walk_forward(["600519", "000858", "300750"])
                again = continuous_learning.run_six_month_walk_forward(["600519", "000858", "300750"])
            self.assertEqual(result["status"], "ACTIVE")
            self.assertEqual(result["training_end"], "2026-09-04")
            self.assertEqual(result["baseline_version"], active["version_key"])
            self.assertEqual(result["gate"]["evaluation_trading_days"], 5)
            self.assertEqual(result["gate"]["evaluation_scope"], "incremental_forward")
            self.assertEqual(again["status"], "UNCHANGED")
            self.assertEqual(again["gate"]["evaluation_scope"], "incremental_forward")
            self.assertTrue(again["gate"]["evaluation_reused"])
            self.assertFalse(again["gate"]["new_data_available"])
            with closing(connect(path)) as conn:
                row = conn.execute("SELECT * FROM prediction_model_versions WHERE status='ACTIVE'").fetchone()
                self.assertNotEqual(json.loads(row["coefficients_json"]), weights)
                dates = conn.execute("SELECT MIN(target_date) FROM prediction_backtest_points").fetchone()[0]
                self.assertGreater(dates, "2026-08-28")

    def test_daily_learning_rejects_regression_against_current_model(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "regression.db"
            active, weights, samples = self._online_fixture(path)
            def bad_update(coefficients, batch):
                coefficients["bias"] = -3.0
            with self._patch_db(path), patch.object(
                continuous_learning, "build_walk_forward_samples", return_value=samples
            ), patch.object(continuous_learning, "_latest_data_date", return_value="2026-09-04"), patch.object(
                continuous_learning, "_update_online_weights", side_effect=bad_update
            ):
                result = continuous_learning.run_six_month_walk_forward(["600519", "000858", "300750"])
            self.assertEqual(result["status"], "REJECTED")
            self.assertFalse(result["gate"]["non_regression"])
            with closing(connect(path)) as conn:
                row = conn.execute("SELECT version_key,training_end FROM prediction_model_versions WHERE status='ACTIVE'").fetchone()
                self.assertEqual(row["version_key"], active["version_key"])
                self.assertEqual(row["training_end"], "2026-08-28")

    def test_failed_portfolios_remain_retryable_and_retry_only_failures(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            path = Path(folder) / "cycle.db"
            initialize(path)
            stack.enter_context(self._patch_db(path))
            for name, value in {
                "settle_predictions": {"scored": 0}, "run_six_month_walk_forward": {"status": "UNCHANGED"},
                "_latest_data_date": "2026-09-04", "market_data_coverage": {"complete": True},
                "create_daily_predictions": {"count": 0}, "next_trading_day": date(2026, 9, 7),
            }.items():
                stack.enter_context(patch.object(continuous_learning, name, return_value=value))
            stack.enter_context(patch("app.deep_learning.settle_deep_predictions", return_value={"scored": 0}))
            refresh = stack.enter_context(patch("app.quant_portfolio.refresh_active_quant_portfolios", side_effect=[
                {"mandates": 2, "updated": 1, "results": [{"mandate_key": "ok"}],
                 "errors": [{"mandate_key": "failed", "error": "timeout"}]},
                {"mandates": 1, "updated": 1, "results": [{"mandate_key": "failed"}], "errors": []},
            ]))
            inputs = {"phase": "POST_CLOSE", "cycle_date": "2026-09-04", "symbols": ["600519"],
                      "refresh_calendar": False, "refresh_data": False, "refresh_fundamentals": False,
                      "collect_sentiment": False, "train_deep_model": False, "evolve_intraday": False,
                      "evolve_source_code": False}
            first = continuous_learning.run_continuous_learning_cycle(inputs)
            self.assertEqual(first["status"], "PARTIAL")
            with closing(connect(path)) as conn:
                row = conn.execute("SELECT * FROM harness_learning_cycles").fetchone()
                self.assertTrue(continuous_learning._cycle_needs_retry(row))
            with self.assertRaisesRegex(ValueError, "universe is frozen"):
                continuous_learning.run_continuous_learning_cycle({**inputs, "symbols": ["000858"]})
            retry_inputs = {**inputs, "refresh_data": True}
            retry_inputs.pop("symbols")
            with patch.object(continuous_learning, "learning_universe", return_value=["000858"]), patch.object(
                continuous_learning, "market_close_coverage", return_value={"complete": True}
            ) as coverage, patch.object(continuous_learning, "_refresh_market_data_isolated") as network:
                second = continuous_learning.run_continuous_learning_cycle(retry_inputs)
                coverage.assert_called_once_with(["600519"], "2026-09-04")
                network.assert_not_called()
            self.assertEqual(refresh.call_args.kwargs["only_mandate_keys"], ["failed"])
            self.assertEqual(second["status"], "SUCCESS")
            self.assertEqual(second["metrics"]["quant_portfolios"]["updated"], 2)

    def _patch_db(self, path):
        return patch.multiple(
            continuous_learning,
            connect=lambda: connect(path),
            initialize=lambda: initialize(path),
        )

    @staticmethod
    def _seed_bars(path, rows=240):
        with closing(connect(path)) as conn:
            source_id = conn.execute(
                "SELECT id FROM data_sources WHERE code='tdx_public'"
            ).fetchone()[0]
            values = []
            for offset, symbol in enumerate(("600519", "000858", "300750")):
                previous = 100.0 - offset * 10
                for index in range(rows):
                    day = (date(2025, 1, 2) + timedelta(days=index)).isoformat()
                    close = previous * (1.0005 + 0.003 * math.sin(index / 9 + offset))
                    values.append((symbol, day, previous, max(previous, close) * 1.01,
                                   min(previous, close) * 0.99, close,
                                   10_000_000 + index * 1000, source_id,
                                   "2026-01-01T00:00:00+00:00", "test"))
                    previous = close
            conn.executemany(
                """INSERT INTO market_daily_bars
                   (asset_symbol,trade_date,adjust_mode,open,high,low,close,volume,
                    source_id,captured_at,raw_path)
                   VALUES(?,?,'qfq',?,?,?,?,?,?,?,?)""", values,
            )
            conn.commit()

    def test_samples_are_strictly_next_day_and_contain_no_outcome_feature(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            self._seed_bars(path)
            with self._patch_db(path):
                samples = continuous_learning.build_walk_forward_samples(
                    ["600519", "000858", "300750"])
            self.assertGreater(len(samples), 500)
            self.assertTrue(all(item["signal_date"] < item["target_date"] for item in samples))
            self.assertTrue(all(set(item["features"]) == set(continuous_learning.FEATURE_NAMES)
                                for item in samples))
            self.assertTrue(all("actual_return" not in item["features"] for item in samples))

    def test_latest_scores_are_pinned_to_the_evaluated_model_date(self):
        bars = [
            {
                "trade_date": (date(2026, 8, 1) + timedelta(days=index)).isoformat(),
                "close": 100.0 + index,
                "volume": 1_000_000 + index * 1000,
            }
            for index in range(30)
        ]
        model = {
            "version_key": "prediction-evaluated",
            "coefficients": dict(continuous_learning.INITIAL_COEFFICIENTS),
        }
        with patch.object(
            continuous_learning, "_ensure_active_model", return_value=model
        ), patch.object(
            continuous_learning, "_load_bars", return_value={"600519": bars}
        ), patch.object(
            continuous_learning, "_load_sentiment", return_value={
                ("600519", "2026-08-27"): 0.25,
                ("600519", "2026-08-30"): 0.95,
            }
        ):
            result = continuous_learning.latest_symbol_scores(
                ["600519"], data_asof="2026-08-28"
            )
        self.assertEqual(result["model_version"], "prediction-evaluated")
        self.assertEqual(result["scores"][0]["signal_date"], "2026-08-28")
        self.assertEqual(result["scores"][0]["sentiment_date"], "2026-08-27")
        self.assertEqual(result["scores"][0]["features"]["sentiment"], 0.25)

    def test_cached_calendar_treats_missing_weekday_inside_coverage_as_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                conn.executemany(
                    """INSERT INTO trading_calendar
                       (trade_date,market,is_open,source,updated_at)
                       VALUES(?,'CN',1,'test','2026-01-01')""",
                    [("2026-04-30",), ("2026-05-06",)],
                )
                conn.commit()
            with self._patch_db(path):
                self.assertFalse(continuous_learning.is_trading_day(date(2026, 5, 1)))
                self.assertTrue(continuous_learning.is_trading_day(date(2026, 5, 6)))

    def test_scheduler_retries_stale_post_close_cycle_after_retry_window(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                conn.execute(
                    """INSERT INTO harness_learning_cycles
                       (cycle_key,cycle_date,phase,status,trigger_kind,universe_json,
                        data_asof,finished_at,started_at)
                       VALUES('continuous-post_close-2026-08-31','2026-08-31','POST_CLOSE',
                              'SUCCESS','test','[\"600519\"]','2026-08-28',
                              '2026-08-31T08:00:00+00:00','2026-08-31T07:00:00+00:00')"""
                )
                conn.commit()
            current = continuous_learning.datetime.fromisoformat(
                "2026-08-31T18:00:00+08:00")
            with self._patch_db(path), patch.object(
                continuous_learning, "learning_universe", return_value=["600519"]
            ), patch.object(
                continuous_learning, "_latest_data_date", return_value="2026-08-28"
            ), patch.object(
                continuous_learning, "is_trading_day", return_value=True
            ), patch.object(
                continuous_learning, "_timestamp_age_seconds", return_value=7200.0
            ):
                request = continuous_learning.scheduled_cycle_request(current)
            self.assertEqual(request["phase"], "POST_CLOSE")
            self.assertTrue(request["retry_if_stale"])

    def test_scheduler_does_not_duplicate_running_catchup_cycle(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                conn.execute(
                    """INSERT INTO harness_learning_cycles
                       (cycle_key,cycle_date,phase,status,trigger_kind,universe_json,
                        heartbeat_at,started_at)
                       VALUES('continuous-post_close-2026-08-28','2026-08-28','POST_CLOSE',
                              'RUNNING','test','[\"600519\"]',
                              '2026-08-29T17:59:00+00:00','2026-08-29T17:00:00+00:00')"""
                )
                conn.commit()
            current = continuous_learning.datetime.fromisoformat(
                "2026-08-30T02:00:00+08:00")
            with self._patch_db(path), patch.object(
                continuous_learning, "learning_universe", return_value=["600519"]
            ), patch.object(
                continuous_learning, "_latest_data_date", return_value="2026-08-28"
            ), patch.object(
                continuous_learning, "_timestamp_age_seconds", return_value=60.0
            ):
                request = continuous_learning.scheduled_cycle_request(current)
            self.assertIsNone(request)

    def test_six_month_walk_forward_persists_model_gate_and_points(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            self._seed_bars(path)
            with self._patch_db(path):
                result = continuous_learning.run_six_month_walk_forward(
                    ["600519", "000858", "300750"], auto_promote=False)
            self.assertEqual(result["candidate_metrics"]["trading_days"], 126)
            self.assertGreater(result["candidate_metrics"]["sample_count"], 300)
            self.assertIn(result["status"], {"CANDIDATE", "REJECTED"})
            with closing(connect(path)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM prediction_evaluations").fetchone()[0], 1)
                point_count = conn.execute(
                    "SELECT COUNT(*) FROM prediction_backtest_points").fetchone()[0]
                self.assertEqual(point_count, result["candidate_metrics"]["sample_count"] * 2)

    def test_same_data_and_weights_do_not_create_another_model_version(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            self._seed_bars(path)
            with self._patch_db(path):
                first = continuous_learning.run_six_month_walk_forward(
                    ["600519", "000858", "300750"], auto_promote=False)
                with closing(connect(path)) as conn:
                    conn.execute("UPDATE prediction_model_versions SET status='ARCHIVED'")
                    conn.execute(
                        """UPDATE prediction_model_versions SET status='ACTIVE',activated_at=created_at
                           WHERE version_key=?""", (first["candidate_version"],),
                    )
                    conn.commit()
                second = continuous_learning.run_six_month_walk_forward(
                    ["600519", "000858", "300750"], auto_promote=True)
                payload = continuous_learning.continuous_learning_payload()
            self.assertEqual(second["status"], "UNCHANGED")
            self.assertEqual(second["candidate_version"], first["candidate_version"])
            self.assertTrue(payload["last_evaluation"]["gate"]["new_data_available"] is False)

    def test_pending_prediction_is_scored_only_after_target_close_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            self._seed_bars(path, rows=50)
            with self._patch_db(path):
                model = continuous_learning._ensure_active_model()
                with closing(connect(path)) as conn:
                    last_dates = conn.execute(
                        """SELECT trade_date FROM market_daily_bars WHERE asset_symbol='600519'
                           ORDER BY trade_date DESC LIMIT 2"""
                    ).fetchall()
                    target = last_dates[0][0]
                    signal = last_dates[1][0]
                    conn.execute(
                        """INSERT INTO daily_predictions
                           (prediction_key,model_version,symbol,signal_date,target_date,phase,
                            probability_up,predicted_return,confidence,features_json,
                            rationale_json,status,created_at)
                           VALUES('test-prediction',?,?,?,?,?,0.6,0.004,0.2,'{}','{}','PENDING',?)""",
                        (model["version_key"], "600519", signal, target, "POST_CLOSE",
                         "2026-01-01T00:00:00+00:00"),
                    )
                    conn.commit()
                scored = continuous_learning.settle_predictions()
            self.assertEqual(scored["scored"], 1)
            with closing(connect(path)) as conn:
                row = conn.execute(
                    "SELECT status,direction_correct,brier_score FROM daily_predictions"
                ).fetchone()
            self.assertEqual(row["status"], "SCORED")
            self.assertIn(row["direction_correct"], {0, 1})
            self.assertIsNotNone(row["brier_score"])

    def test_duplicate_scheduled_cycle_is_rejected_while_first_is_running(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                conn.execute(
                    """INSERT INTO harness_learning_cycles
                       (cycle_key,cycle_date,phase,status,trigger_kind,universe_json,started_at)
                       VALUES('continuous-pre_open-2026-08-31','2026-08-31','PRE_OPEN',
                              'RUNNING','test','[\"600519\"]','2026-08-31T00:40:00+00:00')"""
                )
                conn.commit()
            with self._patch_db(path), patch.object(
                continuous_learning, "_now", return_value="2026-08-31T00:45:00+00:00"
            ), patch.object(continuous_learning, "datetime", wraps=continuous_learning.datetime) as mocked_datetime:
                mocked_datetime.now.return_value = continuous_learning.datetime.fromisoformat(
                    "2026-08-31T00:45:00+00:00")
                result = continuous_learning.run_continuous_learning_cycle({
                    "phase": "PRE_OPEN", "cycle_date": "2026-08-31",
                    "symbols": ["600519"],
                })
            self.assertEqual(result["status"], "ALREADY_RUNNING")

    def test_cycle_persists_stage_progress_and_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            inputs = {
                "phase": "BACKFILL",
                "symbols": ["600519"],
                "refresh_calendar": False,
                "refresh_data": False,
                "refresh_fundamentals": False,
                "collect_sentiment": False,
                "train_deep_model": False,
                "evolve_intraday": False,
                "evolve_source_code": False,
                "refresh_quant_portfolios": False,
            }
            with self._patch_db(path), patch.object(
                continuous_learning, "settle_predictions", return_value={"scored": 0}
            ), patch(
                "app.deep_learning.settle_deep_predictions", return_value={"scored": 0}
            ), patch.object(
                continuous_learning, "run_six_month_walk_forward",
                return_value={"status": "REJECTED"},
            ), patch.object(
                continuous_learning, "_latest_data_date", return_value="2026-08-28"
            ), patch.object(
                continuous_learning, "next_trading_day", return_value=date(2026, 8, 31)
            ), patch.object(
                continuous_learning, "create_daily_predictions", return_value={"count": 1}
            ):
                result = continuous_learning.run_continuous_learning_cycle(inputs)
                payload = continuous_learning.continuous_learning_payload()

            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(result["progress"]["percent"], 100.0)
            self.assertEqual(result["progress"]["total"], 3)
            self.assertTrue(all(
                item["status"] == "SUCCEEDED" for item in result["progress"]["stages"]
            ))
            with closing(connect(path)) as conn:
                row = conn.execute(
                    "SELECT progress_json,heartbeat_at,worker_token FROM harness_learning_cycles"
                ).fetchone()
            stored_progress = json.loads(row["progress_json"])
            self.assertEqual(stored_progress["percent"], 100.0)
            self.assertIsNotNone(row["heartbeat_at"])
            self.assertIsNone(row["worker_token"])
            self.assertEqual(payload["last_cycle"]["progress"]["percent"], 100.0)
            self.assertNotIn("worker_token", payload["last_cycle"])

    def test_stale_cycle_heartbeat_is_recoverable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                conn.execute(
                    """INSERT INTO harness_learning_cycles
                       (cycle_key,cycle_date,phase,status,trigger_kind,universe_json,
                        progress_json,heartbeat_at,worker_token,started_at)
                       VALUES('continuous-pre_open-2026-08-31','2026-08-31','PRE_OPEN',
                              'RUNNING','test','[\"600519\"]','{}',
                              '2026-08-31T00:00:00+00:00','dead-worker',
                              '2026-08-31T00:00:00+00:00')"""
                )
                conn.commit()
            inputs = {
                "phase": "PRE_OPEN",
                "cycle_date": "2026-08-31",
                "symbols": ["600519"],
                "refresh_calendar": False,
                "refresh_data": False,
                "collect_sentiment": False,
                "train_deep_model": False,
                "evolve_intraday": False,
                "evolve_source_code": False,
                "refresh_quant_portfolios": False,
            }
            with self._patch_db(path), patch.object(
                continuous_learning, "_timestamp_age_seconds", return_value=999.0
            ), patch.object(
                continuous_learning, "settle_predictions", return_value={"scored": 0}
            ), patch(
                "app.deep_learning.settle_deep_predictions", return_value={"scored": 0}
            ), patch.object(
                continuous_learning, "run_six_month_walk_forward",
                return_value={"status": "REJECTED"},
            ), patch.object(
                continuous_learning, "_latest_data_date", return_value="2026-08-28"
            ), patch.object(
                continuous_learning, "create_daily_predictions", return_value={"count": 1}
            ):
                result = continuous_learning.run_continuous_learning_cycle(inputs)

            self.assertEqual(result["status"], "SUCCESS")
            with closing(connect(path)) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM harness_learning_cycles"
                ).fetchone()[0]
                row = conn.execute(
                    "SELECT status,worker_token FROM harness_learning_cycles"
                ).fetchone()
            self.assertEqual(count, 1)
            self.assertEqual(row["status"], "SUCCESS")
            self.assertIsNone(row["worker_token"])


if __name__ == "__main__":
    unittest.main()
