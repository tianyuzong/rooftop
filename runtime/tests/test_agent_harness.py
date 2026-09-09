import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import agent_harness
from app.db import connect, initialize


class AgentHarnessTests(unittest.TestCase):
    def _patch_db(self, path):
        return patch.multiple(
            "app.agent_harness",
            connect=lambda: connect(path),
            initialize=lambda: initialize(path),
        )

    def test_strategy_activation_is_automatic_but_other_writes_still_require_approval(self):
        strategy_spec = agent_harness.TOOL_SPECS["activate_strategy_experiment"]
        self.assertEqual(strategy_spec["risk_level"], "INTERNAL_WRITE")
        self.assertFalse(strategy_spec["approval_required"])
        self.assertTrue(agent_harness.TOOL_SPECS["activate_candidate"]["approval_required"])
        self.assertTrue(agent_harness.TOOL_SPECS["rollback_version"]["approval_required"])
        plan = agent_harness._build_plan(
            "strategy_activation", {"experiment_key": "experiment_test"}
        )
        self.assertIn("自动激活", plan[-1]["summary"])

    @staticmethod
    def _successful_stock_tool(tool_name, arguments):
        if tool_name == "resolve_stocks":
            return {"stocks": [
                {"input": "600519", "symbol": "600519", "name": "贵州茅台"},
                {"input": "000858", "symbol": "000858", "name": "五粮液"},
            ]}
        if tool_name == "compare_stocks":
            return {
                "as_of": "2026-08-28",
                "verdict": {"winner": "600519", "headline": "中间派当前优先研究：贵州茅台（600519）",
                            "reason": "质量与回撤控制更占优。"},
                "ranking": [{"symbol": "600519", "name": "贵州茅台", "invalidation": "基本面恶化"},
                            {"symbol": "000858", "name": "五粮液", "invalidation": "趋势反转"}],
            }
        if tool_name == "search_research":
            return {"searches": [], "result_count": 3}
        raise AssertionError(f"unexpected tool: {tool_name}")

    def test_stock_workflow_persists_context_tools_events_and_result(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with self._patch_db(path), patch.object(
                agent_harness, "_execute_tool", side_effect=self._successful_stock_tool
            ):
                created = agent_harness.create_run(
                    "stock_analysis", {"stocks": ["600519", "000858"], "profile": "balanced"},
                    intent="比较两只白酒股", start=False,
                )
                completed = agent_harness.execute_run(created["run"]["run_key"])

            self.assertEqual(completed["run"]["status"], "COMPLETED")
            self.assertEqual(completed["run"]["display_title"], "股票研判：600519 / 000858")
            self.assertEqual(completed["run"]["current_step"], 3)
            self.assertEqual([item["status"] for item in completed["tool_calls"]],
                             ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"])
            self.assertEqual(completed["run"]["result"]["artifacts"]["comparison"]["verdict"]["winner"],
                             "600519")
            self.assertTrue(completed["run"]["context"]["boundaries"]["tool_allowlist_only"])
            event_types = [item["event_type"] for item in completed["events"]]
            self.assertIn("CONTEXT_CAPTURED", event_types)
            self.assertIn("PLAN_CREATED", event_types)
            self.assertIn("RUN_COMPLETED", event_types)

            with self._patch_db(path):
                continued = agent_harness.create_run(
                    "quality_audit", {"stock_limit": 1},
                    thread_key=completed["thread"]["thread_key"], start=False,
                )
            memory = continued["run"]["context"]["thread_memory"]
            self.assertEqual(memory["last_run_key"], completed["run"]["run_key"])
            self.assertEqual(memory["last_workflow"], "stock_analysis")
            self.assertEqual(memory["last_summary"], completed["run"]["result"]["summary"])

    def test_consequential_tool_waits_for_explicit_approval(self):
        calls = []

        def fake_tool(tool_name, arguments):
            calls.append((tool_name, dict(arguments)))
            if tool_name == "evaluate_candidate":
                return {"status": "SUCCESS", "total_cases": 2, "passed_cases": 2,
                        "failed_cases": 0, "regression_count": 0, "pass_rate": 1.0}
            if tool_name == "activate_candidate":
                self.assertEqual(arguments["_approved_by"], "测试批准人")
                return {"version_key": "evo-test", "status": "ACTIVE"}
            raise AssertionError(tool_name)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with self._patch_db(path), patch.object(agent_harness, "_execute_tool", side_effect=fake_tool):
                created = agent_harness.create_run(
                    "candidate_activation", {"candidate_id": 7}, start=False,
                )
                waiting = agent_harness.execute_run(created["run"]["run_key"])
                self.assertEqual(waiting["run"]["status"], "WAITING_APPROVAL")
                self.assertEqual([item[0] for item in calls], ["evaluate_candidate"])
                approval = waiting["waiting_approval"]
                agent_harness.resolve_approval(
                    approval["approval_key"], True, "测试批准人", True, start=False,
                )
                completed = agent_harness.execute_run(created["run"]["run_key"])

            self.assertEqual(completed["run"]["status"], "COMPLETED")
            self.assertEqual([item[0] for item in calls], ["evaluate_candidate", "activate_candidate"])
            self.assertEqual(completed["approvals"][0]["status"], "APPROVED")

    def test_cancelled_run_closes_pending_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with self._patch_db(path):
                created = agent_harness.create_run(
                    "version_rollback", {"version_key": "baseline-v1"}, start=False,
                )
                waiting = agent_harness.execute_run(created["run"]["run_key"])
                approval_key = waiting["waiting_approval"]["approval_key"]
                cancelled = agent_harness.cancel_run(
                    created["run"]["run_key"], "测试取消人",
                )
                with self.assertRaisesRegex(ValueError, "已经处理"):
                    agent_harness.resolve_approval(
                        approval_key, True, "迟到批准人", True, start=False,
                    )

            self.assertEqual(cancelled["run"]["status"], "CANCELLED")
            self.assertIsNone(cancelled["waiting_approval"])
            self.assertEqual(cancelled["approvals"][0]["status"], "CANCELLED")
            self.assertEqual(cancelled["approvals"][0]["resolved_by"], "测试取消人")

    def test_failed_tool_resumes_from_last_successful_checkpoint(self):
        counts = {"resolve_stocks": 0, "compare_stocks": 0, "search_research": 0}

        def flaky_tool(tool_name, arguments):
            counts[tool_name] += 1
            if tool_name == "compare_stocks" and counts[tool_name] <= 2:
                raise RuntimeError("temporary comparison failure")
            return self._successful_stock_tool(tool_name, arguments)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with self._patch_db(path), patch.object(agent_harness, "_execute_tool", side_effect=flaky_tool):
                created = agent_harness.create_run(
                    "stock_analysis", {"stocks": ["600519", "000858"], "profile": "balanced"},
                    start=False,
                )
                failed = agent_harness.execute_run(created["run"]["run_key"])
                self.assertEqual(failed["run"]["status"], "FAILED")
                self.assertEqual(failed["run"]["current_step"], 1)
                agent_harness.resume_run(created["run"]["run_key"], start=False)
                completed = agent_harness.execute_run(created["run"]["run_key"])

            self.assertEqual(completed["run"]["status"], "COMPLETED")
            self.assertEqual(counts["resolve_stocks"], 1)
            self.assertEqual(counts["compare_stocks"], 3)
            self.assertEqual(counts["search_research"], 1)

    def test_restart_marks_inflight_runs_as_resumable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with self._patch_db(path):
                created = agent_harness.create_run(
                    "quality_audit", {"stock_limit": 1}, start=False,
                )
                with closing(connect(path)) as conn:
                    conn.execute(
                        """UPDATE harness_runs SET created_at=?,updated_at=?
                           WHERE run_key=?""",
                        ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00",
                         created["run"]["run_key"]),
                    )
                    conn.commit()
                recovered = agent_harness.recover_interrupted_runs()
                state = agent_harness.get_run(created["run"]["run_key"])

            self.assertEqual(recovered, 1)
            self.assertEqual(state["run"]["status"], "INTERRUPTED")
            self.assertTrue(state["can_resume"])
            self.assertIn("RUN_INTERRUPTED", [item["event_type"] for item in state["events"]])

    def test_restart_preserves_fresh_inflight_run_from_other_service(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with self._patch_db(path):
                created = agent_harness.create_run(
                    "quality_audit", {"stock_limit": 1}, start=False,
                )
                recovered = agent_harness.recover_interrupted_runs()
                state = agent_harness.get_run(created["run"]["run_key"])
            self.assertEqual(recovered, 0)
            self.assertEqual(state["run"]["status"], "QUEUED")

    def test_runtime_payload_keeps_latest_quant_run_outside_recent_window(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            initialize(path)
            with self._patch_db(path):
                quant = agent_harness.create_run(
                    "quant_portfolio",
                    {"sectors": ["白酒"], "capital": 100000},
                    start=False,
                )
                for index in range(25):
                    agent_harness.create_run(
                        "quality_audit", {"stock_limit": index + 1}, start=False,
                    )
                payload = agent_harness.runtime_payload(limit=20)
            self.assertNotIn(
                quant["run"]["run_key"], [item["run_key"] for item in payload["runs"]]
            )
            self.assertEqual(
                payload["latest_quant_run"]["run_key"], quant["run"]["run_key"]
            )


if __name__ == "__main__":
    unittest.main()
