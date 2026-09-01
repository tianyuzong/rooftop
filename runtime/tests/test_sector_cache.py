import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app import sector_cache
from app.db import connect, initialize


class SectorCacheTests(unittest.TestCase):
    def test_target_market_date_uses_last_completed_session(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "cache.db"
            initialize(db_path)
            with closing(connect(db_path)) as conn:
                conn.executemany(
                    """INSERT OR REPLACE INTO trading_calendar
                       (trade_date,is_open,source,updated_at)
                       VALUES(?,1,'test','now')""",
                    [("2026-09-01",), ("2026-09-02",)],
                )
                conn.commit()
            with patch.object(sector_cache, "connect", lambda: connect(db_path)):
                pre_open = datetime(
                    2026, 9, 2, 1, 0, tzinfo=sector_cache.SHANGHAI,
                )
                post_close = datetime(
                    2026, 9, 2, 15, 20, tzinfo=sector_cache.SHANGHAI,
                )
                self.assertEqual(sector_cache._target_market_date(pre_open), "2026-09-01")
                self.assertEqual(sector_cache._target_market_date(post_close), "2026-09-02")

    def test_trigger_persists_every_sector_member(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "cache.db"
            initialize(db_path)
            members = [
                {"symbol": "300390", "name": "天华新能"},
                {"symbol": "688503", "name": "聚和材料"},
                {"symbol": "600519", "name": "贵州茅台"},
            ]
            with patch.multiple(
                sector_cache,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
                _resolved_sector_members=lambda sectors: (
                    {"codes": ["T030501", "T0706"], "names": list(sectors),
                     "unmatched": []},
                    members,
                ),
                _target_market_date=lambda: "2026-08-28",
                _ensure_worker=lambda: None,
            ):
                job = sector_cache.trigger_sector_cache(["白酒", "新能源"])
                self.assertEqual(job["status"], "QUEUED")
                self.assertEqual(job["total_symbols"], 3)
                self.assertEqual(job["retention_start"], "2025-08-28")
                with closing(connect(db_path)) as conn:
                    items = conn.execute(
                        "SELECT symbol,market_status,fundamental_status "
                        "FROM sector_cache_items ORDER BY symbol"
                    ).fetchall()
                self.assertEqual([row["symbol"] for row in items], [
                    "300390", "600519", "688503",
                ])
                self.assertTrue(all(row["market_status"] == "PENDING" for row in items))

    def test_recovery_requeues_interrupted_job(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "cache.db"
            initialize(db_path)
            with closing(connect(db_path)) as conn:
                conn.execute(
                    """INSERT INTO sector_cache_jobs
                       (job_key,status,sectors_json,sector_codes_json,target_asof,
                        retention_start,total_symbols,requested_at,updated_at)
                       VALUES('job','RUNNING_MARKET','[]','[]','2026-08-28',
                              '2025-08-28',0,'now','now')"""
                )
                conn.commit()
            with patch.multiple(
                sector_cache,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
                _target_market_date=lambda: "2026-08-28",
                _ensure_worker=lambda: None,
            ):
                self.assertEqual(sector_cache.recover_sector_cache_jobs(), 1)
            with closing(connect(db_path)) as conn:
                status = conn.execute(
                    "SELECT status FROM sector_cache_jobs WHERE job_key='job'"
                ).fetchone()[0]
            self.assertEqual(status, "QUEUED")

    def test_recovery_cancels_job_for_unfinished_trading_day(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "cache.db"
            initialize(db_path)
            with closing(connect(db_path)) as conn:
                conn.execute(
                    """INSERT INTO sector_cache_jobs
                       (job_key,status,sectors_json,sector_codes_json,target_asof,
                        retention_start,total_symbols,requested_at,updated_at)
                       VALUES('future','RUNNING_MARKET','[]','[]','2026-09-02',
                              '2025-09-02',0,'now','now')"""
                )
                conn.commit()
            with patch.multiple(
                sector_cache,
                connect=lambda: connect(db_path),
                initialize=lambda: initialize(db_path),
                _target_market_date=lambda: "2026-09-01",
                _ensure_worker=lambda: None,
            ):
                self.assertEqual(sector_cache.recover_sector_cache_jobs(), 0)
            with closing(connect(db_path)) as conn:
                row = conn.execute(
                    "SELECT status,last_error FROM sector_cache_jobs WHERE job_key='future'"
                ).fetchone()
            self.assertEqual(row["status"], "CANCELLED")
            self.assertIn("2026-09-01", row["last_error"])


if __name__ == "__main__":
    unittest.main()
