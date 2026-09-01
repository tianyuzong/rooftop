import math
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import continuous_learning, deep_learning
from app.db import connect, initialize


class DeepLearningTests(unittest.TestCase):
    @staticmethod
    def _fake_qwen_runtime():
        import torch

        class FakeBackbone(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.config = SimpleNamespace(hidden_size=16)
                self.transform = torch.nn.Linear(16, 16, bias=False)

            def forward(self, inputs_embeds, **_kwargs):
                return SimpleNamespace(
                    last_hidden_state=torch.tanh(self.transform(inputs_embeds))
                )

        return (
            FakeBackbone(),
            torch.device("cpu"),
            torch.float32,
            {
                "model_id": deep_learning.QWEN_MODEL_ID,
                "model_path": "test-double",
                "weights_sha256": deep_learning.QWEN_WEIGHTS_SHA256,
                "weights_size": deep_learning.QWEN_WEIGHTS_SIZE,
                "config_sha256": "0" * 64,
                "model_type": "qwen3",
                "device": "cpu",
                "device_name": "CPU",
                "dtype": "float32",
                "parameter_count": 256,
                "load_seconds": 0.0,
            },
        )

    @staticmethod
    def _seed(path, rows=220):
        with closing(connect(path)) as conn:
            source_id = conn.execute(
                "SELECT id FROM data_sources WHERE code='tdx_public'"
            ).fetchone()[0]
            values = []
            for offset, symbol in enumerate(("600519", "000858", "300750")):
                previous = 100.0 - offset * 8
                for index in range(rows):
                    day = (date(2025, 1, 2) + timedelta(days=index)).isoformat()
                    close = previous * (1.0002 + 0.004 * math.sin(index / 8 + offset))
                    values.append((symbol, day, previous, max(previous, close) * 1.005,
                                   min(previous, close) * 0.995, close,
                                   10_000_000 + index * 1000, source_id,
                                   "2026-01-01T00:00:00+00:00", "test"))
                    previous = close
            conn.executemany(
                """INSERT INTO market_daily_bars
                   (asset_symbol,trade_date,adjust_mode,open,high,low,close,volume,
                    source_id,captured_at,raw_path)
                   VALUES(?,?,'qfq',?,?,?,?,?,?,?,?)""", values)
            conn.commit()

    def test_sequences_end_at_signal_date_before_target(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "deep.db"
            initialize(path)
            self._seed(path)
            with patch.multiple(continuous_learning, connect=lambda: connect(path),
                                initialize=lambda: initialize(path)):
                samples = deep_learning.build_sequence_samples(["600519", "000858", "300750"])
            self.assertGreater(len(samples), 400)
            self.assertTrue(all(item["signal_date"] < item["target_date"] for item in samples))
            self.assertTrue(all(len(item["sequence"]) == deep_learning.SEQUENCE_LENGTH
                                for item in samples))

    def test_qwen3_adapter_and_chronological_holdout_are_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "deep.db"
            initialize(path)
            self._seed(path)
            with patch.multiple(continuous_learning, connect=lambda: connect(path),
                                initialize=lambda: initialize(path)), patch.multiple(
                deep_learning, connect=lambda: connect(path), initialize=lambda: initialize(path),
                DATA_LAKE=root,
                _load_qwen_runtime=self._fake_qwen_runtime,
            ):
                result = deep_learning.train_deep_model(
                    ["600519", "000858", "300750"], auto_promote=False, epochs=4)
            self.assertIn(result["status"], {"CANDIDATE", "REJECTED"})
            self.assertTrue(Path(result["artifact_path"]).exists())
            self.assertTrue(result["gate"]["strict_time_split"])
            self.assertTrue(result["gate"]["model_family_qwen3"])
            self.assertGreaterEqual(result["metrics"]["holdout"]["sample_count"], 40)
            import torch
            artifact = torch.load(
                result["artifact_path"], map_location="cpu", weights_only=False
            )
            self.assertEqual(artifact["base_model_id"], deep_learning.QWEN_MODEL_ID)
            self.assertIn("adapter_state_dict", artifact)
            self.assertFalse(any(
                key.startswith("backbone.") for key in artifact["adapter_state_dict"]
            ))
            with closing(connect(path)) as conn:
                row = conn.execute("SELECT * FROM deep_model_versions").fetchone()
            self.assertEqual(row["architecture"], deep_learning.ARCHITECTURE)
            self.assertEqual(len(row["artifact_hash"]), 64)


if __name__ == "__main__":
    unittest.main()
