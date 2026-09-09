import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.db import connect, initialize
from app.fundamentals import (
    _date,
    PROFILE_RULES,
    fundamental_snapshot,
    load_fundamental_timelines,
    refresh_fundamental_snapshots,
    valuation_availability_date,
)
from app import strategy_evolution


def report(symbol="600519", report_date="2025-12-31", notice_date="2026-03-31", **values):
    row = {
        "symbol": symbol, "report_date": report_date, "notice_date": notice_date,
        "report_type": "年报", "source_code": "test_finance", "observed_at": "2026-04-01",
        "revenue": 100.0, "net_profit": 20.0, "deduct_net_profit": 18.0,
        "revenue_yoy_pct": 12.0, "net_profit_yoy_pct": 15.0,
        "deduct_net_profit_yoy_pct": 14.0, "roe_pct": 20.0, "roic_pct": 16.0,
        "gross_margin_pct": 70.0, "net_margin_pct": 22.0,
        "current_ratio": 2.0, "quick_ratio": 1.5, "cash_ratio": 1.0,
        "debt_ratio_pct": 25.0, "interest_debt_ratio_pct": 8.0,
        "cashflow_to_profit": 1.1, "fcff": 15.0, "raw_json": "{}",
    }
    row.update(values)
    return row


def valuation(symbol="600519", **values):
    row = {
        "symbol": symbol, "source_code": "test_quote", "observed_at": "2026-04-01",
        "market_cap": 1e12, "pe_ttm": 20.0, "pe_dynamic": 19.0,
        "pb": 4.0, "roe_pct": 20.0, "raw_json": "{}",
    }
    row.update(values)
    return row


class FundamentalTests(unittest.TestCase):
    def test_current_valuation_cannot_be_backdated_during_ingestion_or_lookup(self):
        current = valuation(source_code="eastmoney_quote_profile",
                            observed_at="2026-08-30T17:00:00+00:00")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fundamentals.db"
            initialize(path)
            factory = lambda: connect(path)
            result = refresh_fundamental_snapshots(
                ["600519"], "2026-08-28", report_fetcher=lambda _: [],
                valuation_fetcher=lambda _: dict(current), conn_factory=factory,
            )
            self.assertEqual(result["valuation_asof_by_symbol"], {"600519": "2026-08-31"})
            timeline = load_fundamental_timelines(["600519"], factory)
            self.assertFalse(fundamental_snapshot(timeline, "600519", "2026-08-28", "balanced")["available"])
            self.assertTrue(fundamental_snapshot(timeline, "600519", "2026-08-31", "balanced")["available"])
        legacy = {"valuations": {"600519": [{**current, "asof_date": "2026-08-28"}]}}
        self.assertFalse(fundamental_snapshot(legacy, "600519", "2026-08-28", "balanced")["available"])
        self.assertIsNone(valuation_availability_date({**current, "observed_at": "nan"}))

    def test_explicit_historical_provider_date_is_preserved(self):
        historical = valuation(source_code="dated_historical_provider", asof_date="2026-08-28",
                               observed_at="2026-09-06T00:00:00+00:00")
        self.assertEqual(valuation_availability_date(historical), "2026-08-28")

    def test_invalid_dates_are_rejected_at_ingestion_and_lookup(self):
        for value in (None, float("nan"), "nan", "NaT", "", "2026-02-30"):
            self.assertIsNone(_date(value))
        self.assertEqual(_date("2026-09-04T12:00:00"), "2026-09-04")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fundamentals.db"
            initialize(path)
            factory = lambda: connect(path)
            result = refresh_fundamental_snapshots(
                ["600519"], None, conn_factory=factory,
                report_fetcher=lambda symbol: [report(notice_date="nan"), report()],
            )
            self.assertEqual(result["report_rows"], 1)
            self.assertEqual(len(result["errors"]), 1)
            with closing(factory()) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM fundamental_reports").fetchone()[0], 1)
        snapshot = fundamental_snapshot(
            {"reports": {"600519": [report(notice_date="")]}, "valuations": {}},
            "600519", "2026-09-04", "balanced",
        )
        self.assertFalse(snapshot["available"])

    def test_signal_penalizes_missing_fundamentals_after_dataset_exists(self):
        closes = [100.0 + index for index in range(130)]
        data = {
            "dates": [f"2026-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}" for index in range(130)],
            "closes": {"600519": closes},
            "fundamental_timelines": {
                "reports": {"600519": []}, "valuations": {"600519": []}, "has_data": True,
            },
        }
        mandate = {"max_drawdown_pct": 15, "stop_loss_pct": 8,
                   "trailing_stop_pct": 8, "take_profit_pct": 20,
                   "max_positions": 1, "take_profit_mode": "trailing"}
        params = strategy_evolution._candidate_parameters("balanced", mandate, 0)
        result = strategy_evolution._signal(data, "600519", len(closes), params)
        data["fundamental_timelines"]["has_data"] = False
        unpenalized = strategy_evolution._signal(data, "600519", len(closes), params)
        self.assertFalse(result["eligible"])
        self.assertIn("无可用基本面快照", result["rejection_reasons"])
        self.assertAlmostEqual(
            unpenalized["score"] - result["score"], params["fundamental_weight"]
        )

    def test_refresh_upserts_and_point_in_time_lookup_prevents_leakage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fundamentals.db"
            initialize(path)
            factory = lambda: connect(path)
            fetch_reports = lambda _symbol: [
                report(notice_date="2026-03-31", net_profit_yoy_pct=10.0),
                report(report_date="2026-06-30", notice_date="2026-08-15",
                       net_profit_yoy_pct=35.0),
            ]
            for _ in range(2):
                refresh_fundamental_snapshots(
                    ["600519"], "2026-08-28", fetch_reports, lambda _symbol: valuation(), factory
                )
            timeline = load_fundamental_timelines(["600519"], factory)
            before = fundamental_snapshot(timeline, "600519", "2026-08-01", "balanced")
            after = fundamental_snapshot(timeline, "600519", "2026-08-28", "balanced")
            self.assertEqual(before["report_date"], "2025-12-31")
            self.assertEqual(after["report_date"], "2026-06-30")
            with closing(factory()) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM fundamental_reports").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM fundamental_valuations").fetchone()[0], 1)

    def test_profile_weights_make_safety_more_important_for_conservative(self):
        self.assertGreater(
            PROFILE_RULES["conservative"]["dimensions"]["safety"],
            PROFILE_RULES["aggressive"]["dimensions"]["safety"],
        )
        self.assertGreater(
            PROFILE_RULES["aggressive"]["dimensions"]["growth"],
            PROFILE_RULES["conservative"]["dimensions"]["growth"],
        )
        self.assertGreater(
            PROFILE_RULES["conservative"]["fundamental_weight"],
            PROFILE_RULES["aggressive"]["fundamental_weight"],
        )

    def test_missing_fundamentals_are_exposed_instead_of_fabricated(self):
        empty = {"reports": {"600519": []}, "valuations": {"600519": []}, "has_data": True}
        result = fundamental_snapshot(empty, "600519", "2026-08-28", "balanced")
        self.assertFalse(result["available"])
        self.assertFalse(result["eligible"])
        self.assertIsNone(result["score"])
        self.assertIn("无可用基本面快照", result["reasons"])

    def test_provider_failure_is_audited_without_aborting_other_symbols(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fundamentals.db"
            initialize(path)
            def fetch(symbol):
                if symbol == "000858":
                    raise RuntimeError("provider unavailable")
                return [report(symbol=symbol)]
            result = refresh_fundamental_snapshots(
                ["600519", "000858"], "2026-08-28", fetch,
                lambda symbol: valuation(symbol=symbol), lambda: connect(path),
            )
            self.assertEqual(result["status"], "SUCCESS_WITH_WARNINGS")
            self.assertEqual(result["refreshed"], 1)
            self.assertEqual(result["errors"][0]["symbol"], "000858")


if __name__ == "__main__":
    unittest.main()
