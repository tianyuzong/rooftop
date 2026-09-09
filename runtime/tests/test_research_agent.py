import json
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch, Mock

from app import db, research_agent as agent, codex_bridge, code_evolution
from app.service_monitor import ServiceMonitor


def plan():
    return {"title": "白酒研究", "understanding": "比较白酒", "supported": True,
            "questions": [], "assumptions": [], "stocks": ["600519", "000858"], "sectors": [],
            "capital": 100000, "horizon_months": 12, "target_return_pct": None,
            "max_drawdown_pct": 10, "stop_loss_pct": None, "take_profit_pct": None,
            "max_positions": None, "risk_profile": "balanced", "focus": ["估值"],
            "tools": ["portfolio", "research_search"]}


class ResearchAgentTests(unittest.TestCase):
    def test_closed_output_rejects_injection_fields_and_wrong_numeric_types(self):
        valid = plan()
        codex_bridge.validate_output(valid, agent.PLAN_SCHEMA)
        for bad in ({**valid, "shell_command": "execute"}, {**valid, "capital": True},
                    {**valid, "tools": ["place_order"]}, {**valid, "capital": float("nan")}):
            with self.assertRaises(ValueError):
                codex_bridge.validate_output(bad, agent.PLAN_SCHEMA)

    def test_missing_values_disclosed_and_risk_constraints_preserved(self):
        value = plan()
        value["max_drawdown_pct"] = 3
        request = agent.normalize_plan(value)
        self.assertEqual(request["capital"], 100000)
        self.assertEqual(request["stop_loss_pct"], 3)
        self.assertFalse(request["refresh_data"])
        self.assertFalse(request["order_execution"])
        self.assertTrue(any("目标收益" in x for x in value["assumptions"]))
        value = plan()
        value["stop_loss_pct"] = 20
        with self.assertRaises(ValueError):
            agent.normalize_plan(value)

    def test_durable_run_grounds_report_and_rejects_unknown_citation(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake=Path(tmp)
            with patch.object(db, "DATA_LAKE", lake), patch.object(db, "DB_PATH", lake / "db/test.db"):
                agent.initialize()
                for citation, expected in (("portfolio", "COMPLETED"), ("fabricated", "FAILED")):
                    key="research_"+citation
                    with closing(db.connect()) as conn:
                        conn.execute("INSERT INTO research_agent_runs(run_key,question,status,stage,created_at,updated_at) VALUES(?,'比较茅台和五粮液','QUEUED','QUEUED',?,?)", (key, agent.now(), agent.now()))
                        conn.commit()
                    report={"summary":"研究结论", "sections":[{"title":"分析","analysis":"基于计算", "evidence_ids":[citation]}], "risks":[],"next_steps":[]}
                    responses=[{"data":plan(),"usage":{}},{"data":report,"usage":{}}]
                    evidence={"stocks":[],"sources":[{"id":"portfolio"}],"request":{"capital":100000}}
                    with patch.object(codex_bridge,"structured_call", side_effect=responses), patch.object(agent,"collect_evidence",return_value=evidence):
                        agent.ResearchWorker().execute(key)
                    result=agent.get_run(key)
                    self.assertEqual(result["status"], expected)
                    self.assertEqual(result["evidence"], evidence)
                    self.assertGreaterEqual(len(result["events"]),4)

    def test_cancelled_queued_request_never_calls_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake=Path(tmp)
            with patch.object(db,"DATA_LAKE",lake), patch.object(db,"DB_PATH",lake/"db/test.db"):
                agent.initialize()
                with closing(db.connect()) as conn:
                    conn.execute("INSERT INTO research_agent_runs(run_key,question,status,stage,created_at,updated_at) VALUES('cancelled','测试取消','CANCELLED','CANCELLED',?,?)",(agent.now(),agent.now()))
                    conn.commit()
                with patch.object(codex_bridge,"structured_call") as call:
                    agent.ResearchWorker().execute("cancelled")
                    call.assert_not_called()

    def test_supervisor_restarts_only_dead_worker(self):
        worker=Mock();worker.is_alive.return_value=True;worker.stop_event=threading.Event()
        replacement=Mock();replacement.is_alive.return_value=False;replacement.stop_event=threading.Event()
        factory=Mock(return_value=replacement)
        monitor=ServiceMonitor();monitor.register("test","测试",worker,factory)
        monitor.check_once();factory.assert_not_called()
        worker.is_alive.return_value=False
        monitor.check_once();factory.assert_called_once();replacement.start.assert_called_once()
        self.assertEqual(monitor.entries["test"]["restarts"],1)

    def test_code_evolution_sandbox_has_complete_public_test_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            replacement=workspace/"replacement.py"
            replacement.write_text("def strategy_candidates():\n    return []\n",encoding="utf-8")
            manifest={"replacement_path":str(replacement),"replacement_hash":code_evolution._hash_bytes(replacement.read_bytes())}
            with patch.object(code_evolution,"_copy_database"):
                sandbox, _, _=code_evolution._prepare_sandbox({"workspace_path":str(workspace)},manifest)
            for relative in ("README.md",".env.example",".codex-plugin/plugin.json",".zcode-plugin/plugin.json","runtime/docs/CONFIGURATION.md"):
                self.assertTrue((sandbox/relative).is_file(),relative)


if __name__ == "__main__":
    unittest.main()
