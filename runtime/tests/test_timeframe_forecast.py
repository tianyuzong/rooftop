import os
import math
import random
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

from app.db import connect, initialize
from app.timeframe_forecast import TIMEFRAME_SPECS, build_timeframe_forecast


class TimeframeForecastTests(unittest.TestCase):
    def _seed_falling_tdx_bars(self, db_path: Path) -> None:
        with closing(connect(db_path)) as conn:
            source_id = conn.execute(
                "SELECT id FROM data_sources WHERE code='tdx_public'"
            ).fetchone()[0]
            daily_rows = []
            price = 100.0
            start = date(2024, 1, 1)
            for index in range(500):
                price *= 0.9992
                trade_date = (start + timedelta(days=index)).isoformat()
                daily_rows.append((
                    "600519", trade_date, "qfq", price * 1.001,
                    price * 1.004, price * 0.996, price, 1000000,
                    price * 1000000, source_id, "now", "test",
                ))
            conn.executemany(
                """INSERT INTO market_daily_bars
                   (asset_symbol,trade_date,adjust_mode,open,high,low,close,
                    volume,amount,source_id,captured_at,raw_path)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                daily_rows,
            )
            minute_rows = []
            minute_price = price
            for day_index in range(20):
                day = start + timedelta(days=470 + day_index)
                session_start = datetime.combine(day, time(9, 30))
                for minute in range(121):
                    minute_price *= 0.9999
                    stamp = (session_start + timedelta(minutes=minute)).isoformat()
                    minute_rows.append((
                        "600519", stamp, 1, minute_price * 1.0002,
                        minute_price * 1.0005, minute_price * 0.9995,
                        minute_price, 1000, minute_price * 100000,
                        "OHLC", source_id, "now", "test",
                    ))
            conn.executemany(
                """INSERT INTO minute_bars
                   (asset_symbol,bar_time,interval_minutes,open,high,low,close,
                    volume,amount,bar_kind,source_id,captured_at,raw_path)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                minute_rows,
            )
            conn.commit()

    def _seed_stochastic_tdx_bars(self, db_path: Path) -> str:
        rng = random.Random(5)
        with closing(connect(db_path)) as conn:
            source_id = conn.execute(
                "SELECT id FROM data_sources WHERE code='tdx_public'"
            ).fetchone()[0]
            rows = []
            price = 100.0
            start = date(2020, 1, 1)
            for index in range(1200):
                previous = price
                drift = 0.0004 * math.sin(index / 45) + 0.00015
                price *= math.exp(drift + rng.gauss(0, 0.014))
                high = max(previous, price) * (1 + rng.random() * 0.006)
                low = min(previous, price) * (1 - rng.random() * 0.006)
                trade_date = (start + timedelta(days=index)).isoformat()
                volume = 1000000 + rng.randint(-200000, 200000)
                rows.append((
                    "600519", trade_date, "qfq", previous, high, low, price,
                    volume, price * volume, source_id, "now", "test",
                ))
            conn.executemany(
                """INSERT INTO market_daily_bars
                   (asset_symbol,trade_date,adjust_mode,open,high,low,close,
                    volume,amount,source_id,captured_at,raw_path)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            conn.commit()
        return (start + timedelta(days=1199)).isoformat()

    def test_stochastic_history_extends_rejected_horizons_as_labeled_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "forecast.db"
            initialize(db_path)
            data_asof = self._seed_stochastic_tdx_bars(db_path)
            with closing(connect(db_path)) as conn, patch.dict(
                os.environ, {"ARGUS_MARKET_PROVIDER": "tdx"}
            ):
                result = build_timeframe_forecast(
                    conn, "600519", data_asof, 24, "balanced"
                )

        self.assertEqual(
            [item["period"] for item in result["timeframes"]],
            [item["period"] for item in TIMEFRAME_SPECS],
        )
        periods = {item["period"]: item for item in result["timeframes"]}
        for period in ("1d", "1w", "1mo"):
            self.assertEqual(periods[period]["status"], "AVAILABLE")
            self.assertLessEqual(periods[period]["p10_price"], periods[period]["p50_price"])
            self.assertLessEqual(periods[period]["p50_price"], periods[period]["p90_price"])
            self.assertLessEqual(periods[period]["p10_price"], periods[period]["p25_price"])
            self.assertLessEqual(periods[period]["p75_price"], periods[period]["p90_price"])
            validation = periods[period]["validation"]
            self.assertTrue(validation["gate_passed"])
            self.assertFalse(validation["future_leakage"])
            self.assertGreater(validation["evaluation_points"], 0)
        for period in ("1q", "1y"):
            self.assertEqual(periods[period]["status"], "BASELINE_REFERENCE")
            self.assertTrue(periods[period]["reference_only"])
            self.assertIn("互不重叠", periods[period]["reason"])
        self.assertEqual(result["formal_model_probability"], None)
        self.assertEqual(
            result["validation_status"],
            "WALK_FORWARD_CALIBRATED_PARTIAL_WITH_BASELINE_EXTENSION",
        )
        self.assertEqual(len(result["history_curve"]), 90)
        self.assertEqual(result["forecast_curve"][0]["trading_day"], 0)
        self.assertEqual(result["requested_horizon_trading_days"], 504)
        self.assertEqual(result["horizon_trading_days"], 504)
        self.assertEqual(result["validated_horizon_trading_days"], 21)
        self.assertEqual(result["reference_horizon_trading_days"], 504)
        self.assertEqual(result["horizon_status"], "PARTIAL")
        self.assertIn("504", result["horizon_reason"])
        self.assertIn(63, [item["trading_day"] for item in result["forecast_curve"]])
        self.assertIn(504, [item["trading_day"] for item in result["forecast_curve"]])
        self.assertEqual(
            result["requested_forecast"]["forecast_basis"],
            "HISTORICAL_BASELINE_REFERENCE",
        )
        self.assertIsNotNone(result["requested_forecast"]["historical_up_ratio"])
        self.assertGreater(len(result["forecast_curve"]), 2)

    def test_overwide_interval_is_labeled_as_baseline_not_model_forecast(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "forecast.db"
            initialize(db_path)
            self._seed_falling_tdx_bars(db_path)
            with closing(connect(db_path)) as conn:
                result = build_timeframe_forecast(
                    conn, "600519", "2025-05-14", 24, "balanced"
                )
        daily = next(item for item in result["timeframes"] if item["period"] == "1d")
        self.assertEqual(daily["status"], "BASELINE_REFERENCE")
        self.assertTrue(daily["reference_only"])
        self.assertFalse(daily["validation"]["gates"]["wide_80_coverage"])
        self.assertEqual(result["validation_status"], "BASELINE_SCENARIO_ONLY")
        self.assertEqual(result["horizon_status"], "REFERENCE_PARTIAL")
        self.assertEqual(result["validated_horizon_trading_days"], 0)
        self.assertEqual(result["forecast_curve"][-1]["trading_day"], 252)
        self.assertEqual(
            result["forecast_curve"][-1]["basis"],
            "HISTORICAL_BASELINE_REFERENCE",
        )

    def test_uncalibrated_bearish_intraday_data_does_not_emit_sell_review(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "forecast.db"
            initialize(db_path)
            self._seed_falling_tdx_bars(db_path)
            with closing(connect(db_path)) as conn, patch.dict(
                os.environ, {"ARGUS_MARKET_PROVIDER": "tdx"}
            ):
                result = build_timeframe_forecast(
                    conn, "600519", "2025-05-14", 12, "balanced"
                )

        summary = result["summary"]
        self.assertEqual(summary["status"], "UNAVAILABLE")
        self.assertEqual(summary["action"], "HOLD_WATCH")
        self.assertEqual(summary["sell_review"]["status"], "UNAVAILABLE")
        self.assertIsNone(summary["sell_review"]["label"])
        self.assertTrue(summary["sell_review"]["requires_real_position_for_quantity"])

    def test_missing_bars_are_explicit_instead_of_fabricated(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "forecast.db"
            initialize(db_path)
            with closing(connect(db_path)) as conn, patch.dict(
                os.environ, {"ARGUS_MARKET_PROVIDER": "tdx"}
            ):
                result = build_timeframe_forecast(
                    conn, "600519", "2026-09-01", 6, "aggressive"
                )
        self.assertEqual(result["summary"]["status"], "UNAVAILABLE")
        self.assertIsNone(result["summary"]["sell_review"]["label"])
        self.assertTrue(all(
            item["status"] in {"NO_DATA", "INSUFFICIENT_HISTORY"}
            for item in result["timeframes"]
        ))
        self.assertTrue(all(item["p50_price"] is None for item in result["timeframes"]))
        self.assertEqual(result["history_curve"], [])
        self.assertEqual(result["forecast_curve"], [])
        self.assertEqual(result["horizon_status"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
