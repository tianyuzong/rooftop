import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.db import connect, initialize
from app import harness, harness_autonomy


class HarnessTests(unittest.TestCase):
    def _patch_db(self, path):
        return patch.multiple(
            "app.harness",
            connect=lambda: connect(path),
            initialize=lambda: initialize(path),
        )

    def test_bad_case_is_deduplicated_and_counted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with self._patch_db(path):
                first = harness.record_bad_case(
                    "search_no_result", {"query": "红太杨"},
                    {"canonical_query": "000525", "symbol": "000525"},
                    {"result_count": 0},
                )
                second = harness.record_bad_case(
                    "search_no_result", {"query": "红太杨"},
                    {"canonical_query": "000525", "symbol": "000525"},
                    {"result_count": 0},
                )
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["occurrences"], 2)
            self.assertEqual(second["status"], "READY")

    def test_alias_candidate_requires_evaluation_and_human_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with self._patch_db(path):
                case = harness.record_bad_case(
                    "stock_resolution", {"token": "红太杨"},
                    {"symbol": "000525", "name": "红太阳"},
                    {"error": "无法解析"},
                )
                candidate = harness.generate_candidate(case["id"])
                self.assertEqual(candidate["candidate_type"], "stock_alias")
                with self.assertRaises(ValueError):
                    harness.approve_candidate(candidate["id"], "", False)
                evaluation = harness.evaluate_candidate(candidate["id"])
                self.assertEqual(evaluation["status"], "SUCCESS")
                version = harness.approve_candidate(candidate["id"], "测试批准人", True)
                self.assertEqual(version["config"]["stock_aliases"]["红太杨"]["symbol"], "000525")
                baseline = next(item for item in harness.harness_payload()["versions"]
                                if item["version_key"] == "baseline-v1")
                rolled_back = harness.rollback_version(baseline["version_key"], "测试批准人", True)
                self.assertEqual(rolled_back["version_key"], "baseline-v1")

    def test_candidate_without_measurable_improvement_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with self._patch_db(path):
                case = harness.record_bad_case(
                    "stock_resolution", {"token": "贵州茅台"},
                    {"symbol": "600519", "name": "贵州茅台"},
                    {"symbol": "600519"},
                )
                candidate = harness.generate_candidate(case["id"])
                evaluation = harness.evaluate_candidate(candidate["id"])
                self.assertEqual(evaluation["status"], "FAILED")
                self.assertFalse(any(item["improved"] for item in evaluation["details"]))
                with self.assertRaises(ValueError):
                    harness.approve_candidate(candidate["id"], "测试批准人", True)

    def test_each_profile_accepts_its_own_dimension_contract(self):
        from app.stock_compare import PROFILE_DEFINITIONS

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with self._patch_db(path):
                for profile, definition in PROFILE_DEFINITIONS.items():
                    weights = {key: weight for key, _label, weight in definition["weights"]}
                    case = harness.record_bad_case(
                        "ranking_mismatch", {"stocks": ["600519", "000858"], "profile": profile},
                        {"winner": "600519", "profile_weights": weights}, {"winner": "000858"},
                    )
                    candidate = harness.generate_candidate(case["id"])
                    self.assertEqual(candidate["candidate_type"], "profile_weights")
                    self.assertEqual(set(candidate["config"]["profile_weights"][profile]), set(weights))

    def test_autonomous_cycle_requires_approval_for_fullwidth_fix(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            lake = Path(folder) / "lake"
            initialize(path)
            with self._patch_db(path), \
                    patch("app.harness_autonomy.connect", lambda: connect(path)), \
                    patch("app.harness_autonomy.DATA_LAKE", lake), \
                    patch("app.stock_compare._online_name_match", return_value=None):
                result = harness_autonomy.run_autonomous_cycle(auto_apply=True, stock_limit=1)
                self.assertGreaterEqual(result["failed_count"], 2)
                self.assertEqual(result["activated_count"], 0)
                self.assertFalse(result["auto_apply"])
                with closing(connect(path)) as conn:
                    candidate_id = conn.execute(
                        """SELECT id FROM harness_candidates
                           WHERE candidate_type='normalization_rule' ORDER BY id DESC LIMIT 1"""
                    ).fetchone()[0]
                harness.approve_candidate(candidate_id, "测试批准人", True)
                self.assertTrue(harness.active_config()["normalization_rules"]["nfkc"])
                from app.stock_compare import resolve_stock
                self.assertEqual(resolve_stock("５１２４００")["symbol"], "512400")
                self.assertTrue((lake / "research" / "harness-autonomous-latest.json").exists())
                second = harness_autonomy.run_autonomous_cycle(auto_apply=True, stock_limit=1)
                self.assertGreaterEqual(second["reconciled_count"], 0)
                with closing(connect(path)) as conn:
                    status = conn.execute(
                        "SELECT status FROM harness_bad_cases WHERE case_type='input_normalization'"
                    ).fetchone()[0]
                self.assertEqual(status, "RESOLVED")


if __name__ == "__main__":
    unittest.main()
