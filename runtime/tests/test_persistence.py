import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.db import connect, initialize
from app.persistence import persist_source_documents


class PersistenceTests(unittest.TestCase):
    def test_document_query_keeps_raw_and_normalized_result(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "test.db"
            lake = Path(folder) / "lake"
            initialize(db_path)
            with patch("app.persistence.initialize"), patch("app.persistence.connect", lambda: connect(db_path)), \
                 patch("app.persistence.DATA_LAKE", lake), patch("app.persistence.ensure_data_lake"):
                result = persist_source_documents(
                    "akshare", "news", "AI ETF",
                    [{"title": "测试新闻", "body": "正文", "url": "https://example.test/1"}],
                    json.dumps({"items": [1]}, ensure_ascii=False),
                )
            self.assertTrue(Path(result["raw_path"]).exists())
            with closing(connect(db_path)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_document_versions").fetchone()[0], 1)
                run = conn.execute("SELECT status,row_count,raw_path FROM ingestion_runs ORDER BY id DESC").fetchone()
                self.assertEqual((run["status"], run["row_count"]), ("SUCCESS", 1))
                self.assertTrue(run["raw_path"])


if __name__ == "__main__":
    unittest.main()
