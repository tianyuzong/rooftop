"""Auditable bad-case and gated source-learning loop for local research.

General configuration still requires approval.  A separate allowlisted source
pipeline may promote pure strategy recipes only after isolated tests and
chronological holdout gates.  Neither path can create or transmit orders.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from .db import connect, initialize


BASE_CONFIG = {
    "stock_aliases": {}, "search_aliases": {}, "profile_weights": {},
    "normalization_rules": {"nfkc": False},
}
CASE_TYPES = {
    "search_no_result", "stock_resolution", "ranking_mismatch",
    "data_gap", "interaction_failure", "comparison_error", "semantic_degraded",
    "input_normalization", "skill_contract",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _load(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _merge(base: dict, overlay: dict) -> dict:
    merged = json.loads(json.dumps(base, ensure_ascii=False))
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_input(value: str, config: dict | None = None) -> str:
    """Apply only explicitly activated, deterministic input normalizations."""
    text = str(value or "").strip()
    selected = config if config is not None else active_config()
    if selected.get("normalization_rules", {}).get("nfkc") is True:
        text = unicodedata.normalize("NFKC", text)
    return text


def ensure_baseline() -> None:
    initialize()
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM harness_versions WHERE status='ACTIVE'").fetchone():
            now = _now()
            conn.execute(
                """INSERT INTO harness_versions
                   (version_key,status,config_json,reason,created_at,activated_at)
                   VALUES('baseline-v1','ACTIVE',?,'初始受控配置',?,?)""",
                (_json(BASE_CONFIG), now, now),
            )
            conn.commit()


def active_version() -> dict:
    ensure_baseline()
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM harness_versions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    result = dict(row)
    result["config"] = _merge(BASE_CONFIG, _load(result.pop("config_json"), BASE_CONFIG))
    return result


def active_config() -> dict:
    return active_version()["config"]


def record_bad_case(case_type: str, input_data: Any, expected: Any = None,
                    observed: Any = None, source: str = "manual", page: str | None = None,
                    severity: str = "MEDIUM", notes: str = "") -> dict:
    case_type = str(case_type).strip().lower()
    if case_type not in CASE_TYPES:
        raise ValueError("不支持的坏案例类型")
    if not isinstance(input_data, dict):
        input_data = {"query": str(input_data)}
    expected = expected if isinstance(expected, dict) else {}
    observed = observed if isinstance(observed, dict) else {"value": observed} if observed is not None else {}
    severity = str(severity).strip().upper()
    if severity not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
        raise ValueError("严重程度必须是 LOW、MEDIUM、HIGH 或 CRITICAL")
    fingerprint = hashlib.sha256(
        (case_type + "\n" + _json(input_data) + "\n" + _json(expected)).encode("utf-8")
    ).hexdigest()
    now = _now()
    status = "READY" if expected else "OBSERVED"
    with closing(connect()) as conn:
        conn.execute(
            """INSERT INTO harness_bad_cases
               (fingerprint,case_type,source,page,input_json,expected_json,observed_json,
                severity,status,notes,first_seen_at,last_seen_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(fingerprint) DO UPDATE SET
                 observed_json=excluded.observed_json,severity=excluded.severity,
                 status=CASE WHEN harness_bad_cases.status IN ('RESOLVED','IGNORED')
                             THEN harness_bad_cases.status ELSE excluded.status END,
                 notes=CASE WHEN excluded.notes<>'' THEN excluded.notes ELSE harness_bad_cases.notes END,
                 occurrences=harness_bad_cases.occurrences+1,last_seen_at=excluded.last_seen_at""",
            (fingerprint, case_type, source, page, _json(input_data), _json(expected),
             _json(observed), severity, status, notes, now, now),
        )
        row = conn.execute("SELECT * FROM harness_bad_cases WHERE fingerprint=?", (fingerprint,)).fetchone()
        conn.commit()
    return _case_dict(row)


def _case_dict(row) -> dict:
    item = dict(row)
    item["input"] = _load(item.pop("input_json"), {})
    item["expected"] = _load(item.pop("expected_json"), {})
    item["observed"] = _load(item.pop("observed_json"), {})
    return item


def _candidate_dict(row) -> dict:
    item = dict(row)
    item["config"] = _load(item.pop("config_json"), {})
    return item


def generate_candidate(bad_case_id: int) -> dict:
    ensure_baseline()
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM harness_bad_cases WHERE id=?", (int(bad_case_id),)).fetchone()
        if not row:
            raise ValueError("坏案例不存在")
        case = _case_dict(row)
        expected, inputs = case["expected"], case["input"]
        candidate_type, config, activatable = "manual_patch", {}, 0
        if case["case_type"] == "stock_resolution" and expected.get("symbol"):
            token = str(inputs.get("token") or inputs.get("query") or "").strip()
            if token:
                candidate_type = "stock_alias"
                config = {"stock_aliases": {token: {"symbol": str(expected["symbol"]),
                                                        "name": str(expected.get("name") or token)}}}
                activatable = 1
        elif case["case_type"] == "search_no_result":
            query = str(inputs.get("query") or "").strip()
            canonical = str(expected.get("canonical_query") or expected.get("symbol") or "").strip()
            if query and canonical:
                candidate_type = "search_alias"
                config = {"search_aliases": {query: canonical}}
                activatable = 1
        elif case["case_type"] == "ranking_mismatch" and isinstance(expected.get("profile_weights"), dict):
            profile = str(inputs.get("profile") or "balanced")
            supplied = expected["profile_weights"]
            weights = supplied.get(profile) if isinstance(supplied.get(profile), dict) else supplied
            from .stock_compare import PROFILE_DEFINITIONS
            valid_keys = {key for key, _label, _weight in PROFILE_DEFINITIONS.get(profile, {}).get("weights", ())}
            if valid_keys and set(weights) == valid_keys and abs(sum(float(value) for value in weights.values()) - 100) < 0.001:
                candidate_type = "profile_weights"
                config = {"profile_weights": {profile: weights}}
                activatable = 1
        elif (case["case_type"] == "input_normalization" and
              expected.get("normalization_rule") == "nfkc" and
              expected.get("oracle") == "deterministic_nfkc"):
            candidate_type = "normalization_rule"
            config = {"normalization_rules": {"nfkc": True}}
            activatable = 1
        title = {
            "stock_alias": "补充股票名称或代码别名",
            "search_alias": "补充搜索归一化别名",
            "profile_weights": "候选评分权重调整",
            "normalization_rule": "启用全角字符确定性归一化",
            "manual_patch": "需要工程代理处理的代码候选",
        }[candidate_type]
        rationale = f"由坏案例 #{case['id']} 自动归因生成；必须通过回归评测和人工批准。"
        baseline = active_version()["version_key"]
        cursor = conn.execute(
            """INSERT INTO harness_candidates
               (bad_case_id,candidate_type,title,rationale,config_json,baseline_version,
                status,activatable,created_at) VALUES(?,?,?,?,?,?,'DRAFT',?,?)""",
            (case["id"], candidate_type, title, rationale, _json(config), baseline, activatable, _now()),
        )
        conn.commit()
        created = conn.execute("SELECT * FROM harness_candidates WHERE id=?", (cursor.lastrowid,)).fetchone()
    return _candidate_dict(created)


def _evaluate_case(case: dict, config: dict) -> tuple[str, dict]:
    expected, inputs = case["expected"], case["input"]
    if not expected:
        return "SKIPPED", {"reason": "尚未填写期望结果"}
    if case["case_type"] in {"search_no_result", "semantic_degraded"}:
        from .search import exact_search
        query = str(inputs.get("query") or "").strip()
        query = str(config.get("search_aliases", {}).get(query, query))
        page = inputs.get("page") or case.get("page")
        if query.isdigit() and len(query) == 6:
            page = None
        results = exact_search(query, page)
        titles = [item["title"] for item in results]
        minimum = int(expected.get("min_results", 1))
        required = str(expected.get("title_contains") or expected.get("symbol") or "")
        passed = len(results) >= minimum and (not required or any(required in title for title in titles))
        return ("PASSED" if passed else "FAILED"), {"query": query, "count": len(results), "titles": titles[:5]}
    if case["case_type"] in {"stock_resolution", "input_normalization"}:
        token = str(inputs.get("token") or inputs.get("query") or "").strip()
        token = normalize_input(token, config)
        alias = config.get("stock_aliases", {}).get(token)
        if alias:
            actual = alias
        else:
            from .stock_compare import resolve_stock
            actual = resolve_stock(token)
        passed = str(actual.get("symbol")) == str(expected.get("symbol"))
        return ("PASSED" if passed else "FAILED"), {"actual": actual}
    if case["case_type"] == "ranking_mismatch":
        from .stock_compare import compare_stocks
        stocks = inputs.get("stocks") or []
        profile = str(inputs.get("profile") or "balanced")
        result = compare_stocks(stocks, profile, refresh=False)
        ranking = result["ranking"]
        weights = config.get("profile_weights", {}).get(profile)
        if isinstance(weights, dict) and weights:
            ranking = sorted(ranking, key=lambda item: sum(
                next((d["score"] for d in item["dimensions"] if d["key"] == key), 50) * float(weight) / 100
                for key, weight in weights.items()
            ), reverse=True)
        winner = ranking[0]["symbol"]
        passed = winner == str(expected.get("winner"))
        return ("PASSED" if passed else "FAILED"), {"winner": winner}
    return "SKIPPED", {"reason": "该类型需要工程代理或人工复核"}


def evaluate_candidate(candidate_id: int | None = None) -> dict:
    ensure_baseline()
    started = _now()
    with closing(connect()) as conn:
        candidate = None
        if candidate_id is not None:
            row = conn.execute("SELECT * FROM harness_candidates WHERE id=?", (int(candidate_id),)).fetchone()
            if not row:
                raise ValueError("候选改进不存在")
            candidate = _candidate_dict(row)
        baseline_config = active_config()
        config = baseline_config
        if candidate:
            config = _merge(config, candidate["config"])
        cases = [_case_dict(row) for row in conn.execute(
            "SELECT * FROM harness_bad_cases WHERE status NOT IN ('IGNORED') ORDER BY id"
        )]
        cursor = conn.execute(
            """INSERT INTO harness_evaluations(candidate_id,trigger_kind,status,started_at)
               VALUES(?,?,'RUNNING',?)""",
            (candidate_id, "CANDIDATE" if candidate_id else "BASELINE", started),
        )
        evaluation_id = cursor.lastrowid
        conn.commit()
    details, passed, failed, skipped, regressions, improvements = [], 0, 0, 0, 0, 0
    try:
        for case in cases:
            try:
                outcome, observed = _evaluate_case(case, config)
            except Exception as exc:
                outcome, observed = "FAILED", {"error": str(exc)}
            baseline_outcome = None
            if candidate:
                try:
                    baseline_outcome, _ = _evaluate_case(case, baseline_config)
                except Exception:
                    baseline_outcome = "FAILED"
            improved = bool(candidate and outcome == "PASSED" and baseline_outcome != "PASSED")
            regressed = bool(candidate and baseline_outcome == "PASSED" and outcome != "PASSED")
            improvements += improved
            regressions += regressed
            details.append({"bad_case_id": case["id"], "case_type": case["case_type"],
                            "outcome": outcome, "baseline_outcome": baseline_outcome,
                            "improved": improved, "regressed": regressed, "observed": observed})
            passed += outcome == "PASSED"
            failed += outcome == "FAILED"
            skipped += outcome == "SKIPPED"
        total = passed + failed
        rate = passed / total if total else 0.0
        if candidate is None:
            status = "SUCCESS" if failed == 0 and total > 0 else "FAILED"
        else:
            target_passed = any(
                item["bad_case_id"] == candidate.get("bad_case_id") and item["outcome"] == "PASSED"
                for item in details
            )
            status = "SUCCESS" if target_passed and regressions == 0 and improvements > 0 else "FAILED"
        with closing(connect()) as conn:
            conn.execute(
                """UPDATE harness_evaluations SET status=?,finished_at=?,total_cases=?,passed_cases=?,
                   failed_cases=?,skipped_cases=?,regression_count=?,pass_rate=?,details_json=? WHERE id=?""",
                (status, _now(), total, passed, failed, skipped, regressions, rate, _json(details), evaluation_id),
            )
            if candidate_id is not None:
                conn.execute("UPDATE harness_candidates SET status='EVALUATED',evaluated_at=? WHERE id=?",
                             (_now(), int(candidate_id)))
            conn.commit()
    except Exception as exc:
        with closing(connect()) as conn:
            conn.execute("UPDATE harness_evaluations SET status='ERROR',finished_at=?,error=? WHERE id=?",
                         (_now(), str(exc), evaluation_id))
            conn.commit()
        raise
    return get_evaluation(evaluation_id)


def get_evaluation(evaluation_id: int) -> dict:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM harness_evaluations WHERE id=?", (int(evaluation_id),)).fetchone()
    item = dict(row)
    item["details"] = _load(item.pop("details_json"), [])
    return item


def approve_candidate(candidate_id: int, approved_by: str, confirmed: bool) -> dict:
    if not confirmed or not str(approved_by).strip():
        raise ValueError("候选晋级必须由人工明确确认并填写批准人")
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM harness_candidates WHERE id=?", (int(candidate_id),)).fetchone()
        if not row:
            raise ValueError("候选改进不存在")
        candidate = _candidate_dict(row)
        if not candidate["activatable"]:
            raise ValueError("该候选涉及代码修改，只能交给工程代理处理，不能自动晋级")
        evaluation = conn.execute(
            "SELECT * FROM harness_evaluations WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
            (int(candidate_id),),
        ).fetchone()
        if not evaluation or evaluation["status"] != "SUCCESS" or evaluation["regression_count"]:
            raise ValueError("候选尚未通过无退化回归评测")
        details = _load(evaluation["details_json"], [])
        if not any(item.get("improved") for item in details):
            raise ValueError("候选没有相对当前版本修复任何坏案例")
        current = conn.execute("SELECT * FROM harness_versions WHERE status='ACTIVE'").fetchone()
        merged = _merge(_load(current["config_json"], BASE_CONFIG), candidate["config"])
        now = _now()
        version_key = f"evo-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{candidate_id}"
        conn.execute("UPDATE harness_versions SET status='ARCHIVED' WHERE status='ACTIVE'")
        conn.execute(
            """INSERT INTO harness_versions
               (version_key,parent_version,status,config_json,reason,candidate_id,created_at,activated_at)
               VALUES(?,?,'ACTIVE',?,?,?,?,?)""",
            (version_key, current["version_key"], _json(merged), candidate["rationale"], candidate_id, now, now),
        )
        conn.execute(
            "UPDATE harness_candidates SET status='ACTIVE',approved_at=?,approved_by=? WHERE id=?",
            (now, str(approved_by).strip(), int(candidate_id)),
        )
        if candidate.get("bad_case_id"):
            conn.execute("UPDATE harness_bad_cases SET status='RESOLVED' WHERE id=?", (candidate["bad_case_id"],))
        conn.execute(
            "INSERT INTO harness_events(event_type,entity_type,entity_id,details_json,created_at) VALUES('APPROVE','candidate',?,?,?)",
            (int(candidate_id), _json({"version": version_key, "approved_by": approved_by}), now),
        )
        conn.commit()
    return active_version()


def activate_autonomous_candidate(candidate_id: int) -> dict:
    """Activate only deterministic identity/normalization fixes from local canonical truth."""
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT c.candidate_type,b.source,b.expected_json FROM harness_candidates c
               JOIN harness_bad_cases b ON b.id=c.bad_case_id WHERE c.id=?""",
            (int(candidate_id),),
        ).fetchone()
    if not row or row["source"] != "autonomous":
        raise ValueError("该候选不属于允许自动修正的确定性规则")
    expected = _load(row["expected_json"], {})
    allowed = {
        "normalization_rule": "deterministic_nfkc",
        "stock_alias": "canonical_stock_master",
    }
    if allowed.get(row["candidate_type"]) != expected.get("oracle"):
        raise ValueError("自动修正缺少确定性真值来源")
    version = approve_candidate(candidate_id, "Harness自主巡检", True)
    with closing(connect()) as conn:
        conn.execute(
            """INSERT INTO harness_events(event_type,entity_type,entity_id,details_json,created_at)
               VALUES('AUTONOMOUS_ACTIVATE','candidate',?,?,?)""",
            (int(candidate_id), _json({"version": version["version_key"],
                                       "scope": row["candidate_type"]}), _now()),
        )
        conn.commit()
    return version


def rollback_version(version_key: str, approved_by: str, confirmed: bool) -> dict:
    if not confirmed or not str(approved_by).strip():
        raise ValueError("回滚必须由人工明确确认并填写操作人")
    with closing(connect()) as conn:
        target = conn.execute("SELECT * FROM harness_versions WHERE version_key=?", (version_key,)).fetchone()
        if not target:
            raise ValueError("目标版本不存在")
        conn.execute("UPDATE harness_versions SET status='ARCHIVED' WHERE status='ACTIVE'")
        conn.execute("UPDATE harness_versions SET status='ACTIVE',activated_at=? WHERE id=?", (_now(), target["id"]))
        conn.execute(
            "INSERT INTO harness_events(event_type,entity_type,entity_id,details_json,created_at) VALUES('ROLLBACK','version',?,?,?)",
            (target["id"], _json({"version": version_key, "approved_by": approved_by}), _now()),
        )
        conn.commit()
    return active_version()


def harness_payload() -> dict:
    current = active_version()
    with closing(connect()) as conn:
        cases = [_case_dict(row) for row in conn.execute(
            "SELECT * FROM harness_bad_cases ORDER BY last_seen_at DESC LIMIT 100"
        )]
        candidates = [_candidate_dict(row) for row in conn.execute(
            "SELECT * FROM harness_candidates ORDER BY id DESC LIMIT 50"
        )]
        evaluations = []
        for row in conn.execute("SELECT * FROM harness_evaluations ORDER BY id DESC LIMIT 20"):
            item = dict(row)
            item["details"] = _load(item.pop("details_json"), [])
            evaluations.append(item)
        versions = []
        for row in conn.execute("SELECT * FROM harness_versions ORDER BY id DESC LIMIT 20"):
            item = dict(row)
            item["config"] = _load(item.pop("config_json"), {})
            versions.append(item)
        autonomous_row = conn.execute(
            "SELECT details_json,created_at FROM harness_events WHERE event_type='AUTONOMOUS_RUN' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        autonomous_run = _load(autonomous_row["details_json"], {}) if autonomous_row else None
        if autonomous_run is not None:
            autonomous_run["created_at"] = autonomous_row["created_at"]
    latest = evaluations[0] if evaluations else None
    return {
        "active_version": current,
        "summary": {
            "bad_cases": len(cases),
            "open_cases": sum(item["status"] in {"OBSERVED", "READY"} for item in cases),
            "candidates": len(candidates),
            "active_candidates": sum(item["status"] == "ACTIVE" for item in candidates),
            "latest_pass_rate": latest["pass_rate"] if latest else None,
        },
        "bad_cases": cases, "candidates": candidates,
        "evaluations": evaluations, "versions": versions, "autonomous_run": autonomous_run,
        "guardrails": {
            "automatic_code_changes": True,
            "automatic_activation": False,
            "automatic_code_scope": "pure intraday strategy recipes only",
            "code_isolation_and_full_tests": True,
            "code_rollback": True,
            "deterministic_normalization_auto_fix": True,
            "human_approval_required": True,
            "order_execution": False,
        },
    }
