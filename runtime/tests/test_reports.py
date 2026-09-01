import unittest
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from app.data_sources.reports import (_rows_to_documents, current_report_watchlist,
                                      refresh_stock_report, register_report_equities)
from app.data_sources import reports
from app.db import connect, initialize


class ReportCollectorTests(unittest.TestCase):
    def test_recent_reports_are_sorted_and_limited(self):
        frame = pd.DataFrame([{"标题": f"报告{i}", "日期": f"2026-01-{(i % 28)+1:02d}"} for i in range(50)])
        docs = _rows_to_documents(frame, "report", "标题", None, "source", "日期", ["日期"])
        self.assertEqual(len(docs), 30)
        self.assertGreaterEqual(docs[0]["published_at"], docs[-1]["published_at"])

    def test_recent_comparisons_drive_the_report_watchlist(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with patch("app.data_sources.reports.connect", lambda: connect(path)):
                register_report_equities([
                    {"symbol": "600030", "name": "中信证券"},
                    {"symbol": "002594", "name": "比亚迪"},
                    {"symbol": "601939", "name": "中国建设银行"},
                ])
                watchlist = current_report_watchlist()
        self.assertEqual(watchlist["research_equities"], ("600030", "002594", "601939"))

    def test_single_stock_report_refresh_validates_and_collects_one_symbol(self):
        with patch("app.data_sources.reports._collect_research_report", return_value={"rows": 12}) as collect:
            result = refresh_stock_report("688681")
        self.assertEqual(result["rows"], 12)
        self.assertEqual(collect.call_args.args[1], "688681")
        with self.assertRaises(ValueError):
            refresh_stock_report("科汇股份")

    def test_single_stock_without_reports_returns_an_empty_success(self):
        with patch("app.data_sources.reports._collect_research_report", side_effect=KeyError("infoCode")), \
                patch("app.data_sources.reports.persist_source_documents", return_value={"rows": 0}) as persist:
            result = refresh_stock_report("688681")
        self.assertEqual(result["rows"], 0)
        self.assertEqual(result["message"], "当前暂无公开券商研报")
        persist.assert_called_once_with("akshare", "research_report", "688681", [], "[]")

    def test_daily_material_status_is_persisted_and_grouped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            now = datetime.now(timezone.utc).isoformat()
            with closing(connect(path)) as conn:
                conn.execute(
                    """INSERT INTO report_watchlist(symbol,name,last_requested_at,enabled)
                       VALUES('600519','贵州茅台',?,1)""", (now,),
                )
                conn.execute(
                    """INSERT INTO source_documents
                       (doc_key,document_type,title,body,captured_at,raw_path,content_hash,metadata_json)
                       VALUES('news-1','a_share_news','测试新闻','正文',?,'news.json','n1','{}')""",
                    (now,),
                )
                conn.commit()
            with patch("app.data_sources.reports.connect", lambda: connect(path)):
                initial = reports.research_material_sync_status()
                with closing(connect(path)) as conn:
                    conn.execute(
                        """INSERT INTO report_sync_jobs(job_key,mode,status,completed_at,updated_at)
                           VALUES('incremental','incremental','SUCCESS',?,?)""", (now, now),
                    )
                    conn.execute(
                        """UPDATE report_sync_jobs SET status='SUCCESS',completed_at=?,updated_at=?
                           WHERE job_key=?""",
                        (now, now, reports.DAILY_MATERIAL_JOB_KEY),
                    )
                    conn.commit()
                completed = reports.research_material_sync_status()
        self.assertFalse(initial["completed_today"])
        self.assertTrue(completed["completed_today"])
        self.assertEqual(completed["status_label"], "今日已更新")
        self.assertEqual(completed["tracked_symbols"], 1)
        self.assertEqual(completed["groups"]["news"]["document_count"], 1)
        self.assertTrue(completed["material_completed_today"])
        self.assertTrue(completed["report_increment_completed_today"])

    def test_daily_refresh_is_skipped_after_today_succeeds(self):
        with patch("app.data_sources.reports.research_material_sync_status", return_value={
            "completed_today": True, "material_completed_today": True,
            "completed_at": "2026-09-01T01:00:00+00:00",
            "groups": {"reports": {"document_count": 3}, "news": {"document_count": 4}},
        }), patch("app.data_sources.reports.refresh_report_library") as refresh:
            result = reports.refresh_report_library_if_due()
        refresh.assert_not_called()
        self.assertEqual(result["status"], "UP_TO_DATE")
        self.assertEqual(result["documents"], 7)

    def test_daily_refresh_includes_reports_news_and_sentiment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with patch("app.data_sources.reports.connect", lambda: connect(path)), \
                    patch("app.data_sources.reports.current_report_watchlist", return_value={
                        "funds": ("512400", "562500"),
                        "research_equities": ("600519", "000858"),
                    }), \
                    patch("app.data_sources.reports._collect_one", return_value={"rows": 2}), \
                    patch("app.data_sources.reports._collect_research_report", return_value={"rows": 3}), \
                    patch("app.data_sources.reports.collect_multisource_sentiment", return_value={
                        "symbols": [{"symbol": "600519"}, {"symbol": "000858"}],
                        "documents": 5, "errors": [],
                    }) as sentiment:
                result = reports.refresh_report_library()
                sync = reports.research_material_sync_status()
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["report_documents"], 12)
        self.assertEqual(result["news_and_sentiment_documents"], 5)
        self.assertEqual(result["documents"], 17)
        self.assertTrue(sync["material_completed_today"])
        self.assertFalse(sync["completed_today"])
        sentiment.assert_called_once()


if __name__ == "__main__":
    unittest.main()
