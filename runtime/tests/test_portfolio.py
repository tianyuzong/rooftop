import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app import portfolio, server
from app.db import connect, initialize


class PortfolioImportTests(unittest.TestCase):
    def _database(self, folder: str) -> Path:
        path = Path(folder) / "test.db"
        initialize(path)
        return path

    def _add_quote(self, path: Path, symbol: str, name: str, price: float) -> None:
        with closing(connect(path)) as conn:
            source_id = conn.execute(
                "SELECT id FROM data_sources WHERE code='tdx_public'"
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO quote_snapshots
                   (asset_symbol,asset_name,observed_at,price,source_id,captured_at,raw_path)
                   VALUES(?,?,?,?,?,?,?)""",
                (symbol, name, f"{date.today().isoformat()}T15:00:00+08:00", price,
                 source_id, f"{date.today().isoformat()}T15:01:00+08:00", "test.json"),
            )
            conn.commit()

    def test_manual_import_requires_preview_and_user_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._database(folder)
            self._add_quote(path, "600519", "贵州茅台", 1300.0)
            payload = {
                "mode": "manual", "account_name": "长期账户",
                "as_of": date.today().isoformat(),
                "positions": [{"symbol": "600519", "quantity": 10, "cost_price": 1200}],
            }
            with patch.object(portfolio, "connect", side_effect=lambda: connect(path)):
                preview = portfolio.preview_portfolio_import(payload)
                self.assertTrue(preview["can_confirm"])
                self.assertEqual(preview["reconciliation"]["added"], 1)
                self.assertEqual(preview["rows"][0]["valuation"]["price"], 1300.0)
                with self.assertRaisesRegex(ValueError, "明确确认"):
                    portfolio.confirm_portfolio_import({"preview_id": preview["preview_id"]})
                result = portfolio.confirm_portfolio_import({
                    "preview_id": preview["preview_id"], "confirmed": True,
                })
            self.assertEqual(result["position_count"], 1)
            self.assertFalse(result["order_execution"])
            with closing(connect(path)) as conn:
                row = conn.execute(
                    "SELECT * FROM positions"
                ).fetchone()
                self.assertEqual(row["verification_status"], "USER_CONFIRMED")
                self.assertEqual(row["valuation_status"], "AVAILABLE")
                self.assertEqual(row["price_source"], "tdx_public")
                self.assertEqual(conn.execute(
                    "SELECT status FROM portfolio_imports"
                ).fetchone()[0], "CONFIRMED")

    def test_unpriced_position_never_uses_cost_as_market_value(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._database(folder)
            payload = {
                "mode": "manual", "as_of": date.today().isoformat(),
                "positions": [{"symbol": "000001", "name": "平安银行",
                               "quantity": 100, "cost_price": 12.34}],
            }
            with patch.object(portfolio, "connect", side_effect=lambda: connect(path)):
                preview = portfolio.preview_portfolio_import(payload)
                portfolio.confirm_portfolio_import({
                    "preview_id": preview["preview_id"], "confirmed": True,
                })
            with patch.object(server, "connect", side_effect=lambda: connect(path)), \
                    patch.object(server, "strategy_lab_payload", return_value={}), \
                    patch.object(server, "source_access_status", return_value=[]), \
                    patch.object(server, "smtp_status", return_value={}):
                dashboard = server.dashboard_payload()
            self.assertTrue(dashboard["portfolio"]["has_positions"])
            self.assertEqual(dashboard["portfolio"]["valuation_status"], "UNAVAILABLE")
            self.assertIsNone(dashboard["portfolio"]["market_value"])
            self.assertIsNone(dashboard["portfolio"]["unrealized_pnl"])
            self.assertIsNone(dashboard["portfolio"]["positions"][0]["market_value"])

    def test_csv_preview_validates_headers_rows_and_reconciliation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._database(folder)
            with patch.object(portfolio, "connect", side_effect=lambda: connect(path)):
                invalid = portfolio.preview_portfolio_import({
                    "mode": "csv", "filename": "持仓.csv",
                    "csv_text": "股票代码,持仓数量,成本价\n600519,10,1200\n600519,2,1300",
                })
                self.assertFalse(invalid["can_confirm"])
                self.assertIn("重复股票代码", invalid["errors"][-1]["message"])
                valid = portfolio.preview_portfolio_import({
                    "mode": "csv", "filename": "持仓.csv",
                    "csv_text": "股票代码,股票名称,持仓数量,成本价\n600519,贵州茅台,10,1200",
                })
                portfolio.confirm_portfolio_import({
                    "preview_id": valid["preview_id"], "confirmed": True,
                })
                changed = portfolio.preview_portfolio_import({
                    "mode": "manual", "positions": [
                        {"symbol": "600519", "quantity": 12, "cost_price": 1200},
                        {"symbol": "300750", "quantity": 20, "cost_price": 350},
                    ],
                })
            self.assertEqual(changed["reconciliation"]["changed"], 1)
            self.assertEqual(changed["reconciliation"]["added"], 1)
            self.assertEqual(changed["reconciliation"]["removed"], 0)

    def test_clear_portfolio_keeps_an_audit_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._database(folder)
            with patch.object(portfolio, "connect", side_effect=lambda: connect(path)):
                preview = portfolio.preview_portfolio_import({
                    "mode": "manual", "positions": [
                        {"symbol": "600519", "quantity": 10, "cost_price": 1200},
                    ],
                })
                portfolio.confirm_portfolio_import({
                    "preview_id": preview["preview_id"], "confirmed": True,
                })
                with self.assertRaisesRegex(ValueError, "明确确认"):
                    portfolio.clear_portfolio({})
                cleared = portfolio.clear_portfolio({"confirmed": True})
            self.assertEqual(cleared["removed"], 1)
            with closing(connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)
                latest = conn.execute(
                    "SELECT status,source_type FROM portfolio_imports ORDER BY id DESC LIMIT 1"
                ).fetchone()
                self.assertEqual((latest["status"], latest["source_type"]),
                                 ("CONFIRMED", "MANUAL_CLEAR"))


if __name__ == "__main__":
    unittest.main()
