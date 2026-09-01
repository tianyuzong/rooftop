import math
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import intraday_strategy
from app.db import connect, initialize


class IntradayStrategyTests(unittest.TestCase):
    @staticmethod
    def _bars(days=150):
        result = []
        shanghai = ZoneInfo("Asia/Shanghai")
        start = date(2025, 1, 2)
        price = 100.0
        for day_index in range(days):
            current = start + timedelta(days=day_index)
            for bar_index in range(48):
                price *= 1 + 0.00035 * math.sin((day_index * 48 + bar_index) / 12) + 0.00004
                stamp = datetime.combine(current, time(9, 35), shanghai) + timedelta(minutes=5 * bar_index)
                result.append({
                    "asset_symbol": "600519", "bar_time": stamp.isoformat(),
                    "trade_date": current.isoformat(), "open": price * 0.9998,
                    "high": price * 1.0005, "low": price * 0.9995,
                    "close": price, "volume": 1_000_000, "amount": price * 1_000_000,
                })
        return result

    def test_simulation_uses_next_bar_and_never_sells_t_plus_zero(self):
        bars = self._bars(40)
        costs = {"commission": 0.0003, "minimum_commission": 5.0,
                 "stamp_tax": 0.0005, "slippage": 0.0005,
                 "participation": 0.05, "round_lot": 100, "t_plus_one": True}
        params = intraday_strategy._candidate_library()[0]
        result = intraday_strategy._simulate_symbol(
            bars, params, bars[0]["trade_date"], bars[-1]["trade_date"], 100_000.0, costs)
        buys = []
        for trade in result["trades"]:
            if trade["action"] == "BUY":
                buys.append(trade)
            elif trade["action"] == "SELL":
                self.assertTrue(buys)
                self.assertGreater(trade["time"][:10], buys[-1]["time"][:10])

    def test_run_identity_changes_when_same_day_minute_coverage_improves(self):
        bars = self._bars(2)
        partial = {"600519": bars[:-1]}
        complete = {"600519": bars}
        partial_snapshot = intraday_strategy._intraday_data_snapshot(5, ["600519"], partial)
        complete_snapshot = intraday_strategy._intraday_data_snapshot(5, ["600519"], complete)
        self.assertNotEqual(partial_snapshot, complete_snapshot)
        self.assertEqual(
            complete_snapshot,
            intraday_strategy._intraday_data_snapshot(5, ["600519"], complete),
        )

    def test_walk_forward_persists_untouched_holdout_and_version(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "intraday.db"
            initialize(path)
            bars = self._bars()
            with closing(connect(path)) as conn:
                source_id = conn.execute(
                    "SELECT id FROM data_sources WHERE code='tdx_public'"
                ).fetchone()[0]
                conn.executemany(
                    """INSERT INTO minute_bars
                       (asset_symbol,bar_time,interval_minutes,open,high,low,close,
                        volume,amount,bar_kind,source_id,captured_at,raw_path)
                       VALUES(?,?,5,?,?,?,?,?,?,'OHLC',?,'2026-01-01T00:00:00+00:00','test')""",
                    [(item["asset_symbol"], item["bar_time"], item["open"], item["high"],
                      item["low"], item["close"], item["volume"], item["amount"], source_id)
                     for item in bars],
                )
                conn.commit()
            with patch.multiple(
                intraday_strategy,
                connect=lambda: connect(path),
                initialize=lambda: initialize(path),
            ):
                result = intraday_strategy.run_intraday_evolution(["600519"], auto_promote=False)
            self.assertEqual(result["status"], "SUCCESS")
            metrics = result["metrics"]
            self.assertGreaterEqual(metrics["candidate_count"], 4)
            self.assertGreaterEqual(metrics["holdout"]["trading_days"], 20)
            with closing(connect(path)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM intraday_strategy_candidates").fetchone()[0],
                                 metrics["candidate_count"])
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM intraday_strategy_versions").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
