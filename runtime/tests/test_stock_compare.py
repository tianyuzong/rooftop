import unittest
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import stock_compare
from app.db import connect, initialize
from app.stock_compare import (calculate_price_metrics, normalize_profile,
                               register_compared_stocks, resolve_stock,
                               run_profile_backtest, split_stock_inputs)


def sample_bars(days=260):
    rows = []
    price = 10.0
    for index in range(days):
        price *= 1.001 + (0.002 if index % 17 == 0 else 0)
        rows.append({
            "trade_date": f"2025-{1 + index // 28:02d}-{1 + index % 28:02d}",
            "open": price * 0.998, "high": price * 1.01, "low": price * 0.99,
            "close": price, "volume": 1_000_000, "amount": 20_000_000,
        })
    return rows


class StockCompareTests(unittest.TestCase):
    def test_chinese_profile_aliases(self):
        self.assertEqual(normalize_profile("激进派"), "aggressive")
        self.assertEqual(normalize_profile("中立"), "balanced")
        self.assertEqual(normalize_profile("保守"), "conservative")

    def test_input_splitting_and_limits(self):
        self.assertEqual(split_stock_inputs("600519，000858 300750"), ["600519", "000858", "300750"])
        with self.assertRaises(ValueError):
            split_stock_inputs("600519")

    def test_common_chinese_stock_aliases_resolve_without_network(self):
        self.assertEqual(resolve_stock("东方航空")["symbol"], "600115")
        self.assertEqual(resolve_stock("中国建行")["symbol"], "601939")

    def test_compared_stocks_are_persisted_once_and_counted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with patch.object(stock_compare, "connect", lambda: connect(path)):
                register_compared_stocks([
                    {"symbol": "601988", "name": "中国银行"},
                    {"symbol": "600115", "name": "东方航空"},
                ])
                register_compared_stocks([{"symbol": "601988", "name": "中国银行"}])
            with closing(connect(path)) as conn:
                rows = conn.execute(
                    "SELECT symbol,compare_count FROM comparison_watchlist ORDER BY symbol"
                ).fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [("600115", 1), ("601988", 2)])

    def test_price_metrics_are_auditable(self):
        result = calculate_price_metrics(sample_bars())
        self.assertEqual(result["history_days"], 260)
        self.assertGreater(result["momentum_60d"], 0)
        self.assertLessEqual(result["max_drawdown"], 0)

    def test_all_profiles_generate_backtests(self):
        bars = sample_bars()
        for profile in ("aggressive", "balanced", "conservative"):
            result = run_profile_backtest(bars, profile)
            self.assertTrue(result["curve"])
            self.assertIn("AKQuant", result["engine"])
            self.assertEqual(result["commission_rate"], 0.0003)

    def test_repeated_refresh_is_coalesced(self):
        resolved = [{"symbol": "600519"}, {"symbol": "000858"}]
        stock_compare._refresh_last_attempt.clear()
        with patch.object(stock_compare, "_stale_symbols", return_value=["600519", "000858"]), \
                patch.object(stock_compare, "_missing_tdx_history_symbols", return_value=[]), \
                patch.object(stock_compare, "_schedule_history_refresh", return_value=[]), \
                patch.object(stock_compare, "refresh_market_data", return_value={"status": "SUCCESS", "errors": []}) as refresh:
            first = stock_compare._refresh_if_needed(resolved, False)
            second = stock_compare._refresh_if_needed(resolved, False)
        self.assertEqual(first["status"], "SUCCESS")
        self.assertEqual(second["status"], "REFRESH_COALESCED")
        refresh.assert_called_once()
        stock_compare._refresh_last_attempt.clear()

    def test_new_stocks_start_full_tdx_history_backfill(self):
        resolved = [{"symbol": "600519"}, {"symbol": "000858"}]
        stock_compare._refresh_last_attempt.clear()
        with patch.object(stock_compare, "_stale_symbols", return_value=["600519", "000858"]), \
                patch.object(stock_compare, "_missing_tdx_history_symbols", return_value=["600519", "000858"]), \
                patch.object(stock_compare, "_schedule_history_refresh", return_value=["600519", "000858"]) as schedule, \
                patch.object(stock_compare, "refresh_market_data", return_value={"status": "REFRESHED", "errors": []}) as refresh:
            result = stock_compare._refresh_if_needed(resolved, False)
        refresh.assert_called_once_with(["600519", "000858"], include_minutes=False,
                                        include_daily=True, include_history=False)
        schedule.assert_called_once_with(["600519", "000858"])
        self.assertEqual(result["history_refresh"]["status"], "STARTED")
        stock_compare._refresh_last_attempt.clear()


if __name__ == "__main__":
    unittest.main()
