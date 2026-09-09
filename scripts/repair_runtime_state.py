"""Repair dates, retry states and publication records with recoverable archives."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from app.fundamentals import CURRENT_QUOTE_SOURCES, VALUATION_TIME_POLICY, valuation_availability_date


def valid_date(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def observed_time(value):
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def pending_quant_publications(conn):
    pending = []
    for row in conn.execute(
        """SELECT r.* FROM quant_portfolio_runs r WHERE r.status='SUCCESS'
           AND r.id=(SELECT MAX(latest.id) FROM quant_portfolio_runs latest
                     WHERE latest.mandate_id=r.mandate_id)"""
    ):
        result = json.loads(row["result_json"])
        version = result.get("version", {})
        if (version.get("status") != "UNCHANGED"
                or result.get("fundamental_time_policy") != VALUATION_TIME_POLICY):
            continue
        active = conn.execute(
            "SELECT * FROM quant_portfolio_versions WHERE mandate_id=? AND status='ACTIVE' ORDER BY id DESC LIMIT 1",
            (row["mandate_id"],),
        ).fetchone()
        if not active or active["version_key"] != version.get("version_key"):
            continue
        old = json.loads(active["result_json"])
        gate = version.get("gate", {})
        if (old.get("fundamental_time_policy") != VALUATION_TIME_POLICY
                or gate.get("risk_pass") is False or gate.get("non_regression_vs_active") is False):
            pending.append({"run": dict(row), "active_version": dict(active)})
    return pending


def inspect_state(conn):
    invalid_ids = [row["id"] for row in conn.execute(
        "SELECT id,report_date,notice_date FROM fundamental_reports"
    ) if not valid_date(row["report_date"]) or not valid_date(row["notice_date"])]
    reports = []
    for row_id in invalid_ids:
        reports.append(dict(conn.execute("SELECT * FROM fundamental_reports WHERE id=?", (row_id,)).fetchone()))
    cycles = []
    for row in conn.execute("SELECT * FROM harness_learning_cycles WHERE status IN ('SUCCESS','SUCCESS_WITH_WARNINGS')"):
        metrics = json.loads(row["metrics_json"])
        errors = json.loads(row["errors_json"])
        if metrics.get("quant_portfolios", {}).get("errors") or any(
            error.get("stage") != "sentiment" for error in errors
        ):
            cycles.append(dict(row))
    valuations = []
    conflicts = {}
    for row in conn.execute("SELECT * FROM fundamental_valuations"):
        value = dict(row)
        if value["source_code"] not in CURRENT_QUOTE_SOURCES:
            continue
        available_date = valuation_availability_date(value)
        if available_date and available_date != value["asof_date"]:
            valuations.append({"previous_row": value, "corrected_asof_date": available_date})
            conflict = conn.execute(
                "SELECT * FROM fundamental_valuations WHERE symbol=? AND asof_date=? AND source_code=?",
                (value["symbol"], available_date, value["source_code"]),
            ).fetchone()
            if conflict:
                conflicts[int(conflict["id"])] = dict(conflict)
    return {"fundamental_reports": reports, "learning_cycles": cycles,
            "valuation_dates": valuations, "valuation_conflicts": list(conflicts.values()),
            "quant_publications": pending_quant_publications(conn)}


def repair_state(db_path, archive_path=None):
    uri = Path(db_path).resolve().as_uri() + ("?mode=rw" if archive_path else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE" if archive_path else "BEGIN")
        evidence = inspect_state(conn)
        result = {"invalid_reports": len(evidence["fundamental_reports"]),
                  "retryable_cycles": len(evidence["learning_cycles"]),
                  "valuation_dates": len(evidence["valuation_dates"]),
                  "quant_publications": len(evidence["quant_publications"]), "applied": False}
        if not archive_path or not any(evidence.values()):
            return result
        if (evidence["valuation_dates"] or evidence["quant_publications"]) and conn.execute(
            "SELECT 1 FROM harness_learning_cycles WHERE status='RUNNING' LIMIT 1"
        ).fetchone():
            raise RuntimeError("wait for or explicitly stop the learning worker before data/publication repair")
        archive = Path(archive_path)
        archive.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents a later maintenance run overwriting recovery evidence.
        with archive.open("x", encoding="utf-8") as output:
            json.dump({"database": str(Path(db_path).resolve()), "previous_rows": evidence},
                      output, ensure_ascii=False, indent=2)
        try:
            for item in evidence["quant_publications"]:
                from app.quant_portfolio import _version_quant_result
                run = item["run"]
                corrected = json.loads(run["result_json"])
                corrected.pop("version", None)
                corrected["publication_repair"] = {
                    "archive": str(archive), "evaluation_reused_from_run": run["run_key"],
                    "repaired_at": datetime.now(timezone.utc).isoformat(),
                }
                corrected["version"] = _version_quant_result(conn, run["mandate_id"], run["id"], corrected)
                conn.execute("UPDATE quant_portfolio_runs SET result_json=? WHERE id=?",
                             (json.dumps(corrected, ensure_ascii=False), run["id"]))
                cycle = conn.execute("SELECT metrics_json FROM harness_learning_cycles WHERE id=?",
                                     (run["learning_cycle_id"],)).fetchone()
                if cycle:
                    metrics = json.loads(cycle["metrics_json"])
                    for summary in metrics.get("quant_portfolios", {}).get("results", []):
                        if summary.get("run_key") == run["run_key"]:
                            summary["version"] = corrected["version"]
                            summary["publication_repair_archive"] = str(archive)
                    conn.execute("UPDATE harness_learning_cycles SET metrics_json=? WHERE id=?",
                                 (json.dumps(metrics, ensure_ascii=False), run["learning_cycle_id"]))
            # Remove moving keys first, then merge date collisions by observation time.
            # Every displaced row was included in the exclusive recovery archive above.
            for item in evidence["valuation_dates"]:
                conn.execute("DELETE FROM fundamental_valuations WHERE id=?", (item["previous_row"]["id"],))
            for item in evidence["valuation_dates"]:
                row = {**item["previous_row"], "asof_date": item["corrected_asof_date"]}
                conflict = conn.execute(
                    "SELECT * FROM fundamental_valuations WHERE symbol=? AND asof_date=? AND source_code=?",
                    (row["symbol"], row["asof_date"], row["source_code"]),
                ).fetchone()
                if conflict:
                    if observed_time(conflict["observed_at"]) >= observed_time(row["observed_at"]):
                        continue
                    conn.execute("DELETE FROM fundamental_valuations WHERE id=?", (conflict["id"],))
                columns = list(row)
                conn.execute(
                    f"INSERT INTO fundamental_valuations ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})", [row[key] for key in columns],
                )
            for row in evidence["fundamental_reports"]:
                conn.execute("DELETE FROM fundamental_reports WHERE id=?", (row["id"],))
            for row in evidence["learning_cycles"]:
                metrics = json.loads(conn.execute(
                    "SELECT metrics_json FROM harness_learning_cycles WHERE id=?", (row["id"],)
                ).fetchone()[0])
                metrics["retry_required"] = True
                metrics["state_repair_archive"] = str(archive)
                progress = json.loads(row["progress_json"] or "{}")
                progress.update({"status": "PARTIAL", "current_label": "Incomplete stages require retry"})
                conn.execute(
                    "UPDATE harness_learning_cycles SET status='PARTIAL',metrics_json=?,progress_json=? WHERE id=?",
                    (json.dumps(metrics, ensure_ascii=False), json.dumps(progress, ensure_ascii=False), row["id"]),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return {**result, "applied": True, "archive": str(archive)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-lake", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    lake = args.data_lake.resolve()
    database = lake / "db" / "market_intelligence.db"
    if not database.is_file():
        parser.error("existing data-lake database required")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = lake / "dead_letter" / ("runtime-state-repair-" + stamp + ".json") if args.apply else None
    print(json.dumps(repair_state(database, archive), ensure_ascii=True))


if __name__ == "__main__":
    main()
