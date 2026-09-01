import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.db import connect, initialize
from app.search import _local_model_snapshot, collect_documents, exact_search


class SearchTests(unittest.TestCase):
    def test_local_model_snapshot_uses_cached_main_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            repository = cache / "models--Qwen--Qwen3-Embedding-0.6B"
            snapshot = repository / "snapshots" / "revision-1"
            snapshot.mkdir(parents=True)
            (snapshot / "config.json").write_text("{}", encoding="utf-8")
            (repository / "refs").mkdir()
            (repository / "refs" / "main").write_text("revision-1\n", encoding="utf-8")

            self.assertEqual(_local_model_snapshot(cache), snapshot)

    def test_compared_stock_is_searchable_on_research_page(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                conn.execute(
                    """INSERT INTO comparison_watchlist
                       (symbol,name,first_compared_at,last_compared_at,compare_count)
                       VALUES('601988','中国银行','2026-08-26','2026-08-26',2)"""
                )
                conn.commit()
            with patch("app.search.connect", lambda: connect(path)):
                results = exact_search("中国银行", page="research")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "中国银行 601988")
        self.assertEqual(results[0]["match_mode"], "exact")

    def test_exact_report_search_stays_in_sql_for_source_documents(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                conn.execute(
                    """INSERT INTO source_documents
                       (doc_key,document_type,title,body,source_name,captured_at,raw_path,content_hash,metadata_json)
                       VALUES('doc-1','broker_research','红太阳研报','股票代码：000525','测试源',
                              '2026-01-01','raw.json','hash',?)""",
                    (json.dumps({"股票代码": "000525"}, ensure_ascii=False),),
                )
                conn.commit()
                self.assertFalse(any(item["doc_key"] == "source-document:1"
                                     for item in collect_documents(conn, include_source_documents=False)))
            with patch("app.search.connect", lambda: connect(path)):
                results = exact_search("000525", page="reports")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "红太阳研报")
        self.assertEqual(results[0]["page"], "reports")

    def test_unified_search_returns_direct_sources_newest_first(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with closing(connect(path)) as conn:
                for key, title, date in (
                    ("old", "新能源旧资料", "2026-01-01"),
                    ("new", "新能源最新资料", "2026-08-28"),
                ):
                    conn.execute(
                        """INSERT INTO source_documents
                           (doc_key,document_type,title,body,source_url,source_name,published_at,
                            captured_at,raw_path,content_hash,metadata_json)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (key, "broker_research", title, "新能源行业跟踪",
                         f"https://example.com/{key}", "测试研报源", date, date,
                         f"{key}.json", key, "{}"),
                    )
                conn.commit()
            with patch("app.search.connect", lambda: connect(path)):
                results = exact_search("新能源", page=None)
        self.assertEqual([item["title"] for item in results], ["新能源最新资料", "新能源旧资料"])
        self.assertEqual(results[0]["source_ref"], "https://example.com/new")
        self.assertEqual(results[0]["source_name"], "测试研报源")
        self.assertEqual(results[0]["published_at"], "2026-08-28")


if __name__ == "__main__":
    unittest.main()
