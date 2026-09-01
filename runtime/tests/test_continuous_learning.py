import json
import math
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from app import continuous_learning
from app.db import connect, initialize


class ContinuousLearningTests(unittest.TestCase):
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
