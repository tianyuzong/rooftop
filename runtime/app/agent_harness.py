"""Persistent, auditable execution harness for bounded stock-research agents.

The model/planner is replaceable. The harness owns durable state, context,
tool boundaries, progress events, approvals, checkpoints, and recovery.
It never exposes a shell or an order-execution tool.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from .db import connect, initialize


WORKFLOWS = {
    "stock_analysis": {
        "label": "股票研判",
        "description": "解析标的、运行对比、检索本地研究证据并形成可审计结论。",
    },
    "quality_audit": {
        "label": "质量巡检",
        "description": "运行主动探针和全量回归，生成改进候选但不自动生效。",
    },
    "quant_portfolio": {
        "label": "量化选股与组合",
        "description": "根据本金、期限、止盈止损、回撤和板块自动建立候选池、选股、回测并输出组合与预期收益区间。",
    },
    "strategy_evolution": {
        "label": "策略自进化",
        "description": "冻结投资约束，持续迭代三档组合策略；全部门禁通过后自动激活，否则在新交易日继续优化。",
    },
    "continuous_learning": {
        "label": "持续自进化",
        "description": "盘前固化预测；盘后刷新行情与多源舆情，重训在线模型和 Qwen3 数值时序适配器，进化 5 分钟策略，并对源码候选执行隔离测试、门禁发布与回滚。",
    },
    "strategy_activation": {
        "label": "策略激活",
        "description": "复核三档策略的样本外风险门禁，并自动激活全部门禁通过的版本。",
    },
    "candidate_activation": {
        "label": "候选晋级",
        "description": "先复评候选，再等待人工批准后切换配置版本。",
    },
    "version_rollback": {
        "label": "版本回滚",
        "description": "等待人工批准后回滚到指定配置版本。",
    },
}

TOOL_SPECS = {
    "resolve_stocks": {
        "description": "把名称或代码解析为唯一 A 股标的。",
        "risk_level": "READ",
        "approval_required": False,
        "max_attempts": 2,
    },
    "compare_stocks": {
        "description": "运行本地评分、历史回放和风险对比；只写内部缓存与关注记录。",
        "risk_level": "INTERNAL_WRITE",
        "approval_required": False,
        "max_attempts": 2,
    },
    "search_research": {
        "description": "只读检索本地股票档案和研报索引。",
        "risk_level": "READ",
        "approval_required": False,
        "max_attempts": 2,
    },
    "autonomous_audit": {
        "description": "运行确定性探针并记录坏案例；禁止自动激活候选。",
        "risk_level": "INTERNAL_WRITE",
        "approval_required": False,
        "max_attempts": 1,
    },
    "baseline_regression": {
        "description": "对当前配置运行坏案例回归集。",
        "risk_level": "INTERNAL_WRITE",
        "approval_required": False,
        "max_attempts": 1,
    },
    "list_activation_candidates": {
        "description": "列出已评测、可晋级且零退化的配置候选。",
        "risk_level": "READ",
        "approval_required": False,
        "max_attempts": 1,
    },
    "construct_quant_portfolio": {
        "description": "同步 A 股板块和行情，自动筛选候选股，运行逐日模型与组合样本外回测并版本化结果。",
        "risk_level": "INTERNAL_WRITE",
        "approval_required": False,
        "max_attempts": 1,
    },
    "validate_investment_mandate": {
        "description": "校验并冻结本金、期限、目标收益、最大回撤、股票池和成交假设。",
        "risk_level": "READ",
        "approval_required": False,
        "max_attempts": 1,
    },
    "evolve_portfolio_strategies": {
        "description": "在参数白名单内迭代三档组合策略并运行滚动样本外回测。",
        "risk_level": "INTERNAL_WRITE",
        "approval_required": False,
        "max_attempts": 1,
    },
    "continuous_learning_cycle": {
        "description": "刷新 A 股日线/分钟行情和多源舆情，结算预测，滚动回测在线/Qwen3/分钟策略，并运行隔离式源码进化。",
        "risk_level": "INTERNAL_WRITE",
        "approval_required": False,
        "max_attempts": 1,
    },
    "review_strategy_experiment": {
        "description": "读取实验、数据窗口和三档风险门禁结论。",
        "risk_level": "READ",
        "approval_required": False,
        "max_attempts": 1,
    },
    "activate_strategy_experiment": {
        "description": "把三档策略均通过样本外风险门禁的实验激活为策略版本。",
        "risk_level": "INTERNAL_WRITE",
        "approval_required": False,
        "max_attempts": 1,
    },
    "evaluate_candidate": {
        "description": "对候选与当前版本做对照回归。",
        "risk_level": "INTERNAL_WRITE",
        "approval_required": False,
        "max_attempts": 1,
    },
    "activate_candidate": {
        "description": "把评测通过的候选切换为当前配置版本。",
        "risk_level": "CONSEQUENTIAL_WRITE",
        "approval_required": True,
        "max_attempts": 1,
    },
    "rollback_version": {
        "description": "把当前配置回滚到历史版本。",
        "risk_level": "CONSEQUENTIAL_WRITE",
        "approval_required": True,
        "max_attempts": 1,
    },
}

TERMINAL_STATUSES = {"COMPLETED", "CANCELLED"}
RESUMABLE_STATUSES = {"FAILED", "INTERRUPTED"}
_run_locks: dict[str, threading.Lock] = {}
_run_locks_guard = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False,
                      sort_keys=True, default=str)


def _load(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


def _run_lock(run_key: str) -> threading.Lock:
    with _run_locks_guard:
        return _run_locks.setdefault(run_key, threading.Lock())


def _event(conn, run_id: int, event_type: str, message: str,
           payload: dict | None = None, level: str = "INFO") -> int:
    sequence = conn.execute(
        "SELECT COALESCE(MAX(sequence),0)+1 FROM harness_run_events WHERE run_id=?",
        (run_id,),
    ).fetchone()[0]
    cursor = conn.execute(
        """INSERT INTO harness_run_events
           (run_id,sequence,event_type,level,message,payload_json,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (run_id, sequence, event_type, level, message, _dump(payload or {}), _now()),
    )
    return int(cursor.lastrowid)


def _decode_run(row) -> dict:
    item = dict(row)
    item["input"] = _load(item.pop("input_json"), {})
    item["context"] = _load(item.pop("context_json"), {})
    item["plan"] = _load(item.pop("plan_json"), [])
    item["result"] = _load(item.pop("result_json"), {})
    return item


def _decode_thread(row) -> dict:
    item = dict(row)
    item["context"] = _load(item.pop("context_json"), {})
    return item


def _decode_event(row) -> dict:
    item = dict(row)
    item["payload"] = _load(item.pop("payload_json"), {})
    return item


def _decode_tool_call(row) -> dict:
    item = dict(row)
    item["arguments"] = _load(item.pop("arguments_json"), {})
    item["result"] = _load(item.pop("result_json"), {})
    item["approval_required"] = bool(TOOL_SPECS.get(item["tool_name"], {}).get("approval_required"))
    return item


def _decode_approval(row) -> dict:
    item = dict(row)
    item["request"] = _load(item.pop("request_json"), {})
    item["resolution"] = _load(item.pop("resolution_json"), {})
    return item


def _validate_inputs(workflow: str, inputs: dict) -> dict:
    if workflow not in WORKFLOWS:
        raise ValueError("不支持的 Harness 工作流")
    if not isinstance(inputs, dict):
        raise ValueError("工作流输入必须是对象")
    if workflow == "stock_analysis":
        from .stock_compare import normalize_profile, split_stock_inputs
        stocks = split_stock_inputs(inputs.get("stocks", []))
        return {
            "stocks": stocks,
            "profile": normalize_profile(str(inputs.get("profile", "balanced"))),
            "research_query": str(inputs.get("research_query", "")).strip()[:160],
        }
    if workflow == "quality_audit":
        return {"stock_limit": max(1, min(int(inputs.get("stock_limit", 20)), 100))}
    if workflow == "quant_portfolio":
        from .quant_portfolio import normalize_quant_request
        return normalize_quant_request(inputs)
    if workflow == "strategy_evolution":
        from .strategy_evolution import normalize_mandate
        return normalize_mandate(inputs)
    if workflow == "continuous_learning":
        phase = str(inputs.get("phase", "BACKFILL")).strip().upper()
        if phase not in {"PRE_OPEN", "POST_CLOSE", "BACKFILL"}:
            raise ValueError("持续学习阶段必须是 PRE_OPEN、POST_CLOSE 或 BACKFILL")
        return {
            "phase": phase,
            "stock_limit": max(3, min(int(inputs.get("stock_limit", 20)), 100)),
            "refresh_data": bool(inputs.get("refresh_data", phase != "BACKFILL")),
            "collect_sentiment": bool(inputs.get("collect_sentiment", True)),
            "refresh_calendar": bool(inputs.get("refresh_calendar", True)),
            "auto_promote": bool(inputs.get("auto_promote", True)),
            "max_social_symbols": max(0, min(int(inputs.get("max_social_symbols", 3)), 10)),
            "train_deep_model": bool(inputs.get("train_deep_model", True)),
            "deep_epochs": max(4, min(int(inputs.get("deep_epochs", 32)), 100)),
            "evolve_intraday": bool(inputs.get("evolve_intraday", True)),
            "intraday_interval": 5,
            "evolve_source_code": bool(inputs.get("evolve_source_code", phase == "POST_CLOSE")),
            "auto_promote_code": bool(inputs.get("auto_promote_code", True)),
            "max_drawdown": max(0.01, min(float(inputs.get("max_drawdown", 0.15)), 0.80)),
            "cycle_date": str(inputs.get("cycle_date", "")).strip() or None,
            "trigger_kind": str(inputs.get("trigger_kind", "harness"))[:80],
        }
    if workflow == "strategy_activation":
        experiment_key = str(inputs.get("experiment_key", "")).strip()
        if not experiment_key:
            raise ValueError("必须指定策略实验")
        return {"experiment_key": experiment_key}
    if workflow == "candidate_activation":
        candidate_id = int(inputs.get("candidate_id", 0))
        if candidate_id <= 0:
            raise ValueError("候选 ID 必须是正整数")
        return {"candidate_id": candidate_id}
    version_key = str(inputs.get("version_key", "")).strip()
    if not version_key:
        raise ValueError("必须指定回滚版本")
    return {"version_key": version_key}


def _build_plan(workflow: str, inputs: dict) -> list[dict]:
    if workflow == "stock_analysis":
        queries = list(inputs["stocks"])
        if inputs.get("research_query"):
            queries.insert(0, inputs["research_query"])
        return [
            {"step_key": "resolve", "tool": "resolve_stocks", "summary": "解析并核对股票身份",
             "arguments": {"stocks": inputs["stocks"]}},
            {"step_key": "compare", "tool": "compare_stocks", "summary": "运行评分、风险与历史回放",
             "arguments": {"stocks": inputs["stocks"], "profile": inputs["profile"]}},
            {"step_key": "research", "tool": "search_research", "summary": "检索本地研究证据",
             "arguments": {"queries": queries, "limit_per_query": 4}},
        ]
    if workflow == "quality_audit":
        return [
            {"step_key": "audit", "tool": "autonomous_audit", "summary": "执行确定性主动探针",
             "arguments": {"stock_limit": inputs["stock_limit"]}},
            {"step_key": "regression", "tool": "baseline_regression", "summary": "运行当前版本全量回归",
             "arguments": {}},
            {"step_key": "review", "tool": "list_activation_candidates", "summary": "整理待人工审阅候选",
             "arguments": {}},
        ]
    if workflow == "quant_portfolio":
        return [
            {"step_key": "construct", "tool": "construct_quant_portfolio",
             "summary": "同步数据、自动选股并构建量化组合", "arguments": {"request": inputs}},
        ]
    if workflow == "strategy_evolution":
        return [
            {"step_key": "mandate", "tool": "validate_investment_mandate",
             "summary": "冻结投资授权书与风险口径", "arguments": {"mandate": inputs}},
            {"step_key": "evolve", "tool": "evolve_portfolio_strategies",
             "summary": "迭代三档策略并执行样本外验证", "arguments": {"mandate": inputs}},
        ]
    if workflow == "continuous_learning":
        return [
            {"step_key": "learn", "tool": "continuous_learning_cycle",
             "summary": "执行 A 股盘前预测、盘后评分与六个月滚动进化闭环",
             "arguments": inputs},
        ]
    if workflow == "strategy_activation":
        return [
            {"step_key": "review", "tool": "review_strategy_experiment",
             "summary": "复核实验和风险门禁", "arguments": {"experiment_key": inputs["experiment_key"]}},
            {"step_key": "activate", "tool": "activate_strategy_experiment",
             "summary": "通过全部风险门禁后自动激活策略版本",
             "arguments": {"experiment_key": inputs["experiment_key"]}},
        ]
    if workflow == "candidate_activation":
        return [
            {"step_key": "evaluate", "tool": "evaluate_candidate", "summary": "复评候选与回归门禁",
             "arguments": {"candidate_id": inputs["candidate_id"]}},
            {"step_key": "activate", "tool": "activate_candidate", "summary": "等待人工批准后晋级",
             "arguments": {"candidate_id": inputs["candidate_id"]}},
        ]
    return [
        {"step_key": "rollback", "tool": "rollback_version", "summary": "等待人工批准后回滚",
         "arguments": {"version_key": inputs["version_key"]}},
    ]


def _context_snapshot(workflow: str, inputs: dict, plan: list[dict]) -> dict:
    with closing(connect()) as conn:
        active = conn.execute(
            "SELECT version_key FROM harness_versions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        sources = [dict(row) for row in conn.execute(
            """SELECT code,enabled,health_status,last_success_at,last_error
               FROM data_sources ORDER BY priority LIMIT 20"""
        )]
    tool_names = [step["tool"] for step in plan]
    return {
        "captured_at": _now(),
        "workflow": workflow,
        "requested_input": inputs,
        "active_config_version": active["version_key"] if active else None,
        "source_health": sources,
        "available_tools": [{"name": name, **TOOL_SPECS[name]} for name in tool_names],
        "boundaries": {
            "tool_allowlist_only": True,
            "arbitrary_shell": False,
            "order_execution": False,
            "consequential_writes_require_approval": True,
            "automatic_candidate_activation": False,
            "automatic_research_model_promotion": workflow in {"continuous_learning", "quant_portfolio"},
            "automatic_allowlisted_code_promotion": workflow == "continuous_learning",
            "code_evolution_isolated_and_reversible": True,
        },
        "reasoning_engine": {
            "provider": "bounded-domain-planner",
            "version": "1",
            "generative_model": False,
        },
    }


def _thread_title(workflow: str, inputs: dict) -> str:
    if workflow == "stock_analysis":
        return f"股票研判：{' / '.join(inputs['stocks'][:4])}"
    if workflow == "quality_audit":
        return f"质量巡检：{inputs['stock_limit']} 只股票"
    if workflow == "quant_portfolio":
        return f"量化组合：{inputs['name']}"
    if workflow == "strategy_evolution":
        return f"策略自进化：{inputs['name']}"
    if workflow == "continuous_learning":
        return f"持续自进化：{inputs['phase']}"
    if workflow == "strategy_activation":
        return f"策略激活：{inputs['experiment_key']}"
    if workflow == "candidate_activation":
        return f"候选晋级：#{inputs['candidate_id']}"
    return f"版本回滚：{inputs['version_key']}"


def create_run(workflow: str, inputs: dict, intent: str = "", thread_key: str | None = None,
               requested_by: str = "user", start: bool = True) -> dict:
    initialize()
    normalized = _validate_inputs(str(workflow).strip(), inputs)
    plan = _build_plan(workflow, normalized)
    context = _context_snapshot(workflow, normalized, plan)
    now = _now()
    run_key = f"run_{uuid.uuid4().hex}"
    with closing(connect()) as conn:
        thread = None
        if thread_key:
            thread = conn.execute("SELECT * FROM harness_threads WHERE thread_key=?", (thread_key,)).fetchone()
            if not thread:
                raise ValueError("Harness 线程不存在")
        context["thread_memory"] = _load(thread["context_json"], {}) if thread else {}
        if not thread:
            thread_key = f"thr_{uuid.uuid4().hex}"
            cursor = conn.execute(
                """INSERT INTO harness_threads
                   (thread_key,title,status,context_json,created_at,updated_at)
                   VALUES(?,?,'ACTIVE','{}',?,?)""",
                (thread_key, _thread_title(workflow, normalized), now, now),
            )
            thread_id = int(cursor.lastrowid)
        else:
            thread_id = int(thread["id"])
            conn.execute(
                "UPDATE harness_threads SET status='ACTIVE',updated_at=? WHERE id=?",
                (now, thread_id),
            )
        cursor = conn.execute(
            """INSERT INTO harness_runs
               (run_key,thread_id,workflow,intent,status,input_json,context_json,plan_json,
                result_json,current_step,requested_by,created_at,updated_at)
               VALUES(?,?,?,?, 'QUEUED',?,?,?,'{}',0,?,?,?)""",
            (run_key, thread_id, workflow, str(intent or "").strip()[:1000], _dump(normalized),
             _dump(context), _dump(plan), str(requested_by or "user")[:160], now, now),
        )
        run_id = int(cursor.lastrowid)
        _event(conn, run_id, "RUN_CREATED", "Harness 运行已创建", {"workflow": workflow})
        _event(conn, run_id, "CONTEXT_CAPTURED", "已冻结本次运行所需业务上下文",
               {"active_config_version": context["active_config_version"],
                "tool_count": len(context["available_tools"])})
        _event(conn, run_id, "PLAN_CREATED", "领域规划器已生成受限执行计划",
               {"steps": [{"step_key": item["step_key"], "tool": item["tool"],
                            "summary": item["summary"]} for item in plan]})
        conn.commit()
    if start:
        _schedule_run(run_key)
    return get_run(run_key)


def _execute_tool(tool_name: str, arguments: dict) -> dict:
    if tool_name not in TOOL_SPECS:
        raise ValueError("工具不在 Harness 允许列表中")
    if tool_name == "resolve_stocks":
        from .stock_compare import resolve_stock
        return {"stocks": [resolve_stock(item) for item in arguments["stocks"]]}
    if tool_name == "compare_stocks":
        from .stock_compare import compare_stocks
        return compare_stocks(arguments["stocks"], arguments["profile"], refresh=False)
    if tool_name == "search_research":
        from .search import exact_search
        limit = max(1, min(int(arguments.get("limit_per_query", 4)), 10))
        searches = []
        for query in arguments.get("queries", [])[:10]:
            results = exact_search(str(query), None, limit=limit)
            searches.append({"query": str(query), "results": results[:limit]})
        return {"searches": searches, "result_count": sum(len(item["results"]) for item in searches)}
    if tool_name == "autonomous_audit":
        from .harness_autonomy import run_autonomous_cycle
        return run_autonomous_cycle(auto_apply=False, stock_limit=int(arguments["stock_limit"]))
    if tool_name == "baseline_regression":
        from .harness import evaluate_candidate
        return evaluate_candidate(None)
    if tool_name == "construct_quant_portfolio":
        from .quant_portfolio import run_quant_portfolio
        return run_quant_portfolio(arguments["request"], normalized=True,
                                   trigger_kind="harness_manual")
    if tool_name == "validate_investment_mandate":
        mandate = arguments["mandate"]
        return {
            "mandate": mandate,
            "risk_priority": True,
            "target_is_soft": mandate.get("target_semantics") == "soft_objective_not_guarantee",
            "order_execution": False,
        }
    if tool_name == "evolve_portfolio_strategies":
        from .strategy_evolution import run_strategy_evolution_with_auto_retry
        return run_strategy_evolution_with_auto_retry(arguments["mandate"], normalized=True)
    if tool_name == "continuous_learning_cycle":
        from .continuous_learning import run_continuous_learning_cycle
        return run_continuous_learning_cycle(arguments)
    if tool_name == "review_strategy_experiment":
        from .strategy_evolution import review_strategy_experiment
        return review_strategy_experiment(str(arguments["experiment_key"]))
    if tool_name == "activate_strategy_experiment":
        from .strategy_evolution import activate_strategy_experiment
        return activate_strategy_experiment(str(arguments["experiment_key"]))
    if tool_name == "list_activation_candidates":
        with closing(connect()) as conn:
            rows = conn.execute(
                """SELECT c.id,c.candidate_type,c.title,c.baseline_version,c.status,c.activatable,
                          e.status AS evaluation_status,e.regression_count,e.pass_rate,e.finished_at
                   FROM harness_candidates c
                   LEFT JOIN harness_evaluations e ON e.id=(
                     SELECT id FROM harness_evaluations WHERE candidate_id=c.id ORDER BY id DESC LIMIT 1)
                   WHERE c.activatable=1 AND c.status='EVALUATED'
                   ORDER BY c.id DESC LIMIT 50"""
            ).fetchall()
        candidates = [dict(row) for row in rows
                      if row["evaluation_status"] == "SUCCESS" and not row["regression_count"]]
        return {"candidates": candidates, "count": len(candidates)}
    if tool_name == "evaluate_candidate":
        from .harness import evaluate_candidate
        return evaluate_candidate(int(arguments["candidate_id"]))
    if tool_name == "activate_candidate":
        candidate_id = int(arguments["candidate_id"])
        with closing(connect()) as conn:
            active = conn.execute(
                """SELECT v.* FROM harness_versions v JOIN harness_candidates c ON c.id=v.candidate_id
                   WHERE c.id=? AND c.status='ACTIVE' ORDER BY v.id DESC LIMIT 1""",
                (candidate_id,),
            ).fetchone()
        if active:
            item = dict(active)
            item["config"] = _load(item.pop("config_json"), {})
            return item
        from .harness import approve_candidate
        return approve_candidate(candidate_id, str(arguments["_approved_by"]), True)
    if tool_name == "rollback_version":
        version_key = str(arguments["version_key"])
        with closing(connect()) as conn:
            active = conn.execute(
                "SELECT * FROM harness_versions WHERE version_key=? AND status='ACTIVE'",
                (version_key,),
            ).fetchone()
        if active:
            item = dict(active)
            item["config"] = _load(item.pop("config_json"), {})
            return item
        from .harness import rollback_version
        return rollback_version(version_key, str(arguments["_approved_by"]), True)
    raise ValueError("工具实现不存在")


def _compact_result(tool_name: str, result: dict) -> dict:
    if tool_name == "resolve_stocks":
        return {"stocks": result.get("stocks", [])}
    if tool_name == "compare_stocks":
        verdict = result.get("verdict", {})
        return {"as_of": result.get("as_of"), "winner": verdict.get("winner"),
                "headline": verdict.get("headline"), "ranking_count": len(result.get("ranking", []))}
    if tool_name == "search_research":
        return {"result_count": result.get("result_count", 0),
                "queries": [item.get("query") for item in result.get("searches", [])]}
    if tool_name == "autonomous_audit":
        return {key: result.get(key) for key in
                ("status", "probe_count", "failed_count", "candidate_count", "activated_count", "summary")}
    if tool_name in {"baseline_regression", "evaluate_candidate"}:
        return {key: result.get(key) for key in
                ("status", "total_cases", "passed_cases", "failed_cases", "regression_count", "pass_rate")}
    if tool_name == "list_activation_candidates":
        return {"count": result.get("count", 0)}
    if tool_name == "construct_quant_portfolio":
        recommendation = result.get("recommendation", {})
        return {"run_key": result.get("run_key"),
                "version_status": result.get("version", {}).get("status"),
                "profile": recommendation.get("profile"),
                "positions": len(recommendation.get("positions", [])),
                "data_asof": recommendation.get("data_asof")}
    if tool_name == "validate_investment_mandate":
        mandate = result.get("mandate", {})
        return {"capital": mandate.get("capital"), "horizon_months": mandate.get("horizon_months"),
                "max_drawdown_pct": mandate.get("max_drawdown_pct"),
                "universe_count": len(mandate.get("universe", []))}
    if tool_name == "evolve_portfolio_strategies":
        return {"experiment_key": result.get("experiment_key"),
                "activation_eligible": result.get("activation_eligible"),
                "profiles": len(result.get("strategies", []))}
    if tool_name == "continuous_learning_cycle":
        metrics = result.get("metrics", {})
        evaluation = metrics.get("evaluation", {})
        return {"cycle_key": result.get("cycle_key"), "status": result.get("status"),
                "data_asof": metrics.get("data_asof"),
                "scored": metrics.get("scoring", {}).get("scored", 0),
                "predictions": metrics.get("next_predictions", {}).get("count", 0),
                "candidate_status": evaluation.get("status")}
    if tool_name == "review_strategy_experiment":
        reviewed = result.get("result", {})
        return {"experiment_key": result.get("experiment_key"), "status": result.get("status"),
                "activation_eligible": reviewed.get("activation_eligible")}
    if tool_name == "activate_strategy_experiment":
        return {"version_key": result.get("version_key"), "status": result.get("status")}
    return {"version_key": result.get("version_key"), "status": result.get("status")}


def _synthesize(workflow: str, outputs: dict[str, dict]) -> dict:
    if workflow == "stock_analysis":
        resolved = outputs.get("resolve", {}).get("stocks", [])
        comparison = outputs.get("compare", {})
        research = outputs.get("research", {})
        verdict = comparison.get("verdict", {})
        ranking = comparison.get("ranking", [])
        return {
            "status": "COMPLETED",
            "summary": verdict.get("headline") or "股票研判已完成",
            "facts": [
                {"label": "事实", "text": f"已解析 {len(resolved)} 只股票：" +
                 "、".join(f"{item.get('name')}（{item.get('symbol')}）" for item in resolved)},
                {"label": "事实", "text": f"行情数据截至 {comparison.get('as_of') or '未知'}；本地研究命中 {research.get('result_count', 0)} 条。"},
            ],
            "opinion": {"label": "模型观点", "text": verdict.get("reason") or "没有形成可用排序观点。"},
            "hypotheses": [{"label": "推翻条件", "text": item.get("invalidation")}
                           for item in ranking[:3] if item.get("invalidation")],
            "artifacts": {"comparison": comparison, "research": research},
            "order_execution": False,
        }
    if workflow == "quality_audit":
        audit = outputs.get("audit", {})
        regression = outputs.get("regression", {})
        review = outputs.get("review", {})
        return {
            "status": "COMPLETED",
            "summary": audit.get("summary") or "质量巡检已完成",
            "facts": [
                {"label": "事实", "text": f"主动探针 {audit.get('probe_count', 0)} 项，失败 {audit.get('failed_count', 0)} 项。"},
                {"label": "事实", "text": f"回归通过 {regression.get('passed_cases', 0)}/{regression.get('total_cases', 0)}；待人工审阅候选 {review.get('count', 0)} 项。"},
            ],
            "candidates": review.get("candidates", []),
            "automatic_activation": False,
            "order_execution": False,
        }
    if workflow == "quant_portfolio":
        quant = outputs.get("construct", {})
        recommendation = quant.get("recommendation", {})
        expectation = recommendation.get("expectation", {})
        metrics = recommendation.get("holdout_metrics", {})
        positions = recommendation.get("positions", [])
        decision_label = recommendation.get("decision_label") or "已按收益与风险择优"
        return {
            "status": "COMPLETED",
            "summary": (f"{decision_label}：{recommendation.get('profile_label', '自动')}档，"
                        f"当前建议 {len(positions)} 只股票，预期收益中位数 "
                        f"{expectation.get('p50', 0):.1%}"),
            "facts": [
                {"label": "决策结论", "text":
                 f"{decision_label}；目标收益 {recommendation.get('target_return', 0):.1%}，"
                 f"P50 差额 {recommendation.get('target_gap', 0):+.1%}。"},
                {"label": "数据截点", "text":
                 f"行情截至 {recommendation.get('data_asof') or '未知'}；预测模型 {recommendation.get('model_version') or '未知'}。"},
                {"label": "预期区间", "text":
                 f"按投资期限的历史留出集区块自助结果：P10 {expectation.get('p10', 0):.1%}，P50 {expectation.get('p50', 0):.1%}，P90 {expectation.get('p90', 0):.1%}。"},
                {"label": "目标概率", "text":
                 f"历史模拟达到目标的比例 {expectation.get('probability_target', 0):.1%}；亏损比例 {expectation.get('probability_loss', 0):.1%}。"},
                {"label": "样本外风控", "text":
                 f"最大回撤 {abs(metrics.get('max_drawdown', 0)):.1%}；风险门禁 {'通过' if metrics.get('risk_pass') else '未通过'}。"},
            ],
            "quant_portfolio": quant,
            "recommendation": recommendation,
            "version": quant.get("version", {}),
            "research_only": True,
            "order_execution": False,
        }
    if workflow == "strategy_evolution":
        experiment = outputs.get("evolve", {})
        strategies = experiment.get("strategies", [])
        risk_passes = sum(item.get("holdout", {}).get("metrics", {}).get("risk_pass", False)
                          for item in strategies)
        non_regressions = sum(item.get("non_regression_pass", False) for item in strategies)
        target_hits = sum(item.get("holdout", {}).get("metrics", {}).get("target_reached", False)
                          for item in strategies)
        return {
            "status": "COMPLETED",
            "summary": f"三档策略实验完成：样本外风控通过 {risk_passes}/3，目标收益命中 {target_hits}/3",
            "facts": [
                {"label": "风险门禁", "text": f"独立留出集通过 {risk_passes}/3；风险不合格的策略不可激活。"},
                {"label": "非退化门禁", "text": f"相对第 1 轮基线通过 {non_regressions}/3；留出集退化的策略不可激活。"},
                {"label": "收益目标", "text": f"按投资期限折算后命中 {target_hits}/3；目标是软目标，不是收益承诺。"},
                {"label": "成交约束", "text": "已计入佣金、最低收费、卖出印花税、滑点、整手、成交量、停牌、涨跌停近似和 T+1。"},
            ],
            "experiment_key": experiment.get("experiment_key"),
            "strategies": strategies,
            "activation_eligible": experiment.get("activation_eligible", False),
            "activation_requires_human_approval": False,
            "automatic_activation": True,
            "automatic_version": experiment.get("automatic_version"),
            "retry_status": experiment.get("retry_status"),
            "retry_reason": experiment.get("retry_reason"),
            "retry_job": experiment.get("retry_job"),
            "artifacts": {"experiment": experiment},
            "order_execution": False,
        }
    if workflow == "continuous_learning":
        cycle = outputs.get("learn", {})
        metrics = cycle.get("metrics", {})
        evaluation = metrics.get("evaluation", {})
        baseline = evaluation.get("baseline_metrics", {})
        candidate = evaluation.get("candidate_metrics", {})
        return {
            "status": "COMPLETED",
            "summary": (f"持续学习完成：评分 {metrics.get('scoring', {}).get('scored', 0)} 条，"
                        f"生成 {metrics.get('next_predictions', {}).get('count', 0)} 条次日预测，"
                        f"候选模型 {evaluation.get('status', '未知')}"),
            "facts": [
                {"label": "数据截点", "text": f"本轮行情截至 {metrics.get('data_asof') or '未知'}。"},
                {"label": "样本外准确率", "text":
                 f"基线 {baseline.get('directional_accuracy', 0):.2%}；候选 {candidate.get('directional_accuracy', 0):.2%}。"},
                {"label": "概率误差", "text":
                 f"基线 Brier {baseline.get('brier_score', 0):.4f}；候选 {candidate.get('brier_score', 0):.4f}，越低越好。"},
                {"label": "晋级门禁", "text":
                 f"候选状态 {evaluation.get('status', '未知')}；最大回撤 {candidate.get('max_drawdown', 0):.2%}。"},
            ],
            "cycle": cycle,
            "research_model_auto_promotion": True,
            "order_execution": False,
        }
    if workflow == "strategy_activation":
        version = outputs.get("activate", {})
        return {"status": "COMPLETED", "summary": f"策略实验已激活为 {version.get('version_key')}",
                "version": version, "approved": True, "order_execution": False}
    if workflow == "candidate_activation":
        version = outputs.get("activate", {})
        return {"status": "COMPLETED", "summary": f"候选已晋级为 {version.get('version_key')}",
                "version": version, "approved": True, "order_execution": False}
    version = outputs.get("rollback", {})
    return {"status": "COMPLETED", "summary": f"已回滚至 {version.get('version_key')}",
            "version": version, "approved": True, "order_execution": False}


def _ensure_tool_call(conn, run_id: int, step: dict):
    spec = TOOL_SPECS[step["tool"]]
    conn.execute(
        """INSERT OR IGNORE INTO harness_tool_calls
           (run_id,step_key,tool_name,risk_level,status,arguments_json,result_json,created_at)
           VALUES(?,?,?,?, 'PROPOSED',?,'{}',?)""",
        (run_id, step["step_key"], step["tool"], spec["risk_level"],
         _dump(step["arguments"]), _now()),
    )
    return conn.execute(
        "SELECT * FROM harness_tool_calls WHERE run_id=? AND step_key=?",
        (run_id, step["step_key"]),
    ).fetchone()


def _ensure_approval(conn, run_id: int, call, step: dict) -> tuple[Any, bool]:
    existing = conn.execute("SELECT * FROM harness_approvals WHERE tool_call_id=?", (call["id"],)).fetchone()
    if existing:
        return existing, False
    approval_key = f"apr_{uuid.uuid4().hex}"
    summary = f"{step['summary']}：{TOOL_SPECS[step['tool']]['description']}"
    conn.execute(
        """INSERT INTO harness_approvals
           (approval_key,run_id,tool_call_id,action,summary,status,request_json,resolution_json,requested_at)
           VALUES(?,?,?,?,?,'PENDING',?,'{}',?)""",
        (approval_key, run_id, call["id"], step["tool"], summary,
         _dump({"arguments": step["arguments"], "risk_level": "CONSEQUENTIAL_WRITE"}), _now()),
    )
    return conn.execute("SELECT * FROM harness_approvals WHERE approval_key=?", (approval_key,)).fetchone(), True


def execute_run(run_key: str) -> dict:
    initialize()
    lock = _run_lock(run_key)
    if not lock.acquire(blocking=False):
        return get_run(run_key)
    try:
        with closing(connect()) as conn:
            row = conn.execute("SELECT * FROM harness_runs WHERE run_key=?", (run_key,)).fetchone()
            if not row:
                raise ValueError("Harness 运行不存在")
            if row["status"] in TERMINAL_STATUSES:
                return get_run(run_key)
            if row["status"] == "WAITING_APPROVAL":
                pending = conn.execute(
                    "SELECT 1 FROM harness_approvals WHERE run_id=? AND status='PENDING'", (row["id"],)
                ).fetchone()
                if pending:
                    return get_run(run_key)
            resumed = bool(row["started_at"])
            now = _now()
            conn.execute(
                """UPDATE harness_runs SET status='RUNNING',started_at=COALESCE(started_at,?),
                   updated_at=?,error=NULL WHERE id=?""", (now, now, row["id"]),
            )
            _event(conn, row["id"], "RUN_RESUMED" if resumed else "RUN_STARTED",
                   "Harness 从最近检查点继续执行" if resumed else "Harness 开始执行受限代理循环")
            conn.commit()
            run_id = int(row["id"])
            plan = _load(row["plan_json"], [])
            current_step = int(row["current_step"])

        for index in range(current_step, len(plan)):
            step = plan[index]
            spec = TOOL_SPECS[step["tool"]]
            with closing(connect()) as conn:
                state = conn.execute("SELECT status FROM harness_runs WHERE id=?", (run_id,)).fetchone()
                if not state or state["status"] == "CANCELLED":
                    return get_run(run_key)
                call = _ensure_tool_call(conn, run_id, step)
                if call["status"] == "SUCCEEDED":
                    conn.execute("UPDATE harness_runs SET current_step=?,updated_at=? WHERE id=?",
                                 (index + 1, _now(), run_id))
                    conn.commit()
                    continue
                approved_by = None
                if spec["approval_required"]:
                    approval, created = _ensure_approval(conn, run_id, call, step)
                    if approval["status"] == "PENDING":
                        conn.execute("UPDATE harness_tool_calls SET status='WAITING_APPROVAL' WHERE id=?",
                                     (call["id"],))
                        conn.execute("UPDATE harness_runs SET status='WAITING_APPROVAL',updated_at=? WHERE id=?",
                                     (_now(), run_id))
                        if created:
                            _event(conn, run_id, "APPROVAL_REQUIRED", "高影响操作已暂停，等待人工批准",
                                   {"approval_key": approval["approval_key"], "action": step["tool"],
                                    "summary": approval["summary"]}, "WARNING")
                        conn.commit()
                        return get_run(run_key)
                    if approval["status"] == "REJECTED":
                        now = _now()
                        conn.execute("UPDATE harness_tool_calls SET status='REJECTED',finished_at=? WHERE id=?",
                                     (now, call["id"]))
                        conn.execute(
                            "UPDATE harness_runs SET status='CANCELLED',error=?,updated_at=?,finished_at=? WHERE id=?",
                            ("人工拒绝高影响操作", now, now, run_id),
                        )
                        _event(conn, run_id, "RUN_CANCELLED", "人工拒绝操作，运行已停止", {}, "WARNING")
                        conn.commit()
                        return get_run(run_key)
                    approved_by = approval["resolved_by"]
                conn.commit()

            max_attempts = int(spec["max_attempts"])
            result = None
            for attempt in range(1, max_attempts + 1):
                with closing(connect()) as conn:
                    now = _now()
                    conn.execute(
                        """UPDATE harness_tool_calls SET status='RUNNING',attempts=?,started_at=COALESCE(started_at,?),
                           error=NULL WHERE run_id=? AND step_key=?""",
                        (attempt, now, run_id, step["step_key"]),
                    )
                    _event(conn, run_id, "TOOL_STARTED", step["summary"],
                           {"tool": step["tool"], "step_key": step["step_key"], "attempt": attempt})
                    conn.commit()
                arguments = dict(step["arguments"])
                if approved_by:
                    arguments["_approved_by"] = approved_by
                try:
                    result = _execute_tool(step["tool"], arguments)
                except Exception as exc:
                    with closing(connect()) as conn:
                        if attempt < max_attempts:
                            conn.execute(
                                "UPDATE harness_tool_calls SET status='RETRYING',error=? WHERE run_id=? AND step_key=?",
                                (str(exc), run_id, step["step_key"]),
                            )
                            _event(conn, run_id, "TOOL_RETRY", "工具调用失败，将按边界重试",
                                   {"tool": step["tool"], "attempt": attempt, "error": str(exc)}, "WARNING")
                            conn.commit()
                            continue
                        now = _now()
                        conn.execute(
                            """UPDATE harness_tool_calls SET status='FAILED',error=?,finished_at=?
                               WHERE run_id=? AND step_key=?""",
                            (str(exc), now, run_id, step["step_key"]),
                        )
                        conn.execute(
                            """UPDATE harness_runs SET status='FAILED',error=?,updated_at=?,finished_at=? WHERE id=?""",
                            (str(exc), now, now, run_id),
                        )
                        _event(conn, run_id, "RUN_FAILED", "工具失败，已保留检查点供人工续跑",
                               {"tool": step["tool"], "step_key": step["step_key"], "error": str(exc)}, "ERROR")
                        conn.commit()
                    return get_run(run_key)
                break

            with closing(connect()) as conn:
                now = _now()
                conn.execute(
                    """UPDATE harness_tool_calls SET status='SUCCEEDED',result_json=?,error=NULL,finished_at=?
                       WHERE run_id=? AND step_key=?""",
                    (_dump(result), now, run_id, step["step_key"]),
                )
                conn.execute("UPDATE harness_runs SET current_step=?,updated_at=? WHERE id=?",
                             (index + 1, now, run_id))
                _event(conn, run_id, "TOOL_COMPLETED", f"{step['summary']}完成",
                       {"tool": step["tool"], "step_key": step["step_key"],
                        "result": _compact_result(step["tool"], result or {})})
                conn.commit()

        with closing(connect()) as conn:
            run = conn.execute("SELECT * FROM harness_runs WHERE id=?", (run_id,)).fetchone()
            calls = conn.execute(
                "SELECT step_key,result_json FROM harness_tool_calls WHERE run_id=? AND status='SUCCEEDED'",
                (run_id,),
            ).fetchall()
            outputs = {row["step_key"]: _load(row["result_json"], {}) for row in calls}
            result = _synthesize(run["workflow"], outputs)
            now = _now()
            conn.execute(
                """UPDATE harness_runs SET status='COMPLETED',result_json=?,error=NULL,
                   current_step=?,updated_at=?,finished_at=? WHERE id=?""",
                (_dump(result), len(_load(run["plan_json"], [])), now, now, run_id),
            )
            thread = conn.execute("SELECT * FROM harness_threads WHERE id=?", (run["thread_id"],)).fetchone()
            thread_context = _load(thread["context_json"], {})
            thread_context.update({"last_run_key": run_key, "last_workflow": run["workflow"],
                                   "last_summary": result.get("summary"), "updated_at": now})
            conn.execute(
                "UPDATE harness_threads SET status='ACTIVE',context_json=?,updated_at=? WHERE id=?",
                (_dump(thread_context), now, run["thread_id"]),
            )
            _event(conn, run_id, "RUN_COMPLETED", result.get("summary", "Harness 运行完成"),
                   {"workflow": run["workflow"], "order_execution": False})
            conn.commit()
        return get_run(run_key)
    finally:
        lock.release()


def _schedule_run(run_key: str) -> None:
    thread = threading.Thread(target=execute_run, args=(run_key,),
                              name=f"argus-harness-{run_key[-8:]}", daemon=True)
    thread.start()


def resolve_approval(approval_key: str, approved: bool, resolved_by: str,
                     confirmed: bool, start: bool = True) -> dict:
    actor = str(resolved_by or "").strip()
    if not confirmed or not actor:
        raise ValueError("必须明确确认并填写操作人")
    with closing(connect()) as conn:
        approval = conn.execute(
            """SELECT a.*,r.status AS run_status FROM harness_approvals a
               JOIN harness_runs r ON r.id=a.run_id WHERE a.approval_key=?""",
            (str(approval_key),),
        ).fetchone()
        if not approval:
            raise ValueError("审批请求不存在")
        if approval["status"] != "PENDING":
            raise ValueError("审批请求已经处理")
        if approval["run_status"] != "WAITING_APPROVAL":
            raise ValueError("对应运行已不再等待审批")
        status = "APPROVED" if approved else "REJECTED"
        now = _now()
        conn.execute(
            """UPDATE harness_approvals SET status=?,resolution_json=?,resolved_at=?,resolved_by=? WHERE id=?""",
            (status, _dump({"approved": bool(approved), "confirmed": True}), now, actor, approval["id"]),
        )
        conn.execute("UPDATE harness_runs SET status='QUEUED',updated_at=?,finished_at=NULL WHERE id=?",
                     (now, approval["run_id"]))
        _event(conn, approval["run_id"], "APPROVAL_RESOLVED",
               "人工已批准高影响操作" if approved else "人工已拒绝高影响操作",
               {"approval_key": approval_key, "status": status, "resolved_by": actor},
               "INFO" if approved else "WARNING")
        run_key = conn.execute("SELECT run_key FROM harness_runs WHERE id=?",
                               (approval["run_id"],)).fetchone()[0]
        conn.commit()
    if start:
        _schedule_run(run_key)
    return get_run(run_key)


def resume_run(run_key: str, requested_by: str = "user", start: bool = True) -> dict:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM harness_runs WHERE run_key=?", (str(run_key),)).fetchone()
        if not row:
            raise ValueError("Harness 运行不存在")
        if row["status"] not in RESUMABLE_STATUSES:
            raise ValueError("只有失败或中断的运行可以续跑")
        now = _now()
        conn.execute(
            "UPDATE harness_runs SET status='QUEUED',error=NULL,finished_at=NULL,updated_at=?,requested_by=? WHERE id=?",
            (now, str(requested_by or "user")[:160], row["id"]),
        )
        _event(conn, row["id"], "RUN_QUEUED_FOR_RESUME", "运行已从最近检查点加入续跑队列")
        conn.commit()
    if start:
        _schedule_run(run_key)
    return get_run(run_key)


def cancel_run(run_key: str, requested_by: str = "user") -> dict:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM harness_runs WHERE run_key=?", (str(run_key),)).fetchone()
        if not row:
            raise ValueError("Harness 运行不存在")
        if row["status"] in TERMINAL_STATUSES:
            return get_run(run_key)
        now = _now()
        actor = str(requested_by or "user")[:160]
        conn.execute(
            """UPDATE harness_approvals SET status='CANCELLED',resolution_json=?,
               resolved_at=?,resolved_by=? WHERE run_id=? AND status='PENDING'""",
            (_dump({"approved": False, "reason": "run_cancelled"}), now, actor, row["id"]),
        )
        conn.execute(
            "UPDATE harness_runs SET status='CANCELLED',error=?,updated_at=?,finished_at=? WHERE id=?",
            (f"由 {actor} 取消", now, now, row["id"]),
        )
        _event(conn, row["id"], "RUN_CANCELLED", "运行已由用户取消", {"requested_by": actor}, "WARNING")
        conn.commit()
    return get_run(run_key)


def recover_interrupted_runs() -> int:
    """Convert expired in-flight leases into explicit, resumable checkpoints."""
    initialize()
    lease_seconds = max(
        30, int(os.environ.get("ARGUS_HARNESS_RUN_LEASE_MINUTES", "240"))
    ) * 60
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT id,created_at,started_at,updated_at FROM harness_runs
               WHERE status IN ('RUNNING','QUEUED') ORDER BY id"""
        ).fetchall()
        rows = [row for row in rows if (
            (age := _timestamp_age_seconds(
                row["updated_at"] or row["started_at"] or row["created_at"]
            )) is None or age >= lease_seconds
        )]
        now = _now()
        for row in rows:
            conn.execute(
                """UPDATE harness_runs SET status='INTERRUPTED',error=?,updated_at=?,finished_at=? WHERE id=?""",
                ("服务重启中断；可从最近成功工具检查点续跑", now, now, row["id"]),
            )
            _event(conn, row["id"], "RUN_INTERRUPTED", "检测到服务重启，运行已转为可恢复状态", {}, "WARNING")
        conn.execute(
            """UPDATE harness_approvals SET status='CANCELLED',resolution_json=?,
               resolved_at=?,resolved_by='system recovery'
               WHERE status='PENDING' AND run_id IN (
                 SELECT id FROM harness_runs WHERE status!='WAITING_APPROVAL'
               )""",
            (_dump({"approved": False, "reason": "run_not_waiting"}), now),
        )
        conn.commit()
    return len(rows)


def get_run(run_key: str, after_event_id: int = 0) -> dict:
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT r.*,t.thread_key,t.title AS thread_title,t.status AS thread_status
               FROM harness_runs r JOIN harness_threads t ON t.id=r.thread_id WHERE r.run_key=?""",
            (str(run_key),),
        ).fetchone()
        if not row:
            raise ValueError("Harness 运行不存在")
        run_columns = {key: row[key] for key in row.keys()
                       if key not in {"thread_key", "thread_title", "thread_status"}}
        run = _decode_run(run_columns)
        run["display_title"] = _thread_title(run["workflow"], run["input"])
        events = [_decode_event(item) for item in conn.execute(
            "SELECT * FROM harness_run_events WHERE run_id=? AND id>? ORDER BY sequence",
            (run["id"], max(0, int(after_event_id))),
        )]
        calls = [_decode_tool_call(item) for item in conn.execute(
            "SELECT * FROM harness_tool_calls WHERE run_id=? ORDER BY id", (run["id"],)
        )]
        approvals = [_decode_approval(item) for item in conn.execute(
            "SELECT * FROM harness_approvals WHERE run_id=? ORDER BY id", (run["id"],)
        )]
    return {
        "run": run,
        "thread": {"thread_key": row["thread_key"], "title": row["thread_title"],
                   "status": row["thread_status"]},
        "events": events,
        "tool_calls": calls,
        "approvals": approvals,
        "can_resume": run["status"] in RESUMABLE_STATUSES,
        "waiting_approval": next((item for item in approvals if item["status"] == "PENDING"), None),
    }


def runtime_payload(limit: int = 20) -> dict:
    initialize()
    with closing(connect()) as conn:
        counts = dict(conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM harness_threads) AS threads,
                 (SELECT COUNT(*) FROM harness_runs) AS runs,
                 (SELECT COUNT(*) FROM harness_runs WHERE status='RUNNING') AS running,
                 (SELECT COUNT(*) FROM harness_runs WHERE status='WAITING_APPROVAL') AS waiting_approval,
                 (SELECT COUNT(*) FROM harness_runs WHERE status='FAILED') AS failed,
                 (SELECT COUNT(*) FROM harness_runs WHERE status='INTERRUPTED') AS interrupted"""
        ).fetchone())
        rows = conn.execute(
            """SELECT r.*,t.thread_key,t.title AS thread_title FROM harness_runs r
               JOIN harness_threads t ON t.id=r.thread_id ORDER BY r.id DESC LIMIT ?""",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        runs = []
        for row in rows:
            item = _decode_run({key: row[key] for key in row.keys()
                                if key not in {"thread_key", "thread_title"}})
            item["thread_key"] = row["thread_key"]
            item["thread_title"] = row["thread_title"]
            item["display_title"] = _thread_title(item["workflow"], item["input"])
            item["result"] = {"summary": item["result"].get("summary")} if item["result"] else {}
            runs.append(item)
        latest_quant_row = conn.execute(
            """SELECT r.*,t.thread_key,t.title AS thread_title FROM harness_runs r
               JOIN harness_threads t ON t.id=r.thread_id
               WHERE r.workflow='quant_portfolio' ORDER BY r.id DESC LIMIT 1"""
        ).fetchone()
        latest_quant_run = None
        if latest_quant_row:
            latest_quant_run = _decode_run({
                key: latest_quant_row[key] for key in latest_quant_row.keys()
                if key not in {"thread_key", "thread_title"}
            })
            latest_quant_run["thread_key"] = latest_quant_row["thread_key"]
            latest_quant_run["thread_title"] = latest_quant_row["thread_title"]
            latest_quant_run["display_title"] = _thread_title(
                latest_quant_run["workflow"], latest_quant_run["input"]
            )
            latest_quant_run["result"] = (
                {"summary": latest_quant_run["result"].get("summary")}
                if latest_quant_run["result"] else {}
            )
        pending = [_decode_approval(row) for row in conn.execute(
            """SELECT a.* FROM harness_approvals a JOIN harness_runs r ON r.id=a.run_id
               WHERE a.status='PENDING' AND r.status='WAITING_APPROVAL'
               ORDER BY a.requested_at DESC LIMIT 50"""
        )]
    return {
        "definition": "围绕代理循环的持久执行系统：理解任务、冻结上下文、调用受限工具、暴露进度、处理失败、请求批准并返回结果。",
        "planner": {"provider": "bounded-domain-planner", "generative_model": False,
                    "replaceable": True},
        "summary": counts,
        "workflows": [{"key": key, **value} for key, value in WORKFLOWS.items()],
        "tools": [{"name": key, **value} for key, value in TOOL_SPECS.items()],
        "runs": runs,
        "latest_quant_run": latest_quant_run,
        "pending_approvals": pending,
        "boundaries": {"arbitrary_shell": False, "order_execution": False,
                       "consequential_writes_require_approval": True,
                       "automatic_candidate_activation": False},
    }
