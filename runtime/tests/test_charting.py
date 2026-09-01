import os
import sqlite3
import unittest
from unittest.mock import patch

from app.charting import (SUPPORTED_PERIODS, _daily_source_rows, _minute_source_rows,
                          add_ma5, add_technical_indicators, aggregate_calendar,
                          aggregate_intraday)


class ChartingTests(unittest.TestCase):
    def test_all_requested_periods_are_declared(self):
        self.assertEqual(set(SUPPORTED_PERIODS),
                         {"time", "5d", "1m", "5m", "15m", "30m", "60m", "120m",
                          "1d", "1w", "1mo", "1q", "1y"})

    def test_intraday_aggregation_does_not_bridge_lunch(self):
        rows = [
            {"bar_time": "2026-08-11T11:30:00+08:00", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1, "amount": 10},
            {"bar_time": "2026-08-11T13:00:00+08:00", "open": 11, "high": 11, "low": 11, "close": 11, "volume": 2, "amount": 22},
        ]
        bars = aggregate_intraday(rows, 120)
        self.assertEqual(len(bars), 2)

    def test_native_hour_bars_form_two_session_120_minute_bars(self):
        rows = [
            {"bar_time": f"2026-08-11T{clock}:00+08:00", "open": price, "high": price + 1,
             "low": price - 1, "close": price + .5, "volume": 1, "amount": 10}
            for clock, price in (("10:30", 10), ("11:30", 11), ("14:00", 12), ("15:00", 13))
        ]
        bars = aggregate_intraday(rows, 120, source_minutes=60)
        self.assertEqual(len(bars), 2)
        self.assertEqual([bar["open"] for bar in bars], [10, 12])
        self.assertEqual([bar["close"] for bar in bars], [11.5, 13.5])

    def test_calendar_quarter_and_ma5(self):
        rows = []
        for month in range(1, 7):
            rows.append({"trade_date": f"2026-{month:02d}-01", "open": month, "high": month + 1,
                         "low": month - .5, "close": month + .5, "volume": 1, "amount": 1})
        quarters = aggregate_calendar(rows, "1q")
        self.assertEqual(len(quarters), 2)
        self.assertEqual(quarters[0]["open"], 1)
        self.assertEqual(quarters[0]["close"], 3.5)
        add_ma5(rows)
        self.assertIsNone(rows[3]["ma5"])
        self.assertAlmostEqual(rows[4]["ma5"], 3.5)

    def test_hover_kdj_macd_and_estimated_amount(self):
        rows = [
            {"time": f"2026-08-{day:02d}", "open": 10 + day / 10, "high": 10.5 + day / 10,
             "low": 9.5 + day / 10, "close": 10.2 + day / 10, "volume": 1000, "amount": None}
            for day in range(1, 12)
        ]
        add_technical_indicators(rows, "600519")
        self.assertIn("kdj_j", rows[-1])
        self.assertIn("macd_hist", rows[-1])
        self.assertGreater(rows[-1]["amount"], 0)
        self.assertTrue(rows[-1]["amount_estimated"])
        self.assertIsNone(rows[-1]["turnover_rate"])

    def test_tdx_mode_never_falls_back_to_old_chart_sources(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE data_sources(id INTEGER PRIMARY KEY, code TEXT);
            CREATE TABLE minute_bars(asset_symbol TEXT,bar_time TEXT,interval_minutes INTEGER,
              open REAL,high REAL,low REAL,close REAL,volume REAL,amount REAL,bar_kind TEXT,
              source_id INTEGER,captured_at TEXT);
            CREATE TABLE market_daily_bars(asset_symbol TEXT,trade_date TEXT,adjust_mode TEXT,
              open REAL,high REAL,low REAL,close REAL,volume REAL,amount REAL,source_id INTEGER,
              captured_at TEXT);
            CREATE TABLE assets(id INTEGER PRIMARY KEY,symbol TEXT);
            CREATE TABLE prices(asset_id INTEGER,trade_date TEXT,open REAL,high REAL,low REAL,
              close REAL,volume REAL,amount REAL);
            INSERT INTO data_sources VALUES(1,'tencent');
            INSERT INTO minute_bars VALUES('600519','2026-08-26T10:00:00+08:00',5,1,1,1,1,1,1,'OHLC',1,'2026-08-26');
            INSERT INTO market_daily_bars VALUES('600519','2026-08-26','qfq',1,1,1,1,1,1,1,'2026-08-26');
        """)
        with patch.dict(os.environ, {"ARGUS_MARKET_PROVIDER": "tdx"}):
            self.assertEqual(_minute_source_rows(conn, "600519", 5), [])
            self.assertEqual(_daily_source_rows(conn, "600519"), [])
        conn.close()


if __name__ == "__main__":
    unittest.main()
