import json
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from app import db
from app import research


class ResearchSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "research.db"
        db.initialize(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_strategy_has_one_owned_risk_policy(self):
        original_connect = research.connect
        research.connect = lambda: db.connect(self.path)
        try:
            research.seed_research_catalog()
            with closing(db.connect(self.path)) as conn:
                strategies = conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
                policies = conn.execute("SELECT COUNT(*) FROM strategy_risk_policies").fetchone()[0]
                orphaned = conn.execute(
                    """SELECT COUNT(*) FROM strategies s LEFT JOIN strategy_risk_policies r
                       ON r.strategy_id=s.id WHERE r.id IS NULL"""
                ).fetchone()[0]
                self.assertEqual((strategies, policies, orphaned), (3, 3, 0))
        finally:
            research.connect = original_connect

    def test_factor_and_backtest_audit_tables_exist(self):
        with closing(db.connect(self.path)) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"factors", "factor_runs", "backtest_runs", "strategy_factors"} <= names)

    def test_curve_compaction_keeps_endpoints(self):
        import pandas as pd
        series = pd.Series(range(1000), index=pd.date_range("2023-01-01", periods=1000))
        points = research._curve_points(series, max_points=100)
        self.assertLessEqual(len(points), 101)
        self.assertEqual((points[0]["value"], points[-1]["value"]), (0.0, 999.0))

    def _seed_scorecard_market(self):
        with closing(db.connect(self.path)) as conn:
            source_id = conn.execute(
                "SELECT id FROM data_sources WHERE code='tdx_public'"
            ).fetchone()[0]
            for index in range(80):
                trade_date = (date(2026, 1, 2) + timedelta(days=index)).isoformat()
                close = 100 + index * .35
                conn.execute(
                    """INSERT INTO market_daily_bars
                       (asset_symbol,trade_date,adjust_mode,open,high,low,close,volume,
                        amount,source_id,captured_at,raw_path)
                       VALUES(?,?,'qfq',?,?,?,?,?,?,?,'2026-04-01','test')""",
                    ("600519", trade_date, close - .2, close + 1, close - 1, close,
                     2_000_000 + index * 1000, 80_000_000, source_id),
                )
            conn.execute(
                """INSERT INTO comparison_watchlist
                   (symbol,name,first_compared_at,last_compared_at,compare_count)
                   VALUES('600519','贵州茅台','2026-01-01','2026-04-01',1)"""
            )
            conn.commit()

    def test_composite_score_does_not_turn_missing_fundamentals_into_zero(self):
        self._seed_scorecard_market()
        payload = research.composite_factor_payload(lambda: db.connect(self.path))
        card = payload["models"]["balanced"]["scorecards"][0]
        self.assertIsNone(card["score"])
        self.assertIsNotNone(card["provisional_score"])
        self.assertIn("基本面", card["missing_groups"])
        self.assertEqual(card["status_label"], "缺少基本面数据")

    def test_composite_score_requires_real_fundamentals_and_exposes_factor_catalog(self):
        self._seed_scorecard_market()
        as_of = (date(2026, 1, 2) + timedelta(days=79)).isoformat()
        with closing(db.connect(self.path)) as conn:
            conn.execute(
                """INSERT INTO fundamental_reports
                   (symbol,report_date,notice_date,report_type,source_code,observed_at,
                    revenue,net_profit,deduct_net_profit,revenue_yoy_pct,net_profit_yoy_pct,
                    deduct_net_profit_yoy_pct,roe_pct,roic_pct,gross_margin_pct,net_margin_pct,
                    current_ratio,quick_ratio,cash_ratio,debt_ratio_pct,interest_debt_ratio_pct,
                    cashflow_to_profit,fcff,raw_json)
                   VALUES('600519','2025-12-31','2026-02-01','年报','test','2026-02-01',
                    100,20,18,18,20,17,22,18,70,24,2,1.5,1,24,7,1.2,15,'{}')"""
            )
            conn.execute(
                """INSERT INTO fundamental_valuations
                   (symbol,asof_date,source_code,observed_at,market_cap,pe_ttm,pe_dynamic,pb,roe_pct,raw_json)
                   VALUES('600519',?,'test','2026-03-22',1000000000000,20,19,4,22,'{}')""", (as_of,),
            )
            conn.execute(
                """INSERT INTO sentiment_daily
                   (symbol,trade_date,score,confidence,document_count,positive_count,
                    negative_count,source_breakdown_json,created_at,updated_at)
                   VALUES('600519',?,.4,.8,10,7,2,'{}','2026-03-22','2026-03-22')""", (as_of,),
            )
            conn.commit()
        payload = research.composite_factor_payload(lambda: db.connect(self.path))
        card = payload["models"]["balanced"]["scorecards"][0]
        self.assertIsNotNone(card["score"])
        self.assertTrue(card["eligible"])
        self.assertEqual(card["coverage"], 1.0)
        self.assertTrue(any(item["key"] == "atr_14" for item in payload["factor_catalog"]))
        self.assertTrue(any(item["status"] == "PLANNED" for item in payload["factor_catalog"]))


class SourceStatusTests(unittest.TestCase):
    def test_no_credentials_are_returned(self):
        from app.data_sources.intelligence import source_access_status
        with patch.dict("os.environ", {"ARGUS_X_BEARER_TOKEN": "secret", "ARGUS_SEC_IDENTITY": "a@b.com"}):
            encoded = json.dumps(source_access_status())
        self.assertNotIn("secret", encoded)
        self.assertNotIn("a@b.com", encoded)


if __name__ == "__main__":
    unittest.main()
