"""Proactive, deterministic bad-case discovery for the Argus Harness."""

from __future__ import annotations

import json
import re
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .db import DATA_LAKE, ROOT, connect
from .harness import (active_config, evaluate_candidate, generate_candidate,
                      normalize_input, record_bad_case)


_cycle_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _known_stocks(limit: int) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT symbol,name,0 AS source_order FROM comparison_watchlist
               UNION ALL
               SELECT symbol,name,1 AS source_order FROM assets
               ORDER BY source_order,symbol"""
        ).fetchall()
    stocks, seen = [], set()
    for row in rows:
        symbol, name = str(row["symbol"]), str(row["name"] or "").strip()
        if not re.fullmatch(r"\d{6}", symbol) or symbol in seen or not name or name == symbol:
            continue
        if "\ufffd" in name:
            continue
        seen.add(symbol)
        stocks.append({"symbol": symbol, "name": name})
        if len(stocks) >= limit:
            break
    return stocks


def _probe(probe_key: str, category: str, passed: bool, title: str, *,
           case_type: str | None = None, input_data: dict | None = None,
           expected: dict | None = None, observed: dict | None = None,
           severity: str = "MEDIUM") -> dict:
    return {
        "probe_key": probe_key, "category": category, "passed": bool(passed), "title": title,
        "case_type": case_type, "input": input_data or {}, "expected": expected or {},
        "observed": observed or {}, "severity": severity,
    }


def _profile_contract_probes() -> list[dict]:
    from .stock_compare import PROFILE_DEFINITIONS

    probes = []
    for profile, definition in PROFILE_DEFINITIONS.items():
        weights = definition.get("weights", ())
        keys = [item[0] for item in weights]
        total = sum(float(item[2]) for item in weights)
        passed = len(keys) == 6 and len(set(keys)) == 6 and abs(total - 100) < 0.001
        probes.append(_probe(
            f"profile-contract:{profile}", "策略契约", passed,
            f"{definition.get('label', profile)}六维权重结构完整",
            case_type="skill_contract", input_data={"component": "profile_weights", "profile": profile},
            expected={"dimension_count": 6, "unique": True, "weight_total": 100},
            observed={"keys": keys, "weight_total": total}, severity="HIGH",
        ))
    return probes


def _skill_contract_probe() -> dict:
    skill_path = ROOT.parent / "skills" / "stock-comparison-visualizer" / "SKILL.md"
    required = ("## 代理 Harness", "POST /api/harness/runs", "WAITING_APPROVAL",
                "从检查点恢复", "不用于下单")
    try:
        content = skill_path.read_text(encoding="utf-8")
        missing = [item for item in required if item not in content]
    except OSError as exc:
        missing = list(required)
        content = ""
        error = str(exc)
    else:
        error = None
    return _probe(
        "skill-contract:instructions", "Skill一致性", not missing,
        "Skill 已说明持久运行、恢复、批准边界和非交易属性",
        case_type="skill_contract", input_data={"component": "SKILL.md"},
        expected={"required_sections": list(required)},
        observed={"missing": missing, "error": error, "size": len(content)}, severity="HIGH",
    )


def _stock_probes(stock: dict) -> list[dict]:
    from .search import exact_search
    from .stock_compare import resolve_stock

    symbol, name = stock["symbol"], stock["name"]
    probes = []
    for kind, token in (("代码", symbol), ("名称", name)):
        try:
            actual = resolve_stock(token)
            passed, observed = actual.get("symbol") == symbol, {"actual": actual}
        except Exception as exc:
            passed, observed = False, {"error": str(exc)}
        probes.append(_probe(
            f"resolve:{kind}:{symbol}", "股票识别", passed, f"{name}可通过{kind}稳定识别",
            case_type="stock_resolution", input_data={"token": token},
            expected={"symbol": symbol, "name": name, "oracle": "canonical_stock_master"},
            observed=observed, severity="HIGH",
        ))

    for kind, query in (("代码", symbol), ("名称", name)):
        try:
            results = exact_search(query, None)
            matched = any(symbol in (item.get("title", "") + item.get("body", "")) or
                          name in (item.get("title", "") + item.get("body", "")) for item in results)
            observed = {"result_count": len(results), "titles": [item.get("title") for item in results[:3]]}
        except Exception as exc:
            matched, observed = False, {"error": str(exc)}
        probes.append(_probe(
            f"search:{kind}:{symbol}", "搜索可达性", matched, f"{name}可通过{kind}检索",
            case_type="search_no_result", input_data={"query": query, "page": None, "mode": "exact"},
            expected={"min_results": 1, "symbol": symbol}, observed=observed,
        ))

    with closing(connect()) as conn:
        bars = conn.execute(
            "SELECT COUNT(DISTINCT trade_date) FROM market_daily_bars WHERE asset_symbol=?",
            (symbol,),
        ).fetchone()[0]
    probes.append(_probe(
        f"daily-coverage:{symbol}", "数据覆盖", bars >= 60, f"{name}日线满足最低回放窗口",
        case_type="data_gap", input_data={"symbol": symbol, "dataset": "daily_bars"},
        expected={"min_daily_bars": 60}, observed={"daily_bars": bars}, severity="HIGH",
    ))
    return probes


def _normalization_probe(stock: dict) -> dict:
    symbol = stock["symbol"]
    fullwidth = "".join(chr(ord(char) + 0xFEE0) for char in symbol)
    normalized = normalize_input(fullwidth, active_config())
    return _probe(
        "normalization:nfkc-fullwidth-code", "输入鲁棒性", normalized == symbol,
        "全角股票代码可确定性转换为半角代码",
        case_type="input_normalization", input_data={"token": fullwidth},
        expected={"symbol": symbol, "name": stock["name"], "normalization_rule": "nfkc",
                  "oracle": "deterministic_nfkc"},
        observed={"normalized": normalized}, severity="MEDIUM",
    )


def _record_failure(item: dict) -> dict | None:
    if item["passed"] or not item.get("case_type"):
        return None
    return record_bad_case(
        item["case_type"], item["input"], item["expected"], item["observed"],
        source="autonomous", severity=item["severity"], notes=f"主动巡检：{item['title']}",
    )


def _resolve_passing_probes(probes: list[dict]) -> int:
    passing = [item for item in probes if item["passed"] and item.get("case_type")]
    if not passing:
        return 0
    resolved = 0
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT id,case_type,input_json FROM harness_bad_cases
               WHERE source='autonomous' AND status IN ('OBSERVED','READY')"""
        ).fetchall()
        for row in rows:
            try:
                stored_input = json.loads(row["input_json"])
            except json.JSONDecodeError:
                continue
            if any(item["case_type"] == row["case_type"] and item["input"] == stored_input
                   for item in passing):
                conn.execute("UPDATE harness_bad_cases SET status='RESOLVED',last_seen_at=? WHERE id=?",
                             (_now(), row["id"]))
                resolved += 1
        conn.commit()
    return resolved


def _candidate_for_case(case: dict) -> dict | None:
    deterministic = (
        case["case_type"] == "input_normalization" and
        case["expected"].get("oracle") == "deterministic_nfkc"
    ) or (
        case["case_type"] == "stock_resolution" and
        case["expected"].get("oracle") == "canonical_stock_master"
    )
    if not deterministic or case["status"] != "READY":
        return None
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT * FROM harness_candidates WHERE bad_case_id=?
               AND status IN ('DRAFT','EVALUATED','ACTIVE') ORDER BY id DESC LIMIT 1""",
            (case["id"],),
        ).fetchone()
    if row:
        item = dict(row)
        item["config"] = json.loads(item.pop("config_json"))
        return item
    return generate_candidate(case["id"])


def _persist_summary(summary: dict) -> None:
    with closing(connect()) as conn:
        conn.execute(
            """INSERT INTO harness_events(event_type,entity_type,details_json,created_at)
               VALUES('AUTONOMOUS_RUN','harness',?,?)""",
            (json.dumps(summary, ensure_ascii=False, sort_keys=True), _now()),
        )
        conn.commit()
    output = DATA_LAKE / "research" / "harness-autonomous-latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def run_autonomous_cycle(auto_apply: bool = False, stock_limit: int = 20) -> dict:
    if not _cycle_lock.acquire(blocking=False):
        raise RuntimeError("自主巡检已经在运行")
    started = _now()
    try:
        stocks = _known_stocks(max(1, min(int(stock_limit), 100)))
        probes = _profile_contract_probes() + [_skill_contract_probe()]
        for stock in stocks:
            probes.extend(_stock_probes(stock))
        if stocks:
            probes.append(_normalization_probe(stocks[0]))

        reconciled = _resolve_passing_probes(probes)
        cases = [case for case in (_record_failure(item) for item in probes) if case]
        candidates, evaluations, activated = [], [], []
        for case in cases:
            candidate = _candidate_for_case(case)
            if not candidate:
                continue
            candidates.append(candidate)
            if candidate["status"] != "ACTIVE":
                evaluation = evaluate_candidate(candidate["id"])
                evaluations.append(evaluation)

        failures = [item for item in probes if not item["passed"]]
        summary = {
            "status": "SUCCESS", "started_at": started, "finished_at": _now(),
            "stock_count": len(stocks), "probe_count": len(probes),
            "passed_count": len(probes) - len(failures), "failed_count": len(failures),
            "bad_case_count": len(cases), "candidate_count": len(candidates),
            "evaluated_count": len(evaluations), "activated_count": len(activated),
            "reconciled_count": reconciled,
            "auto_apply": False,
            "summary": (f"自主巡检完成：检查 {len(stocks)} 只股票、{len(probes)} 项契约，"
                        f"预测发现 {len(failures)} 个坏案例，生成 {len(candidates)} 个候选，"
                        f"安全修正 {len(activated)} 项，确认关闭 {reconciled} 个旧案例。"),
            "findings": [{"probe_key": item["probe_key"], "category": item["category"],
                          "title": item["title"], "severity": item["severity"],
                          "observed": item["observed"]} for item in failures[:50]],
            "activated_versions": [item["version_key"] for item in activated],
            "guardrails": {"automatic_activation": False, "human_approval_required": True,
                           "ranking_auto_fix": False, "code_auto_change": False,
                           "order_execution": False},
        }
        _persist_summary(summary)
        return summary
    finally:
        _cycle_lock.release()
