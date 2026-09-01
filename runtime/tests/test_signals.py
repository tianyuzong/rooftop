import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.db import connect, initialize
from app.signal_service import (
    create_subscription,
    materialize_decision_signals,
    signal_feed,
    update_signal_status,
)


class SignalServiceTests(unittest.TestCase):
    def test_legacy_alert_outbox_is_migrated_before_delivery_index(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    """CREATE TABLE alert_outbox (
                       id INTEGER PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
                       channel TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
                       status TEXT NOT NULL DEFAULT 'PENDING', created_at TEXT NOT NULL,
                       sent_at TEXT, error TEXT)"""
                )
                conn.commit()
            initialize(path)
            with closing(connect(path)) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(alert_outbox)")}
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(alert_outbox)")}
            self.assertIn("next_attempt_at", columns)
            self.assertIn("idx_alert_outbox_delivery", indexes)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "signals.db"
        initialize(self.path)
        self.factory = lambda: connect(self.path)
        with closing(self.factory()) as conn:
            cursor = conn.execute(
                """INSERT INTO quant_mandates
                   (mandate_key,name,status,input_json,created_at,updated_at)
                   VALUES('mandate-test','测试组合','ACTIVE','{}','now','now')"""
            )
            self.mandate_id = int(cursor.lastrowid)
            conn.commit()

    def tearDown(self):
        self.folder.cleanup()

    def _decision(self, version_key="quant-v1", shares=100):
        return {
            "mandate": {"id": self.mandate_id, "mandate_key": "mandate-test",
                        "name": "测试组合"},
            "version": {"version_key": version_key, "status": "ACTIVE", "gate": {}},
            "result": {"recommendation": {
                "data_asof": "2026-08-28", "profile": "balanced",
                "profile_label": "均衡", "model_version": "model-1",
                "positions": [{
                    "symbol": "600519", "name": "贵州茅台", "shares": shares,
                    "weight": 0.2, "reference_price": 1500.0,
                    "stop_price": 1380.0, "take_profit_price": 1800.0,
                    "composite_score": 0.72, "probability_up": 0.68,
                }],
                "research_recommendations": [], "expectation": {},
                "holdout_metrics": {},
            }},
        }

    def test_active_versions_create_buy_then_rebalance_without_duplicates(self):
        first = materialize_decision_signals(self._decision(), conn_factory=self.factory)
        duplicate = materialize_decision_signals(self._decision(), conn_factory=self.factory)
        second = materialize_decision_signals(
            self._decision("quant-v2", shares=200), conn_factory=self.factory
        )
        self.assertEqual(first["signals"][0]["action"], "BUY")
        self.assertEqual(duplicate["created"], 0)
        self.assertEqual(second["signals"][0]["action"], "REBALANCE")
        feed = signal_feed(conn_factory=self.factory)
        self.assertEqual(feed["unread_actionable"], 1)
        old = next(item for item in feed["signals"] if item["action"] == "BUY")
        self.assertEqual(old["status"], "SUPERSEDED")

    def test_subscription_queues_email_and_signal_requires_manual_review(self):
        create_subscription({
            "name": "盘后提醒", "channel": "EMAIL", "target": "research@example.com",
            "event_kinds": ["BUY", "SELL", "REBALANCE"],
        }, self.factory)
        published = materialize_decision_signals(self._decision(), conn_factory=self.factory)
        self.assertEqual(published["queued"], 1)
        signal_id = published["signals"][0]["id"]
        updated = update_signal_status(signal_id, "ACKNOWLEDGED", self.factory)
        self.assertEqual(updated["status"], "ACKNOWLEDGED")
        self.assertFalse(updated["order_execution"])
        with closing(self.factory()) as conn:
            outbox = conn.execute("SELECT * FROM alert_outbox").fetchone()
        self.assertEqual(outbox["target"], "research@example.com")
        self.assertEqual(outbox["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
