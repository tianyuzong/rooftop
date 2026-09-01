import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.data_sources import report_bulk
from app.db import connect, initialize


SAMPLE_ITEM = {
    "title": "公司年度报告点评", "stockName": "测试股份", "stockCode": "600001",
    "orgSName": "测试证券", "publishDate": "2026-08-20T00:00:00",
    "infoCode": "AP_TEST", "emRatingName": "增持", "indvInduName": "测试行业",
}


class BulkReportTests(unittest.TestCase):
    def test_completed_utc_timestamp_is_compared_as_local_date(self):
        china_timezone = timezone(timedelta(hours=8))
        completed = report_bulk._timestamp_local_date("2026-08-25T16:30:00+00:00", china_timezone)
        self.assertEqual(completed, date(2026, 8, 26))

    def test_payload_conversion_preserves_report_identity(self):
        docs = report_bulk._payload_to_documents({"data": [SAMPLE_ITEM]})
        self.assertEqual(docs[0]["metadata"]["股票代码"], "600001")
        self.assertEqual(docs[0]["published_at"], "2026-08-20")
        self.assertIn("AP_TEST", docs[0]["source_url"])

    def test_one_page_job_is_persisted_and_completed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with patch("app.data_sources.report_bulk.connect", lambda: connect(path)), \
                    patch("app.data_sources.report_bulk._request_page_with_retry", return_value={
                        "TotalPage": 1, "hits": 1, "data": [SAMPLE_ITEM],
                    }), \
                    patch("app.data_sources.report_bulk.persist_source_documents", return_value={"rows": 1}):
                _job, should_start = report_bulk._prepare_job("full")
                self.assertTrue(should_start)
                report_bulk._run_bulk_sync("full")
                status = report_bulk.bulk_report_status()["jobs"]["full"]
        self.assertEqual(status["status"], "SUCCESS")
        self.assertEqual(status["processed_pages"], 1)
        self.assertEqual(status["fetched_rows"], 1)
        self.assertEqual(status["progress_pct"], 100.0)

    def test_running_job_is_recoverable_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with patch("app.data_sources.report_bulk.connect", lambda: connect(path)):
                report_bulk.bulk_report_status()
                with closing(connect(path)) as conn:
                    conn.execute("UPDATE report_sync_jobs SET status='RUNNING' WHERE job_key='full'")
                    conn.commit()
                recovered = report_bulk.recover_interrupted_report_sync()
                status = report_bulk.bulk_report_status()["jobs"]["full"]
        self.assertEqual(recovered, ["full"])
        self.assertEqual(status["status"], "INTERRUPTED")

    def test_bulk_recovery_does_not_take_over_daily_material_job(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with patch("app.data_sources.report_bulk.connect", lambda: connect(path)):
                report_bulk.bulk_report_status()
                with closing(connect(path)) as conn:
                    conn.execute(
                        """INSERT INTO report_sync_jobs(job_key,mode,status,updated_at)
                           VALUES('materials_daily','materials_daily','RUNNING','2026-09-01')"""
                    )
                    conn.execute("UPDATE report_sync_jobs SET status='RUNNING' WHERE job_key='full'")
                    conn.commit()
                recovered = report_bulk.recover_interrupted_report_sync()
                with closing(connect(path)) as conn:
                    material_status = conn.execute(
                        "SELECT status FROM report_sync_jobs WHERE job_key='materials_daily'"
                    ).fetchone()[0]
        self.assertEqual(recovered, ["full"])
        self.assertEqual(material_status, "RUNNING")


if __name__ == "__main__":
    unittest.main()
