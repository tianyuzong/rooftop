import importlib.util
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.db import connect, initialize

script = Path(__file__).resolve().parents[2] / "scripts" / "repair_runtime_state.py"
spec = importlib.util.spec_from_file_location("repair_runtime_state", script)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class RuntimeRepairTests(unittest.TestCase):
    def test_publication_repair_reuses_evaluation_preserves_archive_and_waits_for_worker(self):
        from app.fundamentals import VALUATION_TIME_POLICY
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.db"
            archive = Path(folder) / "publication-repair.json"
            initialize(path)
            result = {
                "request": {"max_drawdown_pct": 15}, "data": {"end": "2026-09-04"},
                "candidates": [{"symbol": "600519"}],
                "recommendation": {"expectation": {"p50": .2}, "model_version": "model-v1",
                                   "holdout_metrics": {"risk_pass": True, "max_drawdown": .1},
                                   "non_regression_pass": True},
            }
            with closing(connect(path)) as conn:
                mandate = conn.execute(
                    "INSERT INTO quant_mandates(mandate_key,name,status,input_json,created_at,updated_at) "
                    "VALUES('mandate','mandate','ACTIVE','{}','now','now')"
                ).lastrowid
                cycle = conn.execute(
                    "INSERT INTO harness_learning_cycles(cycle_key,cycle_date,phase,status,trigger_kind,"
                    "universe_json,metrics_json,started_at) VALUES('cycle','2026-09-04','POST_CLOSE','RUNNING',"
                    "'test','[]',?,'now')", (json.dumps({"quant_portfolios": {
                        "errors": [{"mandate_key": "other", "error": "fixture"}], "results": [
                        {"run_key": "run", "version": {"version_key": "old"}}]}}),),
                ).lastrowid
                run = conn.execute(
                    "INSERT INTO quant_portfolio_runs(run_key,mandate_id,learning_cycle_id,trigger_kind,status,started_at) "
                    "VALUES('run',?,?,'test','SUCCESS','now')", (mandate, cycle),
                ).lastrowid
                conn.execute(
                    "INSERT INTO quant_portfolio_versions(version_key,mandate_id,run_id,status,score,gate_json,result_json,created_at) "
                    "VALUES('old',?,?,'ACTIVE',.15,'{}',?,'now')", (mandate, run, json.dumps(result)),
                )
                result["fundamental_time_policy"] = VALUATION_TIME_POLICY
                result["version"] = {"version_key": "old", "status": "UNCHANGED"}
                conn.execute("UPDATE quant_portfolio_runs SET result_json=? WHERE id=?", (json.dumps(result), run))
                conn.commit()
            self.assertEqual(repair.repair_state(path)["quant_publications"], 1)
            with self.assertRaisesRegex(RuntimeError, "learning worker"):
                repair.repair_state(path, archive)
            self.assertFalse(archive.exists())
            with closing(connect(path)) as conn:
                conn.execute("UPDATE harness_learning_cycles SET status='SUCCESS' WHERE id=?", (cycle,))
                conn.commit()
            self.assertTrue(repair.repair_state(path, archive)["applied"])
            with closing(connect(path)) as conn:
                saved = json.loads(conn.execute("SELECT result_json FROM quant_portfolio_runs WHERE id=?", (run,)).fetchone()[0])
                self.assertEqual(saved["version"]["status"], "ACTIVE")
                self.assertNotEqual(saved["version"]["version_key"], "old")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM quant_portfolio_runs").fetchone()[0], 1)
                metrics = json.loads(conn.execute("SELECT metrics_json FROM harness_learning_cycles WHERE id=?", (cycle,)).fetchone()[0])
                self.assertEqual(metrics["quant_portfolios"]["results"][0]["version"], saved["version"])
            self.assertFalse(repair.repair_state(path, archive)["applied"])

    def test_valuation_repair_preserves_collision_evidence_and_uses_shanghai_date(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.db"
            archive = Path(folder) / "valuation-repair.json"
            initialize(path)
            with closing(connect(path)) as conn:
                for asof, observed, pe in (("2026-08-28", "2026-08-30T17:00:00+00:00", 20),
                                           ("2026-08-31", "2026-08-31T01:00:00+00:00", 21)):
                    conn.execute(
                        "INSERT INTO fundamental_valuations(symbol,asof_date,source_code,observed_at,pe_ttm) "
                        "VALUES('600519',?,'eastmoney_quote_profile',?,?)", (asof, observed, pe),
                    )
                conn.commit()
            self.assertEqual(repair.repair_state(path)["valuation_dates"], 1)
            result = repair.repair_state(path, archive)
            self.assertTrue(result["applied"])
            previous = json.loads(archive.read_text(encoding="utf-8"))["previous_rows"]
            self.assertEqual(len(previous["valuation_dates"]), 1)
            self.assertEqual(len(previous["valuation_conflicts"]), 1)
            with closing(connect(path)) as conn:
                rows = conn.execute("SELECT asof_date,pe_ttm FROM fundamental_valuations").fetchall()
                self.assertEqual([tuple(row) for row in rows], [("2026-08-31", 21)])
            self.assertFalse(repair.repair_state(path, archive)["applied"])

    def test_repair_preserves_evidence_is_scoped_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.db"
            archive = Path(folder) / "quarantine.json"
            initialize(path)
            with closing(connect(path)) as conn:
                for symbol, notice in (("600519", "nan"), ("000858", "2026-09-04")):
                    conn.execute(
                        "INSERT INTO fundamental_reports(symbol,report_date,notice_date,source_code,observed_at) "
                        "VALUES(?,'2026-06-30',?,'fixture','2026-09-04')", (symbol, notice),
                    )
                for key, metrics in (("failed", {"quant_portfolios": {"errors": [{"mandate_key": "a"}]}}),
                                     ("done", {"quant_portfolios": {"errors": []}})):
                    conn.execute(
                        "INSERT INTO harness_learning_cycles(cycle_key,cycle_date,phase,status,trigger_kind,"
                        "universe_json,metrics_json,started_at) VALUES(?,'2026-09-04','POST_CLOSE','SUCCESS',"
                        "'fixture','[]',?,'2026-09-04')", (key, json.dumps(metrics)),
                    )
                conn.commit()
            preview = repair.repair_state(path)
            self.assertFalse(preview["applied"])
            self.assertFalse(archive.exists())
            self.assertEqual(preview["invalid_reports"], 1)
            result = repair.repair_state(path, archive)
            self.assertTrue(result["applied"])
            evidence = json.loads(archive.read_text(encoding="utf-8"))
            self.assertEqual(evidence["previous_rows"]["fundamental_reports"][0]["notice_date"], "nan")
            with closing(connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT symbol FROM fundamental_reports").fetchone()[0], "000858")
                states = dict(conn.execute("SELECT cycle_key,status FROM harness_learning_cycles"))
                self.assertEqual(states, {"failed": "PARTIAL", "done": "SUCCESS"})
            self.assertFalse(repair.repair_state(path, archive)["applied"])


if __name__ == "__main__":
    unittest.main()
