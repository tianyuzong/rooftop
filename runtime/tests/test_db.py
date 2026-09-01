import json
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from app import db
from app.db import connect, initialize


class DatabaseTests(unittest.TestCase):
    def test_connection_waits_for_concurrent_writers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)

    def test_read_connection_does_not_request_journal_mode_write_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                started = time.monotonic()
                with closing(connect(path)) as reader:
                    self.assertEqual(reader.execute("SELECT 1").fetchone()[0], 1)
                elapsed = time.monotonic() - started
                writer.rollback()
            self.assertLess(elapsed, 1.0)

    def test_tdx_is_enabled_as_the_chart_market_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                sources = {row[0]: row[1] for row in conn.execute(
                    "SELECT code,enabled FROM data_sources WHERE code IN ('tdx_public','tdx_local','tencent','eastmoney','baostock')"
                )}
        self.assertEqual(sources["tdx_public"], 1)
        self.assertEqual(sources["tdx_local"], 1)
        self.assertEqual(sources["tencent"], 0)
        self.assertEqual(sources["eastmoney"], 0)
        self.assertEqual(sources["baostock"], 0)

    def test_seed_is_idempotent_and_labels_hypotheses_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            initialize(path)
            with closing(connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 10)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM portfolios").fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM sources WHERE source_type='PRIMARY_USER_DATA'"
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM reports WHERE report_type='SYSTEM_BOOTSTRAP'"
                ).fetchone()[0], 0)
                statuses = {row[0] for row in conn.execute("SELECT status FROM hypotheses")}
                self.assertEqual(statuses, {"UNVERIFIED"})

    def test_legacy_synthetic_portfolio_is_removed_without_deleting_real_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                portfolio_id = conn.execute(
                    "INSERT INTO portfolios(name,as_of) VALUES('当前持仓','2026-08-11')"
                ).lastrowid
                asset_ids = {
                    row["symbol"]: row["id"] for row in conn.execute(
                        "SELECT id,symbol FROM assets WHERE symbol IN ('512400','562500','000001.SH')"
                    )
                }
                conn.executemany(
                    """INSERT INTO positions
                       (portfolio_id,asset_id,quantity,cost_price,current_price,highest_since_entry)
                       VALUES(?,?,?,?,?,?)""",
                    [
                        (portfolio_id, asset_ids["512400"], 1000, 1.922 / .969, 1.953, 2.06),
                        (portfolio_id, asset_ids["562500"], 4900, 1.021 / 1.0339, 1.021, 1.065),
                        (portfolio_id, asset_ids["000001.SH"], 10, 3500, 3600, 3650),
                    ],
                )
                source_id = conn.execute(
                    """INSERT INTO sources
                       (name,url,source_type,reliability,verification_status,checked_at,notes)
                       VALUES(?,?,?,?,?,?,?)""",
                    ("用户提供的持仓快照", "local://user-input/2026-08-11",
                     "PRIMARY_USER_DATA", .95, "USER_ATTESTED", "2026-08-11", "legacy"),
                ).lastrowid
                conn.execute(
                    """INSERT INTO evidence
                       (claim,label,status,source_id,observed_at,captured_at,independent_check)
                       VALUES(?,?,?,?,?,?,?)""",
                    ("截至 2026-08-11 晚持有 512400 与 562500 两只 A 股 ETF", "事实",
                     "PARTIALLY_VERIFIED", source_id, "2026-08-11", "2026-08-11", "legacy"),
                )
                conn.execute(
                    "INSERT INTO reports(title,report_type,body,created_at,evidence_coverage) VALUES(?,?,?,?,?)",
                    ("初始投资纪律与待验证假设", "SYSTEM_BOOTSTRAP",
                     json.dumps({"fact_opinion_separation": True, "order_execution": False}, ensure_ascii=False),
                     "2026-08-11", .18),
                )
                conn.execute(
                    "DELETE FROM app_migrations WHERE migration_key='20260901_remove_synthetic_portfolio_snapshot'"
                )
                conn.commit()
            db._INITIALIZED_PATHS.discard(str(path.resolve()))
            initialize(path)
            with closing(connect(path)) as conn:
                symbols = [row[0] for row in conn.execute(
                    """SELECT a.symbol FROM positions p JOIN assets a ON a.id=p.asset_id
                       ORDER BY a.symbol"""
                )]
                self.assertEqual(symbols, ["000001.SH"])
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM sources WHERE url='local://user-input/2026-08-11'"
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM reports WHERE report_type='SYSTEM_BOOTSTRAP'"
                ).fetchone()[0], 0)

    def test_disabled_features_do_not_create_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn("orders", names)
                self.assertNotIn("broker_accounts", names)
                self.assertNotIn("event_graphs", names)
                self.assertNotIn("graph_nodes", names)
                self.assertNotIn("graph_edges", names)
                self.assertNotIn("graph_snapshots", names)
                self.assertNotIn("chat_sessions", names)
                self.assertNotIn("chat_messages", names)
                self.assertNotIn("analysis_runs", names)
                self.assertIn("data_sources", names)
                self.assertIn("data_quality_checks", names)
                self.assertIn("quote_snapshots", names)
                self.assertIn("minute_bars", names)
                self.assertIn("market_daily_bars", names)
                self.assertIn("semantic_documents", names)
                self.assertIn("source_documents", names)
                self.assertIn("source_document_versions", names)
                self.assertIn("report_watchlist", names)
                self.assertIn("comparison_watchlist", names)
                self.assertIn("app_migrations", names)
                self.assertIn("portfolio_imports", names)
                self.assertIn("report_sync_jobs", names)
                self.assertIn("harness_bad_cases", names)
                self.assertIn("harness_candidates", names)
                self.assertIn("harness_evaluations", names)
                self.assertIn("harness_versions", names)
                self.assertIn("harness_events", names)
                self.assertIn("investment_mandates", names)
                self.assertIn("strategy_evolution_runs", names)
                self.assertIn("strategy_evolution_candidates", names)
                self.assertIn("strategy_simulations", names)
                self.assertIn("strategy_evolution_versions", names)
                self.assertIn("harness_learning_cycles", names)
                self.assertIn("sentiment_daily", names)
                self.assertIn("prediction_model_versions", names)
                self.assertIn("daily_predictions", names)
                self.assertIn("prediction_evaluations", names)
                self.assertIn("prediction_backtest_points", names)
                self.assertIn("trading_calendar", names)


if __name__ == "__main__":
    unittest.main()
