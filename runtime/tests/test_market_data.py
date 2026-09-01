import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch

from app.data_sources.market import (
    _failure_artifact_name,
    _tdx_quote_item,
    _tdx_observed_at,
    _tdx_observable_bar_time,
    _tdx_time_rows,
    eastmoney_secid,
    fetch_eastmoney_history_minutes,
    fetch_baostock_history_minutes,
    fetch_tencent_daily,
    fetch_tencent_minutes,
    fetch_tencent_quotes,
    market_provider_mode,
    normalize_symbol,
    provider_code,
)


class MarketDataContractTests(unittest.TestCase):
    def test_failure_artifact_name_is_bounded_for_large_symbol_batches(self):
        symbols = ",".join(f"{index:06d}" for index in range(200))
        name = _failure_artifact_name("tdx_public", "realtime_quotes", symbols)
        self.assertLess(len(name), 100)
        self.assertTrue(name.startswith("tdx_public_realtime_quotes_"))
        self.assertTrue(name.endswith(".json"))

    def test_tdx_is_the_configured_market_provider(self):
        with patch.dict(os.environ, {"ARGUS_MARKET_PROVIDER": "tdx"}):
            self.assertEqual(market_provider_mode(), "tdx")

    def test_tdx_quote_normalizes_lots_to_shares(self):
        item = _tdx_quote_item("601988", {
            "price": 6.17, "last_close": 6.14, "open": 6.12,
            "high": 6.23, "low": 6.11, "vol": 1510150,
            "amount": 933926272, "servertime": "11:05:32",
        }, "中国银行")
        self.assertEqual(item["source"], "tdx_public")
        self.assertEqual(item["name"], "中国银行")
        self.assertEqual(item["volume"], 151015000)
        self.assertAlmostEqual(item["change_pct"], (6.17 / 6.14 - 1) * 100)

    def test_tdx_invalid_server_second_falls_back_to_local_clock(self):
        observed_at = _tdx_observed_at("11:39:72")
        self.assertNotIn(":72", observed_at)

    def test_tdx_previous_close_clock_is_not_stamped_into_the_future(self):
        before_open = datetime.fromisoformat("2026-09-01T01:08:00+08:00")
        observed_at = _tdx_observed_at("15:29:28", before_open)
        self.assertEqual(observed_at, "2026-08-31T15:29:28+08:00")
        monday_before_open = datetime.fromisoformat("2026-09-07T08:00:00+08:00")
        observed_at = _tdx_observed_at("15:00:00", monday_before_open)
        self.assertEqual(observed_at, "2026-09-04T15:00:00+08:00")

    def test_tdx_time_points_are_sorted_and_normalized(self):
        rows = _tdx_time_rows("601988", "2026-08-26", [
            {"time": "09:32", "price": 6.15, "vol": 20},
            {"time": "09:31", "price": 6.14, "vol": 10},
        ])
        self.assertTrue(rows[0]["bar_time"].endswith("09:31:00+08:00"))
        self.assertEqual(rows[0]["volume"], 1000)
        self.assertEqual(rows[0]["bar_kind"], "LAST_PRICE_POINT")

    def test_tdx_history_does_not_persist_future_intraday_bars(self):
        observed_at = datetime.fromisoformat("2026-08-26T11:30:00+08:00")
        self.assertIsNotNone(_tdx_observable_bar_time("2026-08-26 11:30", observed_at))
        self.assertIsNone(_tdx_observable_bar_time("2026-08-26 13:00", observed_at))

    def test_baostock_does_not_claim_one_minute_support(self):
        with self.assertRaises(ValueError):
            fetch_baostock_history_minutes("600519", 1)
    def test_symbol_mapping_distinguishes_index_and_stock(self):
        self.assertEqual(provider_code("000001.SH"), "sh000001")
        self.assertEqual(provider_code("000001"), "sz000001")
        self.assertEqual(provider_code("600519"), "sh600519")
        self.assertEqual(eastmoney_secid("512400"), "1.512400")
        with self.assertRaises(ValueError):
            normalize_symbol("../../bad")

    @patch("app.data_sources.market._request")
    def test_tencent_quote_contract(self, request):
        fields = [""] * 39
        fields[0:7] = ["1", "测试股票", "600519", "1346.50", "1348.86", "1348.00", "27073"]
        fields[30:35] = ["20260811161431", "-2.36", "-0.17", "1352.65", "1338.00"]
        fields[37] = "364005"
        fields[38] = "6.25"
        request.return_value = f'v_sh600519="{"~".join(fields)}";'.encode("gbk")
        rows, _ = fetch_tencent_quotes(["600519"])
        self.assertEqual(rows[0]["name"], "测试股票")
        self.assertEqual(rows[0]["price"], 1346.5)
        self.assertEqual(rows[0]["change_pct"], -0.17)
        self.assertEqual(rows[0]["turnover_rate"], 6.25)
        self.assertTrue(rows[0]["observed_at"].endswith("+08:00"))

    @patch("app.data_sources.market._request")
    def test_tencent_minute_volume_is_delta_and_kind_is_explicit(self, request):
        payload = {"data": {"sh600519": {"data": [{
            "date": "20260811",
            "data": ["0930 10.00 100 1000.00", "0931 10.10 160 1610.00"],
        }]}}}
        request.return_value = json.dumps(payload).encode()
        rows, _ = fetch_tencent_minutes("600519")
        self.assertEqual([row["volume"] for row in rows], [100.0, 60.0])
        self.assertEqual(rows[1]["amount"], 610.0)
        self.assertEqual(rows[0]["bar_kind"], "LAST_PRICE_POINT")

    @patch("app.data_sources.market._request")
    def test_tencent_daily_pages_backwards_past_provider_cap(self, request):
        def page(start, count):
            rows = []
            for day in range(start, start + count):
                date = f"2024-{((day - 1) // 28) + 1:02d}-{((day - 1) % 28) + 1:02d}"
                rows.append([date, "10", "10.2", "10.3", "9.9", "100"])
            return json.dumps({"data": {"sh600519": {"qfqday": rows}}}).encode()

        request.side_effect = [page(29, 30), page(1, 28), json.dumps({"data": {"sh600519": {}}}).encode()]
        rows, raw = fetch_tencent_daily("600519", count=50)
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0]["trade_date"], "2024-01-09")
        self.assertIn(b'"pages"', raw)
        self.assertIn(",2024-01-31,640,qfq", request.call_args_list[1].args[0])

    @patch("app.data_sources.market._request")
    def test_eastmoney_native_history_contract(self, request):
        payload = {"data": {"klines": [
            "2023-08-14 10:00,10.0,10.2,10.3,9.9,100,1010,0,0,0,0",
            "2023-08-14 10:30,10.2,10.1,10.4,10.0,120,1212,0,0,0,0",
        ]}}
        request.return_value = json.dumps(payload).encode()
        rows, _ = fetch_eastmoney_history_minutes("512400", 30, "20230812", "20260812")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["interval_minutes"], 30)
        self.assertEqual(rows[0]["bar_kind"], "OHLC")
        self.assertIn("klt=30", request.call_args.args[0])
        self.assertIn("secid=1.512400", request.call_args.args[0])

    @patch("app.data_sources.market._request")
    def test_eastmoney_accepts_every_native_history_interval(self, request):
        request.return_value = json.dumps({"data": {"klines": [
            "2023-08-14 10:00,10.0,10.2,10.3,9.9,100,1010,0,0,0,0",
        ]}}).encode()
        for interval in (1, 5, 15, 30, 60):
            rows, _ = fetch_eastmoney_history_minutes("600519", interval, "20230812", "20260812")
            self.assertEqual(rows[0]["interval_minutes"], interval)


if __name__ == "__main__":
    unittest.main()
