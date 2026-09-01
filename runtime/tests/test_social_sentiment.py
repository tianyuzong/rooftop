import json
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app import social_sentiment
from app.db import connect, initialize


class SocialSentimentTests(unittest.TestCase):
    def test_multisource_scores_are_fused_and_health_is_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sentiment.db"
            initialize(path)
            with closing(connect(path)) as conn:
                conn.execute(
                    """INSERT INTO a_share_universe_assets
                       (symbol,name,exchange,source,source_asof,updated_at)
                       VALUES('600519','贵州茅台','SH','test','2026-08-29','2026-08-29')""")
                conn.commit()
            news = [{"title": "业绩增长 获增持", "body": "盈利改善", "published_at": "2026-08-29",
                     "source_url": "https://example.test/news", "metadata": {"symbol": "600519"}}]
            participation = [{"title": "参与意愿 60", "body": "聚合指标",
                              "published_at": "2026-08-29", "source_url": "https://example.test/guba",
                              "metadata": {"symbol": "600519", "aggregate_index": True,
                                           "participation": 60.0, "participation_change": 2.0}}]
            bilibili = [{"title": "贵州茅台回暖", "body": "需求改善",
                         "published_at": "2026-08-29", "source_url": "https://example.test/video",
                         "metadata": {"symbol": "600519", "metadata_only": True}}]
            with patch.multiple(
                social_sentiment,
                connect=lambda: connect(path), initialize=lambda: initialize(path),
                persist_source_documents=lambda *args, **kwargs: {"rows": len(args[3])},
                _fetch_news=lambda symbol: (news, "{}"),
                _fetch_participation=lambda symbol, target: (participation, "{}"),
                _fetch_bilibili=lambda name, symbol: (bilibili, "{}"),
            ):
                result = social_sentiment.collect_multisource_sentiment(
                    ["600519"], date(2026, 8, 29))
            self.assertEqual(result["symbols"][0]["source_count"], 3)
            self.assertGreater(result["symbols"][0]["score"], 0)
            statuses = {item["source_code"]: item["status"]
                        for item in result["coverage"]["sources"]}
            self.assertEqual(statuses["bilibili_public"], "HEALTHY")
            self.assertEqual(statuses["x_official"], "UNCONFIGURED")
            with closing(connect(path)) as conn:
                row = conn.execute("SELECT * FROM sentiment_daily WHERE symbol='600519'").fetchone()
            breakdown = json.loads(row["source_breakdown_json"])
            self.assertEqual(set(breakdown), {"akshare_eastmoney_news",
                                              "eastmoney_participation", "bilibili_public"})


if __name__ == "__main__":
    unittest.main()
