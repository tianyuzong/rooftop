"""Constraint-driven A-share selection, portfolio construction, and daily versioning.

The module produces research portfolios only. It cannot connect to a broker or
place orders. User risk limits are immutable inputs; market data, learned model
weights, rankings, and bounded strategy parameters may change by version.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import statistics
import unicodedata
import uuid
from array import array
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .charting import build_chart_series
from .db import DATA_LAKE, connect, initialize
from .strategy_evolution import (
    DEFAULT_PREFERENCE_WEIGHTS,
    DEFAULT_EXECUTION,
    PREFERENCE_LABELS,
    PROFILE_LABELS,
    STRATEGY_STYLES,
    _candidate_parameters,
    _load_aligned_universe,
    _signal,
    run_strategy_evolution,
)
from .timeframe_forecast import build_timeframe_forecast


SECTOR_ALIASES = {
    "新能源": ("电气设备", "新型电力", "汽车整车"),
    "新能源车": ("电气设备", "汽车整车", "汽车配件"),
    "军工": ("航空",),
    "消费": ("日常消费", "可选消费"),
    "金融": ("银行", "证券", "保险", "多元金融"),
    "医药": ("医药", "医疗"),
    "科技": ("半导体", "软件", "通信", "电脑硬件"),
}

RULE_SNAPSHOT_STANDARD_HISTORY_DAYS = 420
RULE_SNAPSHOT_MINIMUM_HISTORY_DAYS = 252
DAILY_REFRESH_BATCH_SIZE = 20


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
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


def _number(value, label: str, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if not math.isfinite(result) or result < low or result > high:
        raise ValueError(f"{label}必须在 {low:g} 到 {high:g} 之间")
    return result


def _split(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value)
    result = []
    for raw in values:
        normalized = unicodedata.normalize("NFKC", str(raw or ""))
        for item in re.split(r"[,，、;；\s]+", normalized.strip()):
            if item and item not in result:
                result.append(item)
    return result


def normalize_quant_request(inputs: dict) -> dict:
    if not isinstance(inputs, dict):
        raise ValueError("量化任务输入必须是对象")
    stocks = _split(inputs.get("stocks"))
    sectors = _split(inputs.get("sectors"))
    if any(re.fullmatch(r"[?\uff1f\ufffd\s]+", sector) for sector in sectors):
        raise ValueError("板块名称包含无法恢复的乱码，请重新输入正确板块")
    if not stocks and not sectors:
        raise ValueError("候选股票和关注板块至少填写一项")
    if len(stocks) > 30:
        raise ValueError("候选股票最多 30 只")
    capital = _number(inputs.get("capital", 100000), "本金", 1000, 1_000_000_000)
    horizon = int(_number(inputs.get("horizon_months", 12), "投资期限（月）", 1, 120))
    target = _number(inputs.get("target_return_pct", 20), "目标收益率", 0, 300)
    max_drawdown = _number(inputs.get("max_drawdown_pct", 15), "最大回撤", 1, 80)
    stop_loss = _number(inputs.get("stop_loss_pct", 8), "止损", 1, max_drawdown)
    take_profit = _number(inputs.get("take_profit_pct", 20), "止盈", 1, 300)
    trailing_stop = _number(
        inputs.get("trailing_stop_pct", min(8, max_drawdown)),
        "移动止盈回撤", 1, max_drawdown,
    )
    max_candidates = int(_number(inputs.get("max_candidates", 12), "候选池数量", 2, 30))
    max_positions = int(_number(inputs.get("max_positions", 2), "最大持仓数", 1, 8))
    if max_positions > max_candidates:
        raise ValueError("最大持仓数不能超过候选池数量")
    if stocks and max_positions > len(stocks):
        raise ValueError("最大持仓数不能超过手填候选股票数量")
    iterations = int(_number(inputs.get("max_iterations", 10), "迭代次数", 1, 20))
    take_profit_mode = str(inputs.get("take_profit_mode", "trailing")).strip().lower()
    if take_profit_mode not in {"trailing", "fixed"}:
        raise ValueError("止盈方式只能是 trailing 或 fixed")
    profile = str(inputs.get("risk_profile", "balanced")).strip().lower()
    if profile not in {"auto", "aggressive", "balanced", "conservative"}:
        raise ValueError("风险偏好只能是 auto、aggressive、balanced 或 conservative")
    backtest_window_years = int(_number(
        inputs.get("backtest_window_years", 3), "滚动回测年限", 1, 5
    ))
    if backtest_window_years not in {1, 3, 5}:
        raise ValueError("滚动回测年限只能选择 1、3 或 5 年")
    strategy_style = str(inputs.get("strategy_style", "auto")).strip().lower()
    if strategy_style != "auto" and strategy_style not in STRATEGY_STYLES:
        raise ValueError("不支持的细分策略类型")
    raw_weights = inputs.get("preference_weights") or {}
    if raw_weights and not isinstance(raw_weights, dict):
        raise ValueError("关注重点必须是对象")
    weights = {}
    for key, label in PREFERENCE_LABELS.items():
        if key in raw_weights:
            weights[key] = _number(raw_weights[key], label, 0, 100)
    if weights:
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("关注重点至少有一项大于 0")
        weights = {key: round(weights.get(key, 0.0) / total, 6)
                   for key in PREFERENCE_LABELS}
    elif profile == "auto":
        weights = dict(DEFAULT_PREFERENCE_WEIGHTS["balanced"])
    else:
        weights = dict(DEFAULT_PREFERENCE_WEIGHTS[profile])
    return {
        "name": str(inputs.get("name") or "A股量化组合").strip()[:80],
        "capital": round(capital, 2),
        "horizon_months": horizon,
        "target_return_pct": round(target, 4),
        "max_drawdown_pct": round(max_drawdown, 4),
        "stop_loss_pct": round(stop_loss, 4),
        "take_profit_pct": round(take_profit, 4),
        "trailing_stop_pct": round(trailing_stop, 4),
        "stocks": stocks,
        "sectors": sectors[:20],
        "max_candidates": max_candidates,
        "max_positions": max_positions,
        "max_iterations": iterations,
        "take_profit_mode": take_profit_mode,
        "risk_profile": profile,
        "backtest_window_years": backtest_window_years,
        "strategy_style": strategy_style,
        "preference_weights": weights,
        "preference_version": "preferences-" + hashlib.sha256(
            _dump({"weights": weights, "style": strategy_style,
                   "window": backtest_window_years}).encode("utf-8")
        ).hexdigest()[:12],
        "refresh_data": bool(inputs.get("refresh_data", True)),
        "collect_sentiment": bool(inputs.get("collect_sentiment", True)),
        "execution": dict(DEFAULT_EXECUTION),
        "research_only": True,
        "order_execution": False,
    }


_MANDATE_RUNTIME_FIELDS = {
    "name", "refresh_data", "collect_sentiment", "research_only", "order_execution",
}


def _mandate_identity(request: dict) -> str:
    """Identify immutable investment constraints, excluding execution-time switches."""
    payload = {
        key: value for key, value in request.items()
        if key not in _MANDATE_RUNTIME_FIELDS
    }
    payload["stocks"] = sorted(str(item) for item in payload.get("stocks", []))
    payload["sectors"] = sorted(str(item) for item in payload.get("sectors", []))
    return hashlib.sha256(_dump(payload).encode("utf-8")).hexdigest()


def _find_active_mandate(conn, request: dict):
    identity = _mandate_identity(request)
    for row in conn.execute(
        "SELECT * FROM quant_mandates WHERE status='ACTIVE' ORDER BY id DESC"
    ):
        if _mandate_identity(_load(row["input_json"], {})) == identity:
            return row
    return None


def _upsert_quant_mandate(conn, request: dict, stamp: str) -> tuple[int, str, bool]:
    existing = _find_active_mandate(conn, request)
    request_json = _dump(request)
    if existing:
        mandate_id = int(existing["id"])
        mandate_key = str(existing["mandate_key"])
        conn.execute(
            """UPDATE quant_mandates SET name=?,input_json=?,updated_at=?
               WHERE id=?""",
            (request["name"], request_json, stamp, mandate_id),
        )
        return mandate_id, mandate_key, False
    mandate_key = f"quant-mandate-{uuid.uuid4().hex}"
    cursor = conn.execute(
        """INSERT INTO quant_mandates
           (mandate_key,name,status,input_json,created_at,updated_at)
           VALUES(?,?,'ACTIVE',?,?,?)""",
        (mandate_key, request["name"], request_json, stamp, stamp),
    )
    return int(cursor.lastrowid), mandate_key, True


def _decision_summary(result: dict) -> str:
    recommendation = result.get("recommendation", {})
    label = recommendation.get("decision_label") or "已发布量化策略"
    profile = recommendation.get("profile_label") or recommendation.get("profile") or "自动"
    positions = recommendation.get("positions", [])
    research = recommendation.get("research_recommendations", [])
    research_names = "、".join(item.get("name", item.get("symbol", "")) for item in research)
    inference_kind = result.get("inference", {}).get("kind")
    if inference_kind == "CACHED_RULE_SNAPSHOT":
        data_asof = recommendation.get("data_asof") or result.get("data", {}).get("end")
        watchlist = recommendation.get("research_watchlist", [])
        names = "、".join(item.get("name", item.get("symbol", "")) for item in watchlist)
        actions = "；".join(
            f"{item.get('name', item.get('symbol', ''))}：{item.get('action_label')}"
            for item in watchlist if item.get("action_label")
        )
        if actions:
            return f"基于 {data_asof} 缓存K线的即时预测与复核：{actions}"
        if names:
            if not any(item.get("status") == "RULE_PASS" for item in watchlist):
                return f"基于 {data_asof} 缓存数据的即时排序：行情靠前 {names}，财务资料待补"
            return f"基于 {data_asof} 缓存数据的即时多因子排序：优先查看 {names}"
        return f"基于 {data_asof} 缓存数据的即时多因子排序：当前没有通过基础规则的股票"
    if inference_kind == "LATEST_TRADING_DAY_SNAPSHOT":
        data_asof = recommendation.get("data_asof") or result.get("data", {}).get("end")
        if positions:
            return (f"基于 {data_asof} 已评测模型的当前数据推荐：{profile}档，"
                    f"建议关注 {len(positions)} 只股票，等待盘后完整回测验证")
        if research:
            return (
                f"基于 {data_asof} 已评测模型的快照推荐：{profile}档，"
                f"试探推荐 {len(research)} 只：{research_names}（非正式持仓）"
            )
        return f"基于 {data_asof} 已评测模型的快照推荐：{profile}档，当前无可执行研究标的"
    p50 = float(recommendation.get("expectation", {}).get("p50", 0.0)) * 100
    if not positions and research:
        return (
            f"{label}：{profile}档，试探推荐 {research_names}（非正式持仓），"
            f"预期收益中位数 {p50:.1f}%"
        )
    return f"{label}：{profile}档，当前建议 {len(positions)} 只股票，预期收益中位数 {p50:.1f}%"


def _published_decision(conn, mandate_row) -> dict:
    mandate = {
        "id": int(mandate_row["id"]),
        "mandate_key": str(mandate_row["mandate_key"]),
        "name": str(mandate_row["name"]),
        "status": str(mandate_row["status"]),
        "input": _load(mandate_row["input_json"], {}),
        "updated_at": mandate_row["updated_at"],
    }
    latest_run = conn.execute(
        """SELECT * FROM quant_portfolio_runs
           WHERE mandate_id=? ORDER BY id DESC LIMIT 1""",
        (mandate["id"],),
    ).fetchone()
    version_row = conn.execute(
        """SELECT * FROM quant_portfolio_versions
           WHERE mandate_id=? AND status='ACTIVE' ORDER BY id DESC LIMIT 1""",
        (mandate["id"],),
    ).fetchone()
    latest_attempt = None
    if latest_run:
        attempt_result = _load(latest_run["result_json"], {})
        latest_attempt = {
            "run_key": str(latest_run["run_key"]),
            "status": str(latest_run["status"]),
            "trigger_kind": str(latest_run["trigger_kind"]),
            "data_asof": latest_run["data_asof"],
            "version_status": attempt_result.get("version", {}).get("status"),
            "started_at": latest_run["started_at"],
            "finished_at": latest_run["finished_at"],
            "error": latest_run["error"],
        }
    if not version_row:
        update_pending = bool(latest_run and latest_run["status"] == "RUNNING")
        try:
            result = _latest_trading_day_snapshot_result(mandate)
            evaluated_data_asof = str(
                result.get("recommendation", {}).get("data_asof")
                or result.get("data", {}).get("end") or ""
            )
            try:
                latest_cached_asof = _cached_candidate_asof(
                    mandate["input"],
                    minimum_history_days=RULE_SNAPSHOT_MINIMUM_HISTORY_DAYS,
                )
            except Exception:
                latest_cached_asof = evaluated_data_asof
            if latest_cached_asof > evaluated_data_asof:
                result = _cached_rule_snapshot_result(
                    mandate,
                    fallback_reason=(
                        f"最近完成评测的模型只到 {evaluated_data_asof}；"
                        f"当前日线已更新到 {latest_cached_asof}"
                    ),
                )
        except Exception as evaluated_snapshot_error:
            try:
                result = _cached_rule_snapshot_result(
                    mandate, fallback_reason=str(evaluated_snapshot_error)
                )
            except Exception as rule_snapshot_error:
                missing_reason = str(rule_snapshot_error)
                if str(evaluated_snapshot_error) != missing_reason:
                    missing_reason = (
                        f"即时规则结果缺少可用缓存：{missing_reason}；"
                        f"完整模型结果暂不可用：{evaluated_snapshot_error}"
                    )
                else:
                    missing_reason = f"即时结果缺少可用数据：{missing_reason}"
                return {
                    "status": "PENDING_FIRST_POST_CLOSE",
                    "mandate": mandate,
                    "version": None,
                    "result": None,
                    "summary": "当前缓存不足，暂时无法计算即时结果",
                    "data_asof": None,
                    "update_pending": update_pending,
                    "serving_previous_version": False,
                    "latest_attempt": latest_attempt,
                    "snapshot_error": missing_reason,
                    "serving_policy": "LATEST_EVALUATED_TRADING_DAY_THEN_ACTIVE",
                    "research_only": True,
                    "order_execution": False,
                }
        version = result["version"]
        is_live_rule_snapshot = version.get("status") == "RULE_SNAPSHOT"
        return {
            "status": "AVAILABLE",
            "mandate": mandate,
            "version": version,
            "result": result,
            "summary": _decision_summary(result),
            "data_asof": result["recommendation"]["data_asof"],
            "update_pending": update_pending,
            "serving_previous_version": update_pending,
            "latest_attempt": latest_attempt,
            "snapshot_inference": True,
            "serving_policy": (
                "LATEST_CACHED_RULE_SNAPSHOT_WHILE_MODEL_STALE"
                if is_live_rule_snapshot else
                "LATEST_EVALUATED_TRADING_DAY_THEN_ACTIVE"
            ),
            "research_only": True,
            "order_execution": False,
        }
    result = _load(version_row["result_json"], {})
    try:
        _ensure_recommendation_forecasts(result)
    except Exception as exc:
        recommendation = result.get("recommendation") or {}
        request = result.get("request") or mandate.get("input") or {}
        recommendation["portfolio_forecast"] = {
            "status": "UNAVAILABLE",
            "capital": round(float(request.get("capital") or 0.0), 2),
            "curve": [],
            "reason": f"组合预测补算失败：{exc}",
        }
    active_data_asof = str(
        result.get("recommendation", {}).get("data_asof")
        or result.get("data", {}).get("end") or ""
    )
    try:
        latest_cached_asof = _cached_candidate_asof(
            mandate["input"],
            minimum_history_days=RULE_SNAPSHOT_MINIMUM_HISTORY_DAYS,
        )
    except Exception:
        latest_cached_asof = active_data_asof
    if latest_cached_asof > active_data_asof:
        try:
            live_result = _cached_rule_snapshot_result(
                mandate,
                fallback_reason=(
                    f"上一正式模型只评测到 {active_data_asof}；"
                    f"当前日线已更新到 {latest_cached_asof}"
                ),
            )
        except Exception:
            live_result = None
        if live_result:
            return {
                "status": "AVAILABLE",
                "mandate": mandate,
                "version": live_result["version"],
                "result": live_result,
                "summary": _decision_summary(live_result),
                "data_asof": live_result["recommendation"]["data_asof"],
                "update_pending": True,
                "serving_previous_version": False,
                "latest_attempt": latest_attempt,
                "snapshot_inference": True,
                "superseded_active_version": str(version_row["version_key"]),
                "serving_policy": "LATEST_CACHED_RULE_SNAPSHOT_WHILE_ACTIVE_MODEL_STALE",
                "research_only": True,
                "order_execution": False,
            }
    version = {
        "version_key": str(version_row["version_key"]),
        "status": "ACTIVE",
        "score": float(version_row["score"]),
        "gate": _load(version_row["gate_json"], {}),
        "run_id": int(version_row["run_id"]),
        "created_at": version_row["created_at"],
        "activated_at": version_row["activated_at"],
    }
    result["version"] = version
    newer_attempt = bool(latest_run and int(latest_run["id"]) > int(version_row["run_id"]))
    update_pending = bool(newer_attempt and latest_run["status"] == "RUNNING")
    serving_previous = bool(
        newer_attempt and (
            latest_run["status"] in {"RUNNING", "FAILED", "INTERRUPTED"} or
            latest_attempt.get("version_status") == "REJECTED"
        )
    )
    recommendation = result.get("recommendation", {})
    return {
        "status": "AVAILABLE",
        "mandate": mandate,
        "version": version,
        "result": result,
        "summary": _decision_summary(result),
        "data_asof": recommendation.get("data_asof") or result.get("data", {}).get("end"),
        "update_pending": update_pending,
        "serving_previous_version": serving_previous,
        "latest_attempt": latest_attempt,
        "serving_policy": "LAST_PUBLISHED_UNTIL_REPLACED",
        "research_only": True,
        "order_execution": False,
    }


def resolve_quant_decision(inputs: dict) -> dict:
    """Read a published strategy or infer from the latest evaluated cached snapshot."""
    initialize()
    request = normalize_quant_request(inputs)
    with closing(connect()) as conn:
        row = _find_active_mandate(conn, request)
        if not row:
            return {
                "status": "UNREGISTERED",
                "mandate": None,
                "version": None,
                "result": None,
                "summary": "尚未登记这组投资约束",
                "data_asof": None,
                "update_pending": False,
                "serving_previous_version": False,
                "latest_attempt": None,
                "serving_policy": "LAST_PUBLISHED_UNTIL_REPLACED",
                "research_only": True,
                "order_execution": False,
            }
        return _published_decision(conn, row)


def register_quant_mandate(inputs: dict) -> dict:
    """Persist constraints only; computation is deferred to the post-close worker."""
    initialize()
    request = normalize_quant_request(inputs)
    request["refresh_data"] = False
    request["collect_sentiment"] = False
    stamp = _now()
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        mandate_id, mandate_key, created = _upsert_quant_mandate(conn, request, stamp)
        conn.commit()
        row = conn.execute("SELECT * FROM quant_mandates WHERE id=?", (mandate_id,)).fetchone()
        decision = _published_decision(conn, row)
    try:
        from .signal_service import materialize_decision_signals
        signal_publication = materialize_decision_signals(
            decision, dispatch=False, conn_factory=connect
        )
    except Exception as exc:
        signal_publication = {
            "created": 0, "queued": 0, "status": "FAILED",
            "error": str(exc), "research_only": True, "order_execution": False,
        }
    return {
        "status": "REGISTERED" if created else "EXISTING",
        "mandate_key": mandate_key,
        "decision": decision,
        "execution": "READ_ACTIVE_OR_INFER_LATEST_TRADING_DAY",
        "post_close_refresh": "DEFERRED_TO_POST_CLOSE",
        "refresh_started": False,
        "backtest_started": False,
        "snapshot_inference": bool(decision.get("snapshot_inference")),
        "signal_publication": signal_publication,
        "research_only": True,
        "order_execution": False,
    }


def _tdx_home() -> Path | None:
    candidates = [os.environ.get("ARGUS_TDX_HOME")]
    for value in candidates:
        if value and Path(value).is_dir():
            return Path(value)
    return None


def _read_tdx_names(path: Path, exchange: str) -> dict[str, str]:
    payload = path.read_bytes()
    body = payload[50:]
    record_size = next((size for size in (360, 314) if len(body) % size == 0), None)
    if not record_size:
        raise RuntimeError(f"无法识别通达信证券列表格式：{path}")
    name_offset = 31 if record_size == 360 else 23
    prefixes = (("600", "601", "603", "605", "688") if exchange == "SH" else
                ("000", "001", "002", "003", "300", "301"))
    result = {}
    for start in range(0, len(body), record_size):
        record = body[start:start + record_size]
        code = record[:6].decode("ascii", errors="ignore")
        if not re.fullmatch(r"\d{6}", code) or not code.startswith(prefixes):
            continue
        name = record[name_offset:name_offset + 32].split(b"\x00", 1)[0].decode(
            "gbk", errors="ignore"
        ).replace(" ", "").strip()
        if name:
            result[code] = name
    return result


def _read_tdx_industries(incon_path: Path) -> dict[str, str]:
    result = {}
    active = False
    for line in incon_path.read_text(encoding="gbk", errors="replace").splitlines():
        if line == "#TDXNHY":
            active = True
            continue
        if active and line == "######":
            break
        if active and "|" in line:
            code, name = line.split("|", 1)
            if code.startswith("T") and name:
                result[code] = name.strip()
    return result


def sync_a_share_universe(force: bool = False) -> dict:
    """Refresh A-share names and TDX industry memberships from the desktop cache."""
    initialize()
    with closing(connect()) as conn:
        cached = conn.execute("SELECT COUNT(*) FROM a_share_universe_assets").fetchone()[0]
        latest = conn.execute(
            "SELECT MAX(source_asof) FROM a_share_universe_assets"
        ).fetchone()[0]
    home = _tdx_home()
    if not home:
        if cached:
            return {"status": "CACHED", "assets": cached, "source_asof": latest,
                    "warning": "未找到通达信目录，沿用行业缓存"}
        raise RuntimeError("未找到通达信目录，无法建立 A 股板块候选池")
    cache = home / "T0002" / "hq_cache"
    paths = {
        "SH": cache / "shs.tnf", "SZ": cache / "szs.tnf",
        "mapping": cache / "tdxhy.cfg", "industries": home / "incon.dat",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        if cached:
            return {"status": "CACHED", "assets": cached, "source_asof": latest,
                    "warning": "通达信行业文件不完整，沿用行业缓存"}
        raise RuntimeError("通达信行业文件不完整：" + "、".join(missing))
    source_asof = datetime.fromtimestamp(
        max(path.stat().st_mtime for path in paths.values()), timezone.utc
    ).isoformat()
    if cached and not force and latest and str(latest) >= source_asof:
        return {"status": "UNCHANGED", "assets": cached, "source_asof": latest}
    names = {}
    exchanges = {}
    for exchange in ("SH", "SZ"):
        for symbol, name in _read_tdx_names(paths[exchange], exchange).items():
            names[symbol] = name
            exchanges[symbol] = exchange
    industries = _read_tdx_industries(paths["industries"])
    mappings = {}
    for line in paths["mapping"].read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split("|")
        if len(parts) >= 3 and parts[1] in names and parts[2] in industries:
            mappings[parts[1]] = parts[2]
    stamp = _now()
    memberships = []
    industry_codes = sorted(industries, key=len)
    for symbol, leaf_code in mappings.items():
        for code in industry_codes:
            if leaf_code.startswith(code):
                memberships.append((symbol, code, industries[code], len(code),
                                    "tdx_local", source_asof, stamp))
    with closing(connect()) as conn:
        conn.executemany(
            """INSERT INTO a_share_universe_assets
               (symbol,name,exchange,industry_code,source,source_asof,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET name=excluded.name,
                 exchange=excluded.exchange,industry_code=excluded.industry_code,
                 source=excluded.source,source_asof=excluded.source_asof,
                 updated_at=excluded.updated_at""",
            [(symbol, name, exchanges[symbol], mappings.get(symbol), "tdx_local",
              source_asof, stamp) for symbol, name in names.items()],
        )
        conn.executemany(
            """INSERT INTO assets
               (symbol,exchange_symbol,name,market,asset_type,currency,data_status)
               VALUES(?,?,?,'CN','EQUITY','CNY','LIVE_METADATA')
               ON CONFLICT(symbol) DO UPDATE SET name=excluded.name,
                 exchange_symbol=excluded.exchange_symbol,
                 asset_type=CASE WHEN assets.asset_type IN ('ETF','INDEX')
                                 THEN assets.asset_type ELSE 'EQUITY' END,
                 data_status=CASE WHEN assets.data_status='DEMO'
                                  THEN 'LIVE_METADATA' ELSE assets.data_status END""",
            [(symbol, f"{exchanges[symbol]}.{symbol}", name)
             for symbol, name in names.items()],
        )
        conn.execute("DELETE FROM a_share_sector_memberships WHERE source='tdx_local'")
        conn.executemany(
            """INSERT INTO a_share_sector_memberships
               (symbol,sector_code,sector_name,sector_level,source,source_asof,updated_at)
               VALUES(?,?,?,?,?,?,?)""", memberships,
        )
        conn.commit()
    return {"status": "REFRESHED", "assets": len(names),
            "memberships": len(memberships), "source_asof": source_asof}


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).lower()


def _resolve_sectors(requested: list[str]) -> dict:
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT DISTINCT sector_code,sector_name FROM a_share_sector_memberships
               ORDER BY length(sector_code),sector_code"""
        ).fetchall()
    available = [(str(row["sector_code"]), str(row["sector_name"])) for row in rows]
    matched_codes, matched_names, unmatched = set(), set(), []
    for raw in requested:
        query = _normalized_text(raw)
        terms = [raw, *SECTOR_ALIASES.get(query, ())]
        local_matches = []
        for term in terms:
            normalized = _normalized_text(term)
            local_matches.extend((code, name) for code, name in available
                                 if normalized in _normalized_text(name))
        if not local_matches:
            unmatched.append(raw)
            continue
        for code, name in local_matches:
            matched_codes.add(code)
            matched_names.add(name)
    if requested and not matched_codes:
        examples = "、".join(name for _code, name in available[:12])
        raise ValueError(f"未找到板块：{'、'.join(unmatched)}。可用示例：{examples}")
    return {"codes": sorted(matched_codes), "names": sorted(matched_names),
            "unmatched": unmatched}


def _sector_candidates(sector_codes: list[str]) -> list[dict]:
    if not sector_codes:
        return []
    placeholders = ",".join("?" for _ in sector_codes)
    with closing(connect()) as conn:
        rows = conn.execute(
            f"""SELECT u.symbol,u.name,u.exchange,
                       GROUP_CONCAT(DISTINCT m.sector_name) sector_names
                FROM a_share_universe_assets u
                JOIN a_share_sector_memberships m ON m.symbol=u.symbol
                WHERE m.sector_code IN ({placeholders})
                GROUP BY u.symbol,u.name,u.exchange ORDER BY u.symbol""",
            sector_codes,
        ).fetchall()
    return [{"symbol": str(row["symbol"]), "name": str(row["name"]),
             "sector_names": str(row["sector_names"] or "").split(",")}
            for row in rows]


def _explicit_candidates(stocks: list[str]) -> list[dict]:
    from .stock_compare import resolve_stock
    result = []
    with closing(connect()) as conn:
        for raw in stocks:
            stock = resolve_stock(raw)
            sectors = [str(row[0]) for row in conn.execute(
                """SELECT sector_name FROM a_share_sector_memberships
                   WHERE symbol=? ORDER BY sector_level,sector_code""", (stock["symbol"],)
            )]
            result.append({"symbol": stock["symbol"], "name": stock["name"],
                           "sector_names": sectors})
    return result


def _liquidity(candidates: list[dict]) -> tuple[list[dict], list[str]]:
    from .data_sources.market import TDX_PUBLIC_SERVERS, _tdx_instrument
    from tdxrs import TdxHqClient

    by_symbol = {item["symbol"]: dict(item) for item in candidates}
    errors = []
    for host, port in TDX_PUBLIC_SERVERS:
        client = TdxHqClient()
        try:
            client.connect(host, port, timeout=6)
            values = list(by_symbol)
            for start in range(0, len(values), 60):
                chunk = values[start:start + 60]
                instruments = [_tdx_instrument(symbol) for symbol in chunk]
                rows = client.get_security_quotes([(market, code)
                                                   for code, market, _index, _fund in instruments])
                found = {str(row.get("code")): row for row in rows or []}
                for symbol, instrument in zip(chunk, instruments):
                    row = found.get(instrument[0])
                    if row:
                        by_symbol[symbol]["liquidity_amount"] = float(row.get("amount") or 0)
                        by_symbol[symbol]["quote_price"] = float(row.get("price") or 0)
            if any(item.get("liquidity_amount") for item in by_symbol.values()):
                break
        except Exception as exc:
            errors.append(f"{host}:{port} {exc!r}")
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
    with closing(connect()) as conn:
        for symbol, item in by_symbol.items():
            if item.get("liquidity_amount"):
                continue
            row = conn.execute(
                """SELECT AVG(amount) FROM (
                     SELECT amount FROM market_daily_bars WHERE asset_symbol=?
                     AND amount IS NOT NULL ORDER BY trade_date DESC LIMIT 20)""",
                (symbol,),
            ).fetchone()
            item["liquidity_amount"] = float(row[0] or 0)
    ranked = sorted(
        (item for item in by_symbol.values()
         if not re.search(r"(?:\*?ST|退)", item["name"], re.IGNORECASE)),
        key=lambda item: (float(item.get("liquidity_amount") or 0), item["symbol"]),
        reverse=True,
    )
    for index, item in enumerate(ranked, 1):
        item["liquidity_rank"] = index
    return ranked, errors


def _history_coverage(symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with closing(connect()) as conn:
        rows = conn.execute(
            f"""SELECT asset_symbol,COUNT(DISTINCT trade_date) rows,
                       MIN(trade_date) data_start,MAX(trade_date) data_end
                FROM market_daily_bars WHERE adjust_mode='qfq'
                  AND asset_symbol IN ({placeholders}) GROUP BY asset_symbol""",
            symbols,
        ).fetchall()
    return {str(row["asset_symbol"]): dict(row) for row in rows}


def _public_information_coverage(candidates: list[dict], data_asof: str) -> dict[str, dict]:
    """Summarize available public documents; missing categories stay explicit."""
    output = {}
    management_terms = ("高管", "董事", "监事", "总经理", "辞职", "离任", "聘任", "人事")
    risk_terms = ("风险", "处罚", "诉讼", "立案", "减持", "质押", "退市")
    with closing(connect()) as conn:
        for candidate in candidates:
            symbol, name = str(candidate["symbol"]), str(candidate["name"])
            rows = conn.execute(
                """SELECT document_type,title,source_name,published_at,observed_at,metadata_json
                   FROM source_documents
                   WHERE COALESCE(published_at,observed_at,captured_at) <= ?
                     AND (title LIKE ? OR title LIKE ? OR metadata_json LIKE ?)
                   ORDER BY COALESCE(published_at,observed_at,captured_at) DESC LIMIT 100""",
                (str(data_asof) + "T23:59:59", f"%{symbol}%", f"%{name}%", f"%{symbol}%"),
            ).fetchall()
            docs = [dict(row) for row in rows]
            people = [row for row in docs if any(term in str(row["title"]) for term in management_terms)]
            risks = [row for row in docs if any(term in str(row["title"]) for term in risk_terms)]
            output[symbol] = {
                "document_count": len(docs),
                "latest_document_date": next((row.get("published_at") or row.get("observed_at")
                                                for row in docs), None),
                "source_names": sorted({str(row.get("source_name") or "未标明") for row in docs}),
                "management_change": {
                    "status": "AVAILABLE" if people else "NO_MATCHING_DOCUMENT",
                    "count": len(people),
                    "latest_title": people[0]["title"] if people else None,
                    "latest_date": (people[0].get("published_at") or people[0].get("observed_at"))
                    if people else None,
                },
                "risk_events": {
                    "status": "AVAILABLE" if risks else "NO_MATCHING_DOCUMENT",
                    "count": len(risks),
                    "latest_title": risks[0]["title"] if risks else None,
                    "latest_date": (risks[0].get("published_at") or risks[0].get("observed_at"))
                    if risks else None,
                },
            }
    return output


def _feature_profile(technical: dict, learned: dict, candidate: dict,
                     public_info: dict, data_asof: str) -> dict:
    dimensions = technical.get("fundamental_dimensions", {})
    report_date = technical.get("fundamental_report_date")
    valuation_date = technical.get("fundamental_valuation_date")
    coverage = float(technical.get("fundamental_coverage", 0.0))
    return {
        "asof": data_asof,
        "coverage": round((coverage * 5 + 3 + int(bool(public_info.get("document_count")))) / 9, 4),
        "categories": [
            {"key": "profitability_growth", "plain_label": "公司赚不赚钱、增长快不快",
             "status": "AVAILABLE" if report_date else "MISSING", "source": "公告日财报",
             "asof": report_date, "metrics": {"growth": dimensions.get("growth"),
                                                "quality": dimensions.get("quality")}},
            {"key": "cashflow_debt", "plain_label": "现金够不够、负债重不重",
             "status": "AVAILABLE" if report_date else "MISSING", "source": "公告日财报",
             "asof": report_date, "metrics": {"cashflow": dimensions.get("cashflow"),
                                                "safety": dimensions.get("safety")}},
            {"key": "valuation", "plain_label": "现在的价格贵不贵",
             "status": "AVAILABLE" if valuation_date else "MISSING", "source": "历史估值快照",
             "asof": valuation_date, "metrics": {"value": dimensions.get("value")}},
            {"key": "price_trend", "plain_label": "最近价格走势强不强",
             "status": "AVAILABLE", "source": "通达信前复权日线", "asof": data_asof,
             "metrics": {"momentum": technical.get("momentum"), "trend": technical.get("trend"),
                          "annual_volatility": technical.get("annual_volatility")}},
            {"key": "liquidity", "plain_label": "成交是否活跃、是否容易买卖",
             "status": "AVAILABLE" if candidate.get("liquidity_amount") else "MISSING",
             "source": "最近20日成交额", "asof": data_asof,
             "metrics": {"average_amount": candidate.get("liquidity_amount"),
                          "rank": candidate.get("liquidity_rank")}},
            {"key": "model", "plain_label": "模型估算上涨可能性",
             "status": "AVAILABLE" if learned.get("probability_up") is not None else "MISSING",
             "source": "盘后逐日评测模型", "asof": learned.get("signal_date") or data_asof,
             "metrics": {"probability_up": learned.get("probability_up"),
                          "predicted_return": learned.get("predicted_return"),
                          "confidence": learned.get("confidence")}},
            {"key": "people_events", "plain_label": "管理层变动和重要风险公告",
             "status": "AVAILABLE" if public_info.get("document_count") else "MISSING",
             "source": "公开公告、新闻与研报", "asof": public_info.get("latest_document_date"),
             "metrics": {"management_change": public_info.get("management_change", {}),
                          "risk_events": public_info.get("risk_events", {})}},
        ],
        "missing_rule": "缺失项不填零、不用行业平均数冒充；降低覆盖率并在推荐原因中展示。",
    }


def _cached_daily_symbols(symbols: list[str], data_asof: str) -> set[str]:
    unique = list(dict.fromkeys(str(symbol) for symbol in symbols))
    if not unique:
        return set()
    placeholders = ",".join("?" for _ in unique)
    with closing(connect()) as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT asset_symbol FROM market_daily_bars
                WHERE adjust_mode='qfq' AND trade_date>=?
                  AND asset_symbol IN ({placeholders})""",
            (str(data_asof), *unique),
        ).fetchall()
    return {str(row["asset_symbol"]) for row in rows}


def _refresh_daily(symbols: list[str], cache_complete_asof: str | None = None) -> dict:
    from .continuous_learning import _refresh_market_data_isolated

    unique = list(dict.fromkeys(str(symbol) for symbol in symbols))
    cached = (
        _cached_daily_symbols(unique, cache_complete_asof)
        if cache_complete_asof else set()
    )
    pending = [symbol for symbol in unique if symbol not in cached]
    if not pending:
        return {
            "status": "CACHED", "data_asof": cache_complete_asof,
            "requested_symbols": len(unique), "cached_symbols": len(cached),
            "refreshed_symbols": 0, "batches": 0, "warnings": [],
        }

    batch_size = DAILY_REFRESH_BATCH_SIZE
    batch_results, warnings = [], []
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        try:
            result = _refresh_market_data_isolated(batch, include_minutes=False)
            batch_results.append({
                "symbols": len(batch), "status": result.get("status", "REFRESHED")
            })
        except Exception as exc:
            warnings.append({"symbols": batch, "error": repr(exc)})

    if cache_complete_asof:
        completed = _cached_daily_symbols(unique, cache_complete_asof)
        missing = [symbol for symbol in unique if symbol not in completed]
        if missing:
            detail = warnings[-1]["error"] if warnings else "行情源未返回完整数据"
            raise RuntimeError(
                f"截至 {cache_complete_asof} 仍缺少 {len(missing)} 只股票的日线："
                f"{','.join(missing[:10])}；{detail}"
            )
        refreshed_count = len(completed - cached)
    else:
        if warnings:
            raise RuntimeError(warnings[-1]["error"])
        refreshed_count = len(pending)

    return {
        "status": "REFRESHED_WITH_WARNINGS" if warnings else "REFRESHED",
        "data_asof": cache_complete_asof,
        "requested_symbols": len(unique), "cached_symbols": len(cached),
        "refreshed_symbols": refreshed_count, "batches": len(batch_results),
        "warnings": warnings,
    }


def _prepare_candidates(request: dict) -> dict:
    metadata = sync_a_share_universe()
    sectors = _resolve_sectors(request["sectors"])
    if request["stocks"]:
        source = "EXPLICIT_CANDIDATE_POOL"
        candidates = _explicit_candidates(request["stocks"])
    else:
        source = "SECTOR_AUTO_DISCOVERY"
        candidates = _sector_candidates(sectors["codes"])
    if len(candidates) < request["max_positions"]:
        raise RuntimeError("符合条件的候选股票少于最大持仓数")
    ranked, quote_errors = _liquidity(candidates)
    prefilter = ranked[:min(len(ranked), max(request["max_candidates"] * 2,
                                              request["max_positions"]))]
    refresh = {"status": "SKIPPED"}
    if request["refresh_data"]:
        refresh = _refresh_daily(
            [item["symbol"] for item in prefilter],
            cache_complete_asof=request.get("_daily_cache_asof"),
        )
    coverage = _history_coverage([item["symbol"] for item in prefilter])
    required_days = max(420, int(request.get("backtest_window_years", 3)) * 252)
    eligible = [item for item in prefilter
                if coverage.get(item["symbol"], {}).get("rows", 0) >= required_days]
    selected = eligible[:request["max_candidates"]]
    if len(selected) < request["max_positions"]:
        raise RuntimeError(
            f"只有 {len(selected)} 只候选股具备至少 {required_days} 个交易日历史，无法构建 "
            f"{request['max_positions']} 只持仓的样本外回测"
        )
    for item in selected:
        item["history"] = coverage[item["symbol"]]
    return {"source": source, "metadata": metadata, "sector_resolution": sectors,
            "candidate_count_before_liquidity": len(candidates), "candidates": selected,
            "quote_errors": quote_errors, "market_refresh": refresh}


def _active_prediction_snapshot() -> dict:
    """Return the latest model that already passed the daily promotion gates."""
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT * FROM prediction_model_versions
               WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    if not row or not row["training_end"]:
        raise RuntimeError("尚无完成逐日评测并通过门禁的活动预测模型")
    return {
        "version_key": str(row["version_key"]),
        "training_start": row["training_start"],
        "training_end": str(row["training_end"]),
        "metrics": _load(row["metrics_json"], {}),
        "gate": _load(row["gate_json"], {}),
        "created_at": row["created_at"],
        "activated_at": row["activated_at"],
    }


def _cached_candidate_asof(request: dict, minimum_history_days: int = 420) -> str:
    """Find the newest cached date shared by enough candidates for an immediate ranking."""
    sectors = _resolve_sectors(request["sectors"])
    candidates = (
        _explicit_candidates(request["stocks"])
        if request["stocks"] else _sector_candidates(sectors["codes"])
    )
    candidates = [
        dict(item) for item in candidates
        if not re.search(r"(?:\*?ST|退)", item["name"], re.IGNORECASE)
    ]
    if len(candidates) < request["max_positions"]:
        raise RuntimeError("缓存中的候选股票少于设置的最大持仓数")
    symbols = [item["symbol"] for item in candidates]
    placeholders = ",".join("?" for _ in symbols)
    with closing(connect()) as conn:
        rows = conn.execute(
            f"""SELECT asset_symbol,COUNT(DISTINCT trade_date) rows,
                       MAX(trade_date) data_end
                FROM market_daily_bars WHERE adjust_mode='qfq'
                  AND asset_symbol IN ({placeholders}) GROUP BY asset_symbol""",
            symbols,
        ).fetchall()
    date_counts: dict[str, int] = {}
    for row in rows:
        if int(row["rows"] or 0) < minimum_history_days or not row["data_end"]:
            continue
        key = str(row["data_end"])
        date_counts[key] = date_counts.get(key, 0) + 1
    eligible_dates = [
        key for key, count in date_counts.items()
        if count >= request["max_positions"]
    ]
    if not eligible_dates:
        ready = max(date_counts.values(), default=0)
        raise RuntimeError(
            f"只有 {ready} 只候选股具备至少 {minimum_history_days} 个交易日缓存，"
            f"少于需要比较的 {request['max_positions']} 只"
        )
    return max(eligible_dates)


def _cached_candidate_pool(request: dict, data_asof: str,
                           minimum_history_days: int | None = None) -> dict:
    """Build a candidate pool from persisted bars only, pinned to one model date."""
    sectors = _resolve_sectors(request["sectors"])
    if request["stocks"]:
        source = "EXPLICIT_CANDIDATE_POOL_CACHED"
        candidates = _explicit_candidates(request["stocks"])
    else:
        source = "SECTOR_AUTO_DISCOVERY_CACHED"
        candidates = _sector_candidates(sectors["codes"])
    candidates = [
        dict(item) for item in candidates
        if not re.search(r"(?:\*?ST|退)", item["name"], re.IGNORECASE)
    ]
    if len(candidates) < request["max_positions"]:
        raise RuntimeError("缓存中符合条件的候选股票少于最大持仓数")
    symbols = [item["symbol"] for item in candidates]
    placeholders = ",".join("?" for _ in symbols)
    with closing(connect()) as conn:
        coverage_rows = conn.execute(
            f"""SELECT asset_symbol,COUNT(DISTINCT trade_date) rows,
                       MIN(trade_date) data_start,MAX(trade_date) data_end
                FROM market_daily_bars WHERE adjust_mode='qfq' AND trade_date<=?
                  AND asset_symbol IN ({placeholders}) GROUP BY asset_symbol""",
            (data_asof, *symbols),
        ).fetchall()
        liquidity_rows = conn.execute(
            f"""SELECT asset_symbol,AVG(amount) liquidity_amount
                FROM (
                  SELECT asset_symbol,amount,
                         ROW_NUMBER() OVER (
                           PARTITION BY asset_symbol ORDER BY trade_date DESC
                         ) AS recent_rank
                  FROM market_daily_bars
                  WHERE adjust_mode='qfq' AND trade_date<=?
                    AND asset_symbol IN ({placeholders})
                ) WHERE recent_rank<=20 GROUP BY asset_symbol""",
            (data_asof, *symbols),
        ).fetchall()
        metadata_asof = conn.execute(
            "SELECT MAX(source_asof) FROM a_share_universe_assets"
        ).fetchone()[0]
    coverage = {str(row["asset_symbol"]): dict(row) for row in coverage_rows}
    liquidity = {
        str(row["asset_symbol"]): float(row["liquidity_amount"] or 0.0)
        for row in liquidity_rows
    }
    required_days = (
        int(minimum_history_days) if minimum_history_days is not None
        else max(420, int(request.get("backtest_window_years", 3)) * 252)
    )
    eligible = []
    for item in candidates:
        history = coverage.get(item["symbol"], {})
        if int(history.get("rows") or 0) < required_days or str(history.get("data_end")) != data_asof:
            continue
        item["history"] = history
        item["liquidity_amount"] = liquidity.get(item["symbol"], 0.0)
        eligible.append(item)
    eligible.sort(
        key=lambda item: (float(item["liquidity_amount"]), item["symbol"]),
        reverse=True,
    )
    selected = eligible[:request["max_candidates"]]
    if len(selected) < request["max_positions"]:
        raise RuntimeError(
            f"截至 {data_asof} 只有 {len(selected)} 只候选股具备完整缓存，无法构建 "
            f"{request['max_positions']} 只持仓的快照推荐"
        )
    for index, item in enumerate(selected, 1):
        item["liquidity_rank"] = index
    return {
        "source": source,
        "metadata": {"status": "CACHED", "source_asof": metadata_asof},
        "sector_resolution": sectors,
        "candidate_count_before_liquidity": len(candidates),
        "candidates": selected,
        "market_refresh": {"status": "SKIPPED", "reason": "snapshot_inference"},
    }


def _rule_snapshot_watchlist(ranking: list[dict], limit: int) -> list[dict]:
    """Return an observation list without inventing model probabilities or allocations."""
    ordered = sorted(
        ranking,
        key=lambda item: (
            bool(item.get("eligible")),
            float(item.get("composite_score") or -999.0),
            str(item.get("symbol") or ""),
        ),
        reverse=True,
    )
    output = []
    for item in ordered[:max(1, int(limit))]:
        fundamental_available = item.get("fundamental_score") is not None
        rule_pass = bool(item.get("eligible") and fundamental_available)
        blockers = list(item.get("rejection_reasons", []))
        if not fundamental_available:
            blockers.append("财务数据不足，当前仅按行情因子排序")
        output.append({
            "symbol": item["symbol"],
            "name": item["name"],
            "status": (
                "RULE_PASS" if rule_pass else
                "DATA_GAP" if not fundamental_available else "OBSERVE"
            ),
            "status_label": (
                "通过基础规则" if rule_pass else
                "行情靠前，财务待补" if not fundamental_available else "继续观察"
            ),
            "reference_price": item.get("reference_price"),
            "composite_score": item.get("composite_score"),
            "momentum": item.get("momentum"),
            "trend": item.get("trend"),
            "annual_volatility": item.get("annual_volatility"),
            "fundamental_score": item.get("fundamental_score"),
            "fundamental_coverage": item.get("fundamental_coverage"),
            "blockers": blockers,
            "observation_only": True,
            "formal_position": False,
            "shares": None,
            "weight": None,
        })
    return output


def _weights_with_minimum_lots(raw_weights: list[float],
                               minimum_weights: list[float],
                               exposure: float,
                               max_weight: float) -> list[float]:
    """Distribute exposure while preserving one affordable lot per selection."""
    weights = list(minimum_weights)
    remaining = max(0.0, exposure - sum(weights))
    headrooms = [max(0.0, max_weight - weight) for weight in weights]
    while remaining > 1e-9:
        active = [index for index, room in enumerate(headrooms)
                  if room > 1e-9 and raw_weights[index] > 0]
        if not active:
            break
        total_raw = sum(raw_weights[index] for index in active)
        additions = {
            index: min(headrooms[index], remaining * raw_weights[index] / total_raw)
            for index in active
        }
        applied = sum(additions.values())
        if applied <= 1e-9:
            break
        for index, addition in additions.items():
            weights[index] += addition
            headrooms[index] -= addition
        remaining -= applied
    return weights


def _rule_snapshot_allocations(watchlist: list[dict], request: dict,
                               parameters: dict) -> list[dict]:
    """Create an explicit lot-aware research allocation from current cached rules."""
    capital = float(request["capital"])
    lot_size = int(request.get("execution", {}).get("lot_size", 100))
    max_positions = max(1, int(request["max_positions"]))
    profile = str(request.get("risk_profile") or "balanced")
    if profile == "auto":
        profile = "balanced"
    max_drawdown = max(1.0, float(request["max_drawdown_pct"])) / 100.0
    base_exposure = {"aggressive": 0.95, "balanced": 0.90,
                     "conservative": 0.82}.get(profile, 0.90)
    exposure = base_exposure * min(1.0, max(0.60, max_drawdown / 0.15))
    max_weight = min(exposure, {
        "aggressive": 0.45, "balanced": 0.35, "conservative": 0.25,
    }.get(profile, 0.35))
    viable = []
    for index, item in enumerate(watchlist):
        forecast = item.get("timeframe_forecast") or {}
        summary = forecast.get("summary") or {}
        price = float(item.get("reference_price") or 0.0)
        has_scenario_curve = len(forecast.get("forecast_curve") or []) >= 2
        has_supported_scenario = (
            str(forecast.get("validation_status") or "").startswith(
                "WALK_FORWARD_CALIBRATED"
            )
            or forecast.get("validation_status") == "BASELINE_SCENARIO_ONLY"
        )
        if (
            not has_scenario_curve
            or not has_supported_scenario
            or summary.get("action") in {"SELL_REVIEW", "REDUCE_REVIEW"}
            or price <= 0
            or price * lot_size > capital * max_weight
        ):
            continue
        viable.append({"item": item, "rank": index})
    viable.sort(key=lambda value: (
        (value["item"].get("timeframe_forecast") or {}).get("summary", {}).get("action") == "BUY_WATCH",
        float((value["item"].get("timeframe_forecast") or {}).get("summary", {}).get("direction_score") or 0.0),
        float(value["item"].get("composite_score") or -999.0),
        -value["rank"],
    ), reverse=True)
    selected = []
    minimum_weights = []
    reserved_exposure = 0.0
    for value in viable:
        if len(selected) >= max_positions:
            break
        minimum_weight = (
            float(value["item"]["reference_price"]) * lot_size / capital
        )
        if reserved_exposure + minimum_weight > exposure + 1e-9:
            continue
        selected.append(value)
        minimum_weights.append(minimum_weight)
        reserved_exposure += minimum_weight
    if not selected:
        return []

    composites = [float(value["item"].get("composite_score") or 0.0) for value in selected]
    low, high = min(composites), max(composites)
    raw_weights = []
    for value, composite in zip(selected, composites):
        item = value["item"]
        summary = (item.get("timeframe_forecast") or {}).get("summary") or {}
        normalized_score = 0.5 if high <= low else (composite - low) / (high - low)
        action_boost = 1.35 if summary.get("action") == "BUY_WATCH" else 1.0
        direction_boost = 1.0 + max(-0.4, min(0.8, float(summary.get("direction_score") or 0.0)))
        volatility = max(0.10, float(item.get("annual_volatility") or 0.35))
        raw_weights.append(
            action_boost * direction_boost * (0.75 + normalized_score) / math.sqrt(volatility)
        )
    target_weights = _weights_with_minimum_lots(
        raw_weights, minimum_weights, exposure, max_weight,
    )
    stop_loss = float(request["stop_loss_pct"]) / 100.0
    take_profit = float(request["take_profit_pct"]) / 100.0
    allocations = []
    for value, target_weight in zip(selected, target_weights):
        item = value["item"]
        reference_price = float(item["reference_price"])
        shares = math.floor(
            capital * target_weight / reference_price / lot_size
        ) * lot_size
        if shares <= 0:
            continue
        amount = shares * reference_price
        allocations.append({
            **item,
            "status": "RESEARCH_ALLOCATION",
            "status_label": "研究分配建议",
            "target_weight": round(target_weight, 6),
            "weight": round(amount / capital, 6),
            "shares": int(shares),
            "amount": round(amount, 2),
            "stop_price": round(reference_price * (1.0 - stop_loss), 4),
            "take_profit_price": round(reference_price * (1.0 + take_profit), 4),
            "planned_loss_at_stop": round(amount * stop_loss, 2),
            "research_allocation": True,
            "observation_only": False,
            "formal_position": False,
            "selection_rule": (
                "排除经校准卖出/减仓风险后，按因子、经验证方向和波动分配并按整手取整；"
                "历史基准情景只参与金额范围展示，不提供方向加分"
            ),
        })
    return allocations


def _piecewise_forecast_quantile(point: dict, base_price: float,
                                 probability: float) -> float:
    returns = {
        key: float(point[key]) / base_price - 1.0
        for key in ("p10", "p25", "p50", "p75", "p90")
    }
    anchors = [
        (0.01, max(-0.95, returns["p10"] - 0.60 * (returns["p25"] - returns["p10"]))),
        (0.10, returns["p10"]), (0.25, returns["p25"]),
        (0.50, returns["p50"]), (0.75, returns["p75"]),
        (0.90, returns["p90"]),
        (0.99, returns["p90"] + 0.60 * (returns["p90"] - returns["p75"])),
    ]
    probability = max(0.01, min(0.99, probability))
    for (left_p, left_v), (right_p, right_v) in zip(anchors, anchors[1:]):
        if probability <= right_p:
            fraction = (probability - left_p) / (right_p - left_p)
            return left_v + fraction * (right_v - left_v)
    return anchors[-1][1]


def _historical_copula(allocations: list[dict]) -> dict:
    symbols = [str(item.get("symbol") or "") for item in allocations]
    if not symbols or any(not symbol for symbol in symbols):
        return {"status": "UNAVAILABLE", "rows": [], "common_days": 0}
    data_asof = min(
        str((item.get("timeframe_forecast") or {}).get("data_asof") or "9999-12-31")
        for item in allocations
    )
    returns_by_symbol: dict[str, dict[str, float]] = {}
    with closing(connect()) as conn:
        for symbol in symbols:
            payload = build_chart_series(conn, symbol, "1d")
            rows = [
                row for row in payload.get("series", [])
                if str(row.get("time") or "")[:10] <= data_asof
            ][-505:]
            returns_by_symbol[symbol] = {
                str(rows[index].get("time") or "")[:10]:
                float(rows[index]["close"]) / float(rows[index - 1]["close"]) - 1.0
                for index in range(1, len(rows))
                if float(rows[index - 1].get("close") or 0.0) > 0
                and float(rows[index].get("close") or 0.0) > 0
            }
    common_dates = set(returns_by_symbol[symbols[0]])
    for symbol in symbols[1:]:
        common_dates &= set(returns_by_symbol[symbol])
    dates = sorted(common_dates)
    if len(dates) < 60:
        return {"status": "UNAVAILABLE", "rows": [], "common_days": len(dates)}
    ordered = {
        symbol: sorted(returns_by_symbol[symbol][date] for date in dates)
        for symbol in symbols
    }
    rank_maps = {
        symbol: {
            date: (
                sum(value < returns_by_symbol[symbol][date] for value in ordered[symbol])
                + 0.5 * sum(value == returns_by_symbol[symbol][date] for value in ordered[symbol])
            ) / len(dates)
            for date in dates
        }
        for symbol in symbols
    }
    return {
        "status": "AVAILABLE", "common_days": len(dates),
        "rows": [[rank_maps[symbol][date] for symbol in symbols] for date in dates],
        "symbols": symbols, "data_asof": data_asof,
        "method": "empirical_rank_copula_from_aligned_daily_returns",
    }


def _rule_snapshot_portfolio_forecast(allocations: list[dict],
                                      capital: float,
                                      target_return_pct: float = 0.0) -> dict:
    invested = round(sum(float(item.get("amount") or 0.0) for item in allocations), 2)
    cash = round(max(0.0, float(capital) - invested), 2)
    if not allocations:
        return {
            "status": "UNAVAILABLE", "capital": round(float(capital), 2),
            "invested_amount": 0.0, "cash_amount": round(float(capital), 2),
            "curve": [], "reason": "当前没有通过即时风险复核且可按整手买入的股票",
        }
    modeled_allocations = []
    baseline_allocations = []
    excluded_allocations = []
    for item in allocations:
        forecast = item.get("timeframe_forecast") or {}
        curve = forecast.get("forecast_curve") or []
        validation_status = str(forecast.get("validation_status") or "")
        calibrated = validation_status.startswith(
            "WALK_FORWARD_CALIBRATED"
        )
        baseline_only = validation_status == "BASELINE_SCENARIO_ONLY"
        if (calibrated or baseline_only) and len(curve) >= 2:
            modeled_allocations.append(item)
            if baseline_only or any(
                point.get("basis") == "HISTORICAL_BASELINE_REFERENCE"
                for point in curve
            ):
                baseline_allocations.append({
                    "symbol": item.get("symbol"), "name": item.get("name"),
                    "amount": round(float(item.get("amount") or 0.0), 2),
                    "validation_status": validation_status,
                })
        else:
            excluded_allocations.append({
                "symbol": item.get("symbol"), "name": item.get("name"),
                "amount": round(float(item.get("amount") or 0.0), 2),
                "reason": forecast.get("horizon_reason") or "没有足够历史生成概率情景",
            })
    modeled_invested = round(sum(
        float(item.get("amount") or 0.0) for item in modeled_allocations
    ), 2)
    static_amount = round(max(0.0, float(capital) - modeled_invested), 2)
    if not modeled_allocations:
        return {
            "status": "UNAVAILABLE", "capital": round(float(capital), 2),
            "invested_amount": invested, "modeled_invested_amount": 0.0,
            "cash_amount": cash, "static_amount": round(float(capital), 2),
            "curve": [], "excluded_allocations": excluded_allocations,
            "reason": "建议股票都缺少足够历史生成概率情景，因此不画组合未来线",
        }
    point_maps = []
    for item in modeled_allocations:
        curve = (item.get("timeframe_forecast") or {}).get("forecast_curve") or []
        point_maps.append({int(point["trading_day"]): point for point in curve})
    common_days = set(point_maps[0])
    for points in point_maps[1:]:
        common_days &= set(points)
    curve = []
    probability_by_day = []
    calibrated_days = min(
        int((item.get("timeframe_forecast") or {}).get(
            "validated_horizon_trading_days", 0
        ))
        for item in modeled_allocations
    )
    reference_effective_samples = [
        int(((item.get("timeframe_forecast") or {}).get(
            "requested_forecast", {}
        ) or {}).get("effective_sample_count") or 0)
        for item in modeled_allocations
        if any(
            point.get("basis") == "HISTORICAL_BASELINE_REFERENCE"
            for point in ((item.get("timeframe_forecast") or {}).get(
                "forecast_curve", []
            ) or [])
        )
    ]
    minimum_reference_effective = min(reference_effective_samples, default=0)
    endpoint_outcomes = None
    final_common_day = max(common_days) if common_days else 0
    copula = _historical_copula(modeled_allocations)
    rng = random.Random(int(hashlib.sha256(
        "|".join(str(item.get("symbol") or "") for item in modeled_allocations).encode("utf-8")
    ).hexdigest()[:16], 16))
    for day in sorted(common_days):
        row = {"trading_day": day}
        if copula["status"] == "AVAILABLE":
            outcomes = []
            for _ in range(2000):
                uniforms = copula["rows"][rng.randrange(len(copula["rows"]))]
                total = static_amount
                for index, (allocation, points) in enumerate(zip(modeled_allocations, point_maps)):
                    base_price = float(allocation["reference_price"])
                    marginal_return = _piecewise_forecast_quantile(
                        points[day], base_price, uniforms[index],
                    )
                    total += float(allocation["amount"]) * (1.0 + marginal_return)
                outcomes.append(total)
            if day == final_common_day:
                endpoint_outcomes = outcomes
            probability_by_day.append({
                "trading_day": day,
                "profit": round(
                    sum(value > float(capital) for value in outcomes) / len(outcomes), 4
                ),
                "target": round(
                    sum(value >= float(capital) * (
                        1.0 + float(target_return_pct) / 100.0
                    ) for value in outcomes) / len(outcomes), 4
                ),
                "sample_count": len(outcomes),
            })
            for quantile, probability in (("p10", 0.10), ("p25", 0.25), ("p50", 0.50),
                                          ("p75", 0.75), ("p90", 0.90)):
                row[quantile] = round(_percentile(outcomes, probability), 2)
        else:
            for quantile in ("p10", "p25", "p50", "p75", "p90"):
                total = static_amount
                for allocation, points in zip(modeled_allocations, point_maps):
                    base_price = float(allocation["reference_price"])
                    total += float(allocation["amount"]) * float(points[day][quantile]) / base_price
                row[quantile] = round(total, 2)
        curve.append(row)
    if len(curve) < 2:
        return {
            "status": "UNAVAILABLE", "capital": round(float(capital), 2),
            "invested_amount": invested, "modeled_invested_amount": modeled_invested,
            "cash_amount": cash, "static_amount": static_amount, "curve": [],
            "excluded_allocations": excluded_allocations,
            "reason": "推荐股票没有共同的未来预测周期",
        }
    endpoint = curve[-1]
    endpoint_probabilities = {
        "profit": None, "target": None,
        "target_return_pct": round(float(target_return_pct), 4),
        "sample_count": 0, "method": "UNAVAILABLE",
    }
    endpoint_probability_reliable = bool(
        endpoint_outcomes
        and (not baseline_allocations or minimum_reference_effective >= 10)
    )
    if endpoint_probability_reliable:
        target_amount = float(capital) * (1.0 + float(target_return_pct) / 100.0)
        endpoint_probabilities = {
            "profit": round(
                sum(value > float(capital) for value in endpoint_outcomes)
                / len(endpoint_outcomes), 4,
            ),
            "target": round(
                sum(value >= target_amount for value in endpoint_outcomes)
                / len(endpoint_outcomes), 4,
            ),
            "target_return_pct": round(float(target_return_pct), 4),
            "sample_count": len(endpoint_outcomes),
            "method": "same_empirical_copula_scenarios_as_chart",
        }
    elif endpoint_outcomes:
        endpoint_probabilities.update({
            "reliability_status": "INSUFFICIENT_INDEPENDENT_HISTORY",
            "minimum_independent_periods": minimum_reference_effective,
            "reason": (
                f"用户期限的历史基准只有约 {minimum_reference_effective} 个独立周期，"
                "不能把重采样次数冒充独立证据"
            ),
        })
    requested_days = min(
        int((item.get("timeframe_forecast") or {}).get(
            "requested_horizon_trading_days", endpoint["trading_day"]
        ))
        for item in modeled_allocations
    )
    scenario_days = int(endpoint["trading_day"])
    calibrated_probability = next((
        item for item in reversed(probability_by_day)
        if int(item["trading_day"]) <= calibrated_days
    ), None)
    horizon_status = (
        "REFERENCE_FULL" if baseline_allocations and scenario_days >= requested_days else
        "REFERENCE_PARTIAL" if baseline_allocations else
        "FULL" if scenario_days >= requested_days else "PARTIAL"
    )
    return {
        "status": "AVAILABLE",
        "capital": round(float(capital), 2),
        "invested_amount": invested,
        "modeled_invested_amount": modeled_invested,
        "cash_amount": cash,
        "static_amount": static_amount,
        "invested_weight": round(invested / float(capital), 6),
        "cash_weight": round(cash / float(capital), 6),
        "horizon_trading_days": scenario_days,
        "validated_horizon_trading_days": calibrated_days,
        "requested_horizon_trading_days": requested_days,
        "horizon_status": horizon_status,
        "horizon_reason": (
            (
                f"请求未来 {requested_days} 个交易日；组合情景展示到 {scenario_days} 日，"
                f"其中共同严格校准到 {calibrated_days} 日，其余为历史基准情景"
            ) if baseline_allocations else
            None if scenario_days >= requested_days else
            f"请求未来 {requested_days} 个交易日；组合只展示共同通过校准的前 {scenario_days} 个交易日"
        ),
        "curve": curve,
        "excluded_allocations": excluded_allocations,
        "baseline_allocations": baseline_allocations,
        "endpoint": {
            key: round(float(endpoint[key]), 2) for key in ("p10", "p25", "p50", "p75", "p90")
        },
        "endpoint_returns": {
            key: round(float(endpoint[key]) / float(capital) - 1.0, 6)
            for key in ("p10", "p25", "p50", "p75", "p90")
        },
        "endpoint_probabilities": endpoint_probabilities,
        "calibrated_horizon_probabilities": calibrated_probability,
        "method": (
            "经验秩Copula保留股票同期相关性，再聚合逐股校准区间或明确标注的历史基准情景；现金收益按0计"
            if copula["status"] == "AVAILABLE" else
            "相关性样本不足，退回逐股同分位情景汇总；现金收益按0计"
        ),
        "correlation": {
            "status": copula["status"],
            "method": copula.get("method"),
            "common_daily_samples": copula.get("common_days", 0),
            "simulation_count": 2000 if copula["status"] == "AVAILABLE" else 0,
        },
        "validation_status": (
            "MIXED_CALIBRATED_AND_HISTORICAL_BASELINE_PORTFOLIO_NOT_BACKTESTED"
            if baseline_allocations else
            "MARGINALS_WALK_FORWARD_CALIBRATED_PORTFOLIO_NOT_BACKTESTED"
        ),
    }


def _attach_timeframe_forecasts(items: list[dict], data_asof: str,
                                horizon_months: int,
                                risk_profile: str) -> list[dict]:
    """Attach the same K-line forecast contract to every recommendation mode."""
    if not items:
        return items
    profile = risk_profile if risk_profile in PROFILE_LABELS else "balanced"
    with closing(connect()) as conn:
        for item in items:
            forecast = build_timeframe_forecast(
                conn, item["symbol"], data_asof, horizon_months, profile,
            )
            item["timeframe_forecast"] = forecast
            summary = forecast.get("summary") or {}
            item["action_signal"] = summary.get("action")
            item["action_label"] = summary.get("action_label")
            item["sell_conclusion"] = summary.get("sell_review") or {}
    return items


def _ensure_recommendation_forecasts(result: dict) -> dict:
    """Add forecast fields to current and legacy published recommendation payloads."""
    recommendation = result.get("recommendation") or {}
    request = result.get("request") or {}
    existing = recommendation.get("forecast_allocations") or []
    if (
        recommendation.get("portfolio_forecast")
        and existing
        and all(item.get("timeframe_forecast") for item in existing)
    ):
        return result

    allocations = (
        recommendation.get("positions")
        or recommendation.get("research_recommendations")
        or recommendation.get("research_allocations")
        or []
    )
    data_asof = str(
        recommendation.get("data_asof")
        or result.get("data", {}).get("end")
        or ""
    )
    if not allocations or not data_asof:
        recommendation["forecast_allocations"] = allocations
        recommendation["portfolio_forecast"] = _rule_snapshot_portfolio_forecast(
            allocations, float(request.get("capital") or 0.0),
            float(request.get("target_return_pct") or 0.0),
        )
        return result

    profile = str(recommendation.get("profile") or request.get("risk_profile") or "balanced")
    horizon_months = int(request.get("horizon_months") or 12)
    _attach_timeframe_forecasts(
        allocations, data_asof, horizon_months, profile,
    )
    recommendation["forecast_allocations"] = allocations

    enriched_by_symbol = {
        str(item.get("symbol") or ""): item for item in allocations
    }
    for key in (
        "positions", "research_recommendations", "research_watchlist",
        "research_allocations",
    ):
        for item in recommendation.get(key) or []:
            enriched = enriched_by_symbol.get(str(item.get("symbol") or ""))
            if not enriched or enriched is item:
                continue
            for field in (
                "timeframe_forecast", "action_signal", "action_label",
                "sell_conclusion",
            ):
                if field in enriched:
                    item[field] = enriched[field]

    recommendation["portfolio_forecast"] = _rule_snapshot_portfolio_forecast(
        allocations, float(request.get("capital") or 0.0),
        float(request.get("target_return_pct") or 0.0),
    )
    return result


def _cached_rule_snapshot_result(mandate: dict,
                                 fallback_reason: str | None = None) -> dict:
    """Rank cached candidates immediately when no validated model/template is available."""
    request = dict(mandate["input"])
    data_asof = _cached_candidate_asof(
        request, minimum_history_days=RULE_SNAPSHOT_MINIMUM_HISTORY_DAYS
    )
    prepared = _cached_candidate_pool(
        request, data_asof,
        minimum_history_days=RULE_SNAPSHOT_MINIMUM_HISTORY_DAYS,
    )
    candidates = prepared["candidates"]
    profile = request["risk_profile"] if request["risk_profile"] != "auto" else "balanced"
    portfolio_mandate = {
        "name": request["name"], "capital": request["capital"],
        "horizon_months": request["horizon_months"],
        "target_return_pct": request["target_return_pct"],
        "max_drawdown_pct": request["max_drawdown_pct"],
        "stop_loss_pct": request["stop_loss_pct"],
        "take_profit_pct": request["take_profit_pct"],
        "trailing_stop_pct": request["trailing_stop_pct"],
        "sectors": request["sectors"],
        "universe": [{"input": item["symbol"], "symbol": item["symbol"],
                      "name": item["name"]} for item in candidates],
        "max_positions": request["max_positions"],
        "take_profit_mode": request["take_profit_mode"],
        "max_iterations": request["max_iterations"],
        "backtest_window_years": request["backtest_window_years"],
        "strategy_style": request["strategy_style"],
        "preference_weights": request["preference_weights"],
        "preference_version": request["preference_version"],
        "execution": request["execution"],
    }
    parameters = _candidate_parameters(profile, portfolio_mandate, 0)
    parameters.pop("prediction_floor", None)
    parameters["prediction_weight"] = 0.0
    parameters["prediction_status"] = "UNAVAILABLE_NOT_VALIDATED"
    if request["strategy_style"] == "auto":
        parameters.update({
            "strategy_style": "auto",
            "strategy_style_label": "五因子均衡",
            "strategy_style_description": "未运行回测时不自动挑选细分策略",
            "style_multipliers": {},
        })
    strategy = {
        "profile": profile,
        "label": PROFILE_LABELS[profile],
        "parameters": parameters,
    }
    current = _current_portfolio(
        portfolio_mandate, strategy, candidates, data_asof=data_asof,
        use_prediction_model=False, allocate_positions=False,
        minimum_history_days=RULE_SNAPSHOT_MINIMUM_HISTORY_DAYS,
    )
    if current["data_asof"] != data_asof:
        raise RuntimeError("候选股票的共同缓存日期不一致")
    ranking = _display_candidate_ranking(current["candidate_ranking"], [])
    for item in ranking:
        item["rule_pass"] = bool(
            item.get("eligible") and item.get("fundamental_score") is not None
        )
    forecast_limit = min(len(ranking), max(request["max_positions"] + 4, request["max_positions"]))
    watchlist = _rule_snapshot_watchlist(ranking, forecast_limit)
    _attach_timeframe_forecasts(
        watchlist, data_asof, request["horizon_months"], profile,
    )
    for item in watchlist:
        if (
            item.get("fundamental_score") is None
            and item["action_signal"] == "BUY_WATCH"
        ):
            item["action_label"] = "走势偏多，财务待补后再复核买入"
    allocations = _rule_snapshot_allocations(watchlist, request, parameters)
    portfolio_forecast = _rule_snapshot_portfolio_forecast(
        allocations, float(request["capital"]),
        float(request["target_return_pct"]),
    )
    passed = sum(bool(item.get("rule_pass")) for item in ranking)
    common_history_days = int(current["history"]["rows"])
    compressed_history = common_history_days < RULE_SNAPSHOT_STANDARD_HISTORY_DAYS
    history_window = {
        "mode": "COMPRESSED" if compressed_history else "STANDARD",
        "common_trading_days": common_history_days,
        "standard_days": RULE_SNAPSHOT_STANDARD_HISTORY_DAYS,
        "minimum_usable_days": RULE_SNAPSHOT_MINIMUM_HISTORY_DAYS,
        "confidence": "LOWER" if compressed_history else "STANDARD",
        "description": (
            f"共同历史只有 {common_history_days} 个交易日，已自动压缩即时计算窗口；"
            "结果可用于当前排序，但可信度低于 420 日标准窗口。"
            if compressed_history else
            f"共同历史达到 {common_history_days} 个交易日，满足 420 日标准窗口。"
        ),
    }
    recommendation = {
        "profile": profile,
        "profile_label": PROFILE_LABELS[profile],
        "selection_reason": (
            "用当前缓存的走势、财务、成交活跃度和稳定性即时排序，"
            "通过门禁的期限显示校准区间，其余期限明确降级为历史基准情景"
        ),
        "decision_status": "RULE_SNAPSHOT",
        "decision_label": (
            f"即时研究分配：{len(allocations)} 只股票；"
            "金额分配策略尚未完成组合级回测，图线区分样本外校准与历史基准情景"
        ),
        "model_status": "UNAVAILABLE_NOT_VALIDATED",
        "model_version": None,
        "data_asof": current["data_asof"],
        "positions": [],
        "rules": current["rules"],
        "candidate_ranking": ranking,
        "research_recommendations": [],
        "research_watchlist": watchlist,
        "research_allocations": allocations,
        "portfolio_forecast": portfolio_forecast,
        "expectation": {"status": "UNAVAILABLE_NOT_VALIDATED"},
        "holdout_metrics": {"status": "UNAVAILABLE_NOT_VALIDATED"},
        "parameters": parameters,
        "credibility": {
            "validation_status": (
                "RULE_ONLY_COMPRESSED_HISTORY_NOT_BACKTESTED"
                if compressed_history else "RULE_ONLY_NOT_BACKTESTED"
            ),
            "history_window": history_window,
            "model": {}, "strategy": {}, "walk_forward_history": [],
            "data_ranges": {
                "start": min(str(item["history"]["data_start"]) for item in candidates),
                "end": data_asof,
                "rows": min(int(item["history"]["rows"]) for item in candidates),
            },
            "stocks": [{
                "symbol": item["symbol"], "name": item["name"],
                "included": True, "reason": "仅用于即时规则排序"
            } for item in candidates],
            "warning": (
                history_window["description"] +
                "当前因子排序和资金分配规则尚未完成组合级样本外回测；"
                "逐股未来期限另行执行滚动样本外校准，未通过的期限不展示。"
            ),
        },
        "custom_parameters_applied": True,
        "custom_parameters_validated": False,
        "real_position_required_for_sell_signal": False,
        "real_position_required_for_account_pnl_and_quantity": True,
        "technical_exit_review_without_position": True,
    }
    digest = hashlib.sha256(_dump({
        "mandate": _mandate_identity(request), "data_asof": data_asof,
        "kind": "cached_rule_snapshot", "profile": profile,
        "candidates": [item["symbol"] for item in candidates],
    }).encode("utf-8")).hexdigest()[:20]
    stamp = _now()
    version = {
        "version_key": f"rule-snapshot-{data_asof.replace('-', '')}-{digest[:8]}",
        "status": "RULE_SNAPSHOT", "score": None,
        "gate": {
            "active_model": False, "reference_strategy_active": False,
            "cached_data_only": True, "market_refresh": False,
            "backtest_executed": False, "formal_signal_allowed": False,
            "custom_parameters_validated": False,
            "compressed_history": compressed_history,
        },
        "run_id": None, "created_at": stamp, "activated_at": None,
    }
    return {
        "status": "SUCCESS", "run_key": f"rule-snapshot-{digest}",
        "mandate_key": mandate["mandate_key"], "request": request,
        "candidate_source": prepared["source"],
        "sector_resolution": prepared["sector_resolution"],
        "candidates": candidates, "recommendation": recommendation,
        "strategies": [],
        "data": {
            "end": data_asof,
            "rows": common_history_days,
            "source": "cached_bars_rule_ranking_no_model",
        },
        "inference": {
            "kind": "CACHED_RULE_SNAPSHOT",
            "parameter_status": "RULE_RANKING_PENDING_MODEL_AND_BACKTEST",
            "preference_version": request.get("preference_version"),
            "data_asof": data_asof, "model_version": None,
            "fallback_reason": fallback_reason,
            "history_window": history_window,
            "refresh_data": False, "backtest_executed": False,
        },
        "version": version,
        "data_refresh": {
            "universe": prepared["metadata"],
            "market": prepared["market_refresh"],
            "sentiment": {"status": "SKIPPED", "reason": "rule_snapshot"},
        },
        "limitations": [
            "只使用已缓存的最近完整交易日数据，不联网刷新。",
            *([history_window["description"]] if compressed_history else []),
            "没有活动预测模型时不冒充正式上涨概率；逐股未来区间只展示通过滚动样本外门禁的期限。",
            "研究分配金额和股数按用户本金与整手规则计算，不代表真实持仓或已执行交易。",
            "资金分配规则的组合级样本外回测仍待盘后验证，不阻塞当前多因子排序。",
            "研究结果不连接券商，不执行真实交易。",
        ],
        "research_only": True, "order_execution": False,
    }


def _reference_strategy_template(request: dict, data_asof: str,
                                 candidate_symbols: list[str]) -> dict:
    """Select the closest strategy template already validated by a published version."""
    from .fundamentals import PROFILE_RULES

    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT v.*,m.input_json FROM quant_portfolio_versions v
               JOIN quant_mandates m ON m.id=v.mandate_id
               WHERE v.status='ACTIVE' ORDER BY v.id DESC"""
        ).fetchall()
    requested_sectors = {_normalized_text(item) for item in request["sectors"]}
    requested_candidates = set(candidate_symbols)
    comparable_fields = (
        "horizon_months", "target_return_pct", "max_drawdown_pct",
        "stop_loss_pct", "take_profit_pct", "trailing_stop_pct",
        "max_positions", "take_profit_mode",
    )
    choices = []
    for row in rows:
        result = _load(row["result_json"], {})
        reference_date = str(result.get("data", {}).get("end") or "")
        strategies = result.get("strategies", [])
        if not reference_date or reference_date > data_asof or not strategies:
            continue
        reference_request = _load(row["input_json"], {})
        reference_sectors = {
            _normalized_text(item) for item in reference_request.get("sectors", [])
        }
        union = requested_sectors | reference_sectors
        sector_similarity = (
            len(requested_sectors & reference_sectors) / len(union) if union else 1.0
        )
        reference_candidates = {
            str(item["symbol"]) for item in result.get("candidates", [])
        }
        candidate_overlap = len(requested_candidates & reference_candidates)
        exact_constraints = sum(
            reference_request.get(field) == request.get(field)
            for field in comparable_fields
        )
        choices.append((
            (reference_date, sector_similarity, exact_constraints,
             candidate_overlap, int(row["id"])), row, result,
        ))
    if not choices:
        raise RuntimeError(f"截至 {data_asof} 尚无可引用的已验证策略模板")
    _key, row, result = max(choices, key=lambda item: item[0])
    selected, selection = _choose_strategy(
        result["strategies"], request["risk_profile"],
        request["target_return_pct"] / 100,
    )
    selected = _load(_dump(selected), {})
    params = dict(selected["parameters"])
    profile = selected["profile"]
    fundamental = PROFILE_RULES[profile]
    profile_cap = {"aggressive": 0.60, "balanced": 0.45, "conservative": 0.35}[profile]
    params.update({
        "profile": profile,
        "stop_loss": request["stop_loss_pct"] / 100,
        "take_profit": request["take_profit_pct"] / 100,
        "trailing_stop": request["trailing_stop_pct"] / 100,
        "take_profit_mode": request["take_profit_mode"],
        "max_positions": request["max_positions"],
        "max_position_pct": round(min(profile_cap, 1 / request["max_positions"]), 4),
        "risk_budget": round(min(float(params["risk_budget"]),
                                 request["max_drawdown_pct"] / 100), 4),
        "fundamental_weight": fundamental["fundamental_weight"],
        "fundamental_minimum_score": fundamental["minimum_score"],
        "fundamental_minimum_coverage": fundamental["minimum_coverage"],
        "fundamental_dimension_weights": fundamental["dimensions"],
        "preference_weights": dict(request["preference_weights"]),
        "custom_preferences": True,
    })
    requested_style = request.get("strategy_style", "auto")
    if requested_style in STRATEGY_STYLES:
        style = STRATEGY_STYLES[requested_style]
        params.update({
            "strategy_style": requested_style,
            "strategy_style_label": style["label"],
            "strategy_style_description": style["plain_description"],
            "style_multipliers": dict(style["multipliers"]),
        })
    selected["parameters"] = params
    selected["label"] = PROFILE_LABELS[profile]
    return {
        "strategy": selected,
        "selection": selection,
        "version_key": str(row["version_key"]),
        "strategy_experiment_key": str(
            result.get("strategy_experiment_key") or row["version_key"]
        ),
        "version_score": float(row["score"]),
        "version_gate": _load(row["gate_json"], {}),
        "data_asof": str(result["data"]["end"]),
        "holdout": result["data"].get("final_holdout", {}),
        "data_audit": result.get("data", {}),
        "prediction_audit": result.get("prediction_audit", {}),
        "backtest_stocks": (
            result.get("data", {}).get("universe_audit") or [
                {"symbol": item.get("symbol"), "name": item.get("name"),
                 "included": True, "reason": "参考策略已发布版本的候选池"}
                for item in result.get("candidates", [])
            ]
        ),
        "created_at": row["created_at"],
    }


def _latest_trading_day_snapshot_result(mandate: dict) -> dict:
    """Infer a recommendation from the last fully evaluated trading-day snapshot."""
    request = dict(mandate["input"])
    model = _active_prediction_snapshot()
    data_asof = model["training_end"]
    prepared = _cached_candidate_pool(request, data_asof)
    candidates = prepared["candidates"]
    reference = _reference_strategy_template(
        request, data_asof, [item["symbol"] for item in candidates]
    )
    selected = reference["strategy"]
    portfolio_mandate = {
        "name": request["name"], "capital": request["capital"],
        "horizon_months": request["horizon_months"],
        "target_return_pct": request["target_return_pct"],
        "max_drawdown_pct": request["max_drawdown_pct"],
        "stop_loss_pct": request["stop_loss_pct"],
        "take_profit_pct": request["take_profit_pct"],
        "trailing_stop_pct": request["trailing_stop_pct"],
        "sectors": request["sectors"],
        "universe": [{"input": item["symbol"], "symbol": item["symbol"],
                      "name": item["name"]} for item in candidates],
        "max_positions": request["max_positions"],
        "take_profit_mode": request["take_profit_mode"],
        "max_iterations": request["max_iterations"],
        "backtest_window_years": request["backtest_window_years"],
        "strategy_style": request["strategy_style"],
        "preference_weights": request["preference_weights"],
        "preference_version": request["preference_version"],
        "execution": request["execution"],
    }
    current = _current_portfolio(
        portfolio_mandate, selected, candidates, data_asof=data_asof,
        model_version=model["version_key"],
    )
    if current["data_asof"] != data_asof:
        raise RuntimeError("候选数据日期与活动模型训练日不一致")
    expectation = expected_return_distribution(
        selected["holdout"]["curve"], request["horizon_months"],
        request["target_return_pct"] / 100,
        f"{reference['strategy_experiment_key']}|{selected['profile']}",
    )
    selected["expectation"] = expectation
    _selected, selection = _choose_strategy(
        [selected], selected["profile"], request["target_return_pct"] / 100,
    )
    if not selection["publish_gate_passed"]:
        current["positions"] = []
        current["cash_amount"] = request["capital"]
        current["cash_weight"] = 1.0
    research_recommendations = (
        [] if current["positions"] else
        _research_recommendations(current["candidate_ranking"], portfolio_mandate, selected)
    )
    current["candidate_ranking"] = _display_candidate_ranking(
        current["candidate_ranking"], current["positions"]
    )
    research_watchlist = research_recommendations or _research_watchlist(
        current["candidate_ranking"], request["max_positions"]
    )
    forecast_allocations = current["positions"] or research_watchlist
    _attach_timeframe_forecasts(
        forecast_allocations, data_asof, request["horizon_months"],
        selected["profile"],
    )
    portfolio_forecast = _rule_snapshot_portfolio_forecast(
        current["positions"] or research_recommendations,
        float(request["capital"]),
        float(request["target_return_pct"]),
    )
    if portfolio_forecast["status"] == "AVAILABLE":
        portfolio_forecast.update({
            "source_mode": "LATEST_TRADING_DAY_SNAPSHOT",
        })
    research_names = "、".join(item["name"] for item in research_recommendations)
    decision_label = f"最近交易日模型快照推荐；{selection['label']}"
    if research_names:
        decision_label += f"；试探推荐：{research_names}（非正式持仓）"
    recommendation = {
        "profile": selected["profile"], "profile_label": selected["label"],
        "selection_reason": "最近交易日活动模型与最近发布策略模板的快照推断",
        "decision_status": selection["status"],
        "decision_label": decision_label,
        "target_met": selection["target_met"],
        "risk_constraints_met": selection["risk_constraints_met"],
        "non_regression_met": selection["non_regression_met"],
        "publish_gate_passed": selection["publish_gate_passed"],
        "potential_target_met": selection["potential_target_met"],
        "target_return": selection["target_return"],
        "target_gap": selection["target_gap"],
        "fallback_used": selection["fallback_used"],
        "expectation": expectation,
        "holdout_metrics": selected["holdout"]["metrics"],
        "non_regression_pass": selected["non_regression_pass"],
        "parameters": selected["parameters"],
        "model_version": current["model_version"],
        "data_asof": current["data_asof"],
        "positions": current["positions"],
        "cash_amount": current["cash_amount"],
        "cash_weight": current["cash_weight"],
        "rules": current["rules"],
        "candidate_ranking": current["candidate_ranking"],
        "research_recommendations": research_recommendations,
        "research_watchlist": research_watchlist,
        "forecast_allocations": forecast_allocations,
        "portfolio_forecast": portfolio_forecast,
        "research_cash_weight": round(max(
            0.0, 1.0 - sum(item["weight"] for item in research_recommendations)
        ), 6),
        "research_selection_policy": {
            "formal_gates_unchanged": True,
            "minimum_pillars": 2, "pillar_total": 3,
            "maximum_probability_relaxation": 0.03,
            "lot_aware": True, "order_execution": False,
        },
        "holdout_curve": selected["holdout"]["curve"][::max(
            1, len(selected["holdout"]["curve"]) // 160
        )],
        "reference_backtest": {
            "version_key": reference["version_key"],
            "data_asof": reference["data_asof"],
            "holdout": reference["holdout"],
            "walk_forward_method": reference.get("data_audit", {}).get(
                "half_year_walk_forward", {}
            ),
            "metrics_apply_to": "reference_strategy_template_not_current_candidate_pool",
        },
        "credibility": {
            "validation_status": "REFERENCE_TEMPLATE_ONLY",
            "model": reference.get("prediction_audit") or model.get("metrics", {}),
            "strategy": selected["holdout"]["metrics"],
            "walk_forward_history": selected.get("walk_forward_history", []),
            "data_ranges": {
                **reference.get("data_audit", {}),
                "model_training_start": model.get("training_start"),
                "model_training_end": model.get("training_end"),
                "reference_holdout": reference["holdout"],
            },
            "stocks": reference.get("backtest_stocks", []),
            "current_candidate_stocks": [
                {"symbol": item["symbol"], "name": item["name"],
                 "history": item.get("history", {}), "validated_in_reference": False}
                for item in candidates
            ],
            "warning": "当前股票池尚未按本参数版本重新跑完整回测；正式可信度等待盘后验证。",
        },
        "custom_parameters_applied": True,
        "custom_parameters_validated": False,
    }
    digest = hashlib.sha256(_dump({
        "mandate": _mandate_identity(request), "data_asof": data_asof,
        "model": model["version_key"], "reference": reference["version_key"],
        "profile": selected["profile"], "candidates": [item["symbol"] for item in candidates],
    }).encode("utf-8")).hexdigest()[:20]
    version = {
        "version_key": f"snapshot-{data_asof.replace('-', '')}-{digest[:8]}",
        "status": "SNAPSHOT", "score": reference["version_score"],
        "gate": {
            "active_model": True, "model_training_end": data_asof,
            "reference_strategy_active": True, "cached_data_only": True,
            "market_refresh": False, "backtest_executed": False,
            "custom_parameters_validated": False,
            "preference_version": request.get("preference_version"),
        },
        "run_id": None,
        "created_at": model["activated_at"] or model["created_at"],
        "activated_at": model["activated_at"],
    }
    return {
        "status": "SUCCESS", "run_key": f"snapshot-inference-{digest}",
        "mandate_key": mandate["mandate_key"], "request": request,
        "candidate_source": prepared["source"],
        "sector_resolution": prepared["sector_resolution"],
        "candidates": candidates, "recommendation": recommendation,
        "strategies": [],
        "data": {"end": data_asof, "rows": min(
            int(item["history"]["rows"]) for item in candidates
        ), "source": "cached_bars_pinned_to_active_model_training_end",
                 "reference_holdout": reference["holdout"]},
        "inference": {
            "kind": "LATEST_TRADING_DAY_SNAPSHOT",
            "parameter_status": "CUSTOM_SNAPSHOT_PENDING_POST_CLOSE_VALIDATION",
            "preference_version": request.get("preference_version"),
            "data_asof": data_asof,
            "model_version": model["version_key"],
            "model_training_start": model["training_start"],
            "model_training_end": model["training_end"],
            "model_metrics": model["metrics"],
            "model_gate": model["gate"],
            "reference_strategy_version": reference["version_key"],
            "reference_strategy_data_asof": reference["data_asof"],
            "refresh_data": False, "backtest_executed": False,
        },
        "version": version,
        "data_refresh": {
            "universe": prepared["metadata"],
            "market": prepared["market_refresh"],
            "sentiment": {"status": "SKIPPED", "reason": "snapshot_inference"},
        },
        "limitations": [
            "推荐使用最近一个已完成逐日评测的活动模型与缓存行情，不刷新市场数据。",
            "收益区间和留出指标来自最近发布的参考策略模板，不是当前新候选池的即时回测。",
            "下一次交易日盘后完整回测通过门禁后，系统会以正式 ACTIVE 版本替换快照。",
            "研究组合不连接券商，不执行真实交易。",
        ],
        "research_only": True, "order_execution": False,
    }


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def expected_return_distribution(curve: list[dict], horizon_months: int,
                                 target_return: float, seed_key: str,
                                 simulations: int = 1000) -> dict:
    equities = [float(item["equity"]) for item in curve]
    returns = [equities[index] / equities[index - 1] - 1.0
               for index in range(1, len(equities)) if equities[index - 1] > 0]
    if len(returns) < 20:
        return {"p10": 0.0, "p50": 0.0, "p90": 0.0,
                "probability_target": 0.0, "probability_loss": 1.0,
                "simulations": 0, "horizon_trading_days": 0,
                "forecast_curve": [],
                "method": "insufficient_holdout_returns", "guarantee": False}
    rng = random.Random(int(hashlib.sha256(seed_key.encode("utf-8")).hexdigest()[:16], 16))
    horizon_days = max(21, int(horizon_months) * 21)
    block = min(5, len(returns))
    simulation_count = max(200, min(int(simulations), 5000))
    daily_outcomes = [array("d") for _ in range(horizon_days)]
    for _ in range(simulation_count):
        sampled = []
        while len(sampled) < horizon_days:
            start = rng.randrange(0, len(returns) - block + 1)
            sampled.extend(returns[start:start + block])
        equity = 1.0
        for day, value in enumerate(sampled[:horizon_days]):
            equity *= 1.0 + value
            daily_outcomes[day].append(equity - 1.0)
    forecast_curve = [{
        "trading_day": 0,
        "p10": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p90": 0.0,
    }]
    for day, values in enumerate(daily_outcomes, start=1):
        forecast_curve.append({
            "trading_day": day,
            "p10": round(_percentile(values, 0.10), 6),
            "p25": round(_percentile(values, 0.25), 6),
            "p50": round(_percentile(values, 0.50), 6),
            "p75": round(_percentile(values, 0.75), 6),
            "p90": round(_percentile(values, 0.90), 6),
        })
    outcomes = daily_outcomes[-1]
    return {
        "p10": forecast_curve[-1]["p10"],
        "p50": forecast_curve[-1]["p50"],
        "p90": forecast_curve[-1]["p90"],
        "mean": round(statistics.mean(outcomes), 6),
        "probability_target": round(sum(value >= target_return for value in outcomes) / len(outcomes), 6),
        "probability_loss": round(sum(value < 0 for value in outcomes) / len(outcomes), 6),
        "simulations": len(outcomes),
        "horizon_trading_days": horizon_days,
        "forecast_curve": forecast_curve,
        "method": "five_day_block_bootstrap_of_untouched_holdout_net_returns",
        "guarantee": False,
    }


def _choose_strategy(strategies: list[dict], requested_profile: str,
                     target_return: float) -> tuple[dict, dict]:
    def risk_passes(item: dict) -> bool:
        return bool(item["holdout"]["metrics"]["risk_pass"])

    def is_publishable(item: dict) -> bool:
        return bool(risk_passes(item) and item["non_regression_pass"])

    def closest_key(item: dict) -> tuple[float, float, float]:
        metrics = item["holdout"]["metrics"]
        return (
            abs(float(item["expectation"]["p50"]) - target_return),
            abs(float(metrics["max_drawdown"])),
            -float(metrics["sharpe_ratio"]),
        )

    if requested_profile != "auto":
        selected = next(item for item in strategies if item["profile"] == requested_profile)
    else:
        publishable = [item for item in strategies if is_publishable(item)]
        target_matches = [item for item in publishable
                          if float(item["expectation"]["p50"]) >= target_return]
        if target_matches:
            selected = min(target_matches, key=closest_key)
        elif publishable:
            selected = min(publishable, key=closest_key)
        else:
            risk_safe = [item for item in strategies if risk_passes(item)]
            risk_safe_targets = [
                item for item in risk_safe
                if float(item["expectation"]["p50"]) >= target_return
            ]
            selected = min(
                risk_safe_targets or risk_safe or strategies,
                key=closest_key,
            )

    risk_selected = risk_passes(selected)
    non_regression_selected = bool(selected["non_regression_pass"])
    publishable_selected = is_publishable(selected)
    p50 = float(selected["expectation"]["p50"])
    potential_target_met = p50 >= target_return
    target_met = publishable_selected and potential_target_met
    if target_met:
        status = "TARGET_MET"
        label = "达到目标的风控合格方案"
    elif publishable_selected:
        status = "CLOSEST_FEASIBLE"
        label = "未达到目标，采用风控合格且收益最接近的方案"
    elif risk_selected and potential_target_met:
        status = "RESEARCH_TARGET_MET"
        label = "参考收益达到目标，但未通过非退化门禁，仅列为研究候选"
    elif risk_selected:
        status = "RESEARCH_CLOSEST"
        label = "最大回撤合格，但未通过非退化门禁，仅列为研究候选"
    else:
        status = "NO_RISK_FEASIBLE"
        label = "没有满足最大回撤的方案，仅展示研究候选"
    return selected, {
        "status": status,
        "label": label,
        "target_met": target_met,
        "risk_constraints_met": risk_selected,
        "non_regression_met": non_regression_selected,
        "publish_gate_passed": publishable_selected,
        "potential_target_met": potential_target_met,
        "target_return": round(target_return, 6),
        "estimated_p50": round(p50, 6),
        "target_gap": round(p50 - target_return, 6),
        "fallback_used": not target_met,
    }


def _research_watchlist(ranking: list[dict], limit: int) -> list[dict]:
    """Expose positive model signals without treating them as portfolio positions."""
    positive = [
        item for item in ranking
        if float(item.get("predicted_return") or 0.0) > 0.0
        or float(item.get("probability_up") or 0.0) >= 0.5
    ]
    source = positive or ranking
    return [
        {
            "symbol": item["symbol"],
            "name": item["name"],
            "probability_up": item.get("probability_up"),
            "predicted_return": item.get("predicted_return"),
            "composite_score": item.get("composite_score"),
            "blockers": list(item.get("rejection_reasons", [])),
            "observation_only": True,
        }
        for item in source[:max(1, int(limit))]
    ]


def _research_recommendations(ranking: list[dict], mandate: dict,
                              strategy: dict) -> list[dict]:
    """Select bounded, lot-aware trial ideas without relaxing formal gates."""
    profile = str(strategy.get("profile") or "balanced")
    parameters = strategy.get("parameters", {})
    probability_floor = float(parameters.get("prediction_floor", 0.5))
    soft_floor = max({
        "aggressive": 0.47, "balanced": 0.49, "conservative": 0.51,
    }.get(profile, 0.49), probability_floor - 0.03)
    per_name_cap = {
        "aggressive": 0.20, "balanced": 0.15, "conservative": 0.10,
    }.get(profile, 0.15)
    capital = float(mandate["capital"])
    lot_size = int(mandate.get("execution", {}).get("lot_size", 100))
    stop_loss = float(parameters.get("stop_loss", mandate["stop_loss_pct"] / 100))
    take_profit = float(parameters.get(
        "take_profit", mandate["take_profit_pct"] / 100
    ))

    candidates = []
    for item in ranking:
        reasons = list(item.get("rejection_reasons", []))
        technical_pass = "趋势或动量未通过" not in reasons
        model_pass = "在线模型上涨概率未通过" not in reasons
        fundamental_pass = not any(
            "基本面" in reason or "无可用基本面" in reason for reason in reasons
        )
        passes = {
            "趋势": technical_pass,
            "在线模型": model_pass,
            "公告日基本面": fundamental_pass,
        }
        pass_count = sum(bool(value) for value in passes.values())
        probability = item.get("probability_up")
        reference_price = float(item.get("reference_price") or 0.0)
        minimum_lot_amount = reference_price * lot_size
        affordable = reference_price > 0 and minimum_lot_amount <= capital * per_name_cap
        fundamental_available = item.get("fundamental_score") is not None
        item["research_factor_passes"] = pass_count
        item["research_soft_probability_floor"] = round(soft_floor, 4)
        item["research_affordable"] = affordable
        if not affordable or not fundamental_available or probability is None:
            continue
        candidate = {
            "row": item, "passes": passes, "pass_count": pass_count,
            "probability": float(probability),
        }
        if pass_count >= 2 and float(probability) >= soft_floor:
            candidates.append(candidate)

    candidates.sort(key=lambda value: (
        bool(value["row"].get("eligible")), value["pass_count"],
        float(value["row"].get("composite_score") or -999.0), value["probability"],
    ), reverse=True)
    limit = max(1, int(parameters.get("max_positions", mandate["max_positions"])))
    selected = candidates[:limit]

    output = []
    for value in selected:
        item = value["row"]
        reference_price = float(item["reference_price"])
        shares = math.floor(capital * per_name_cap / reference_price / lot_size) * lot_size
        if shares <= 0:
            continue
        amount = shares * reference_price
        passed = [name for name, passed_value in value["passes"].items() if passed_value]
        failed = [name for name, passed_value in value["passes"].items() if not passed_value]
        item["research_recommended"] = True
        item["research_status"] = "TRIAL_RECOMMENDATION"
        output.append({
            "symbol": item["symbol"], "name": item["name"],
            "status": "TRIAL_RECOMMENDATION", "status_label": "试探推荐",
            "probability_up": item.get("probability_up"),
            "predicted_return": item.get("predicted_return"),
            "composite_score": item.get("composite_score"),
            "reference_price": reference_price, "shares": int(shares),
            "amount": round(amount, 2), "weight": round(amount / capital, 6),
            "stop_price": round(reference_price * (1 - stop_loss), 4),
            "take_profit_price": round(reference_price * (1 + take_profit), 4),
            "planned_loss_at_stop": round(amount * stop_loss, 2),
            "planned_loss_at_stop_pct": round(amount * stop_loss / capital, 6),
            "passed_pillars": passed, "failed_pillars": failed,
            "factor_pass_count": value["pass_count"], "factor_total": 3,
            "soft_probability_floor": round(soft_floor, 4),
            "blockers": list(item.get("rejection_reasons", [])),
            "selection_rule": "三类信号至少通过两类；模型缓冲最多3个百分点；整手可买",
            "observation_only": True, "formal_position": False,
        })
    return output


def _display_candidate_ranking(ranking: list[dict],
                               positions: list[dict] | None = None) -> list[dict]:
    """Place actionable research results first without changing their scores."""
    formal_symbols = {str(item.get("symbol") or "") for item in positions or []}

    def display_key(item: dict) -> tuple[int, float, str]:
        symbol = str(item.get("symbol") or "")
        priority = (
            0 if symbol in formal_symbols else
            1 if item.get("research_recommended") else
            2 if item.get("eligible") else 3
        )
        raw_score = item.get("composite_score")
        score = float(raw_score) if raw_score is not None else -999.0
        return priority, -score, symbol

    return sorted(ranking, key=display_key)


def _current_portfolio(mandate: dict, strategy: dict, candidates: list[dict],
                       data_asof: str | None = None,
                       model_version: str | None = None,
                       use_prediction_model: bool = True,
                       allocate_positions: bool = True,
                       minimum_history_days: int = 420) -> dict:
    from .continuous_learning import latest_symbol_scores

    data = _load_aligned_universe(
        mandate, data_asof=data_asof,
        minimum_history_days=minimum_history_days,
    )
    model = (
        latest_symbol_scores(
            data["symbols"], data_asof=data_asof, model_version=model_version
        ) if use_prediction_model else
        {"model_version": None, "scores": []}
    )
    model_scores = {item["symbol"]: item for item in model["scores"]}
    names = {item["symbol"]: item["name"] for item in candidates}
    metadata = {item["symbol"]: item for item in candidates}
    public_information = _public_information_coverage(candidates, data["data_end"])
    rows = []
    for symbol in data["symbols"]:
        technical = _signal(data, symbol, len(data["dates"]), strategy["parameters"])
        learned = model_scores.get(symbol, {})
        probability = learned.get("probability_up")
        model_pass = (probability is None or
                      probability >= strategy["parameters"].get("prediction_floor", 0.0))
        composite = float(technical.get("score", -1.0))
        rejection_reasons = list(technical.get("rejection_reasons", []))
        if not model_pass and "在线模型上涨概率未通过" not in rejection_reasons:
            rejection_reasons.append("在线模型上涨概率未通过")
        rows.append({
            "symbol": symbol, "name": names.get(symbol, symbol),
            "eligible": bool(technical.get("eligible") and model_pass),
            "composite_score": round(composite, 8),
            "momentum": round(float(technical.get("momentum", 0.0)), 8),
            "trend": round(float(technical.get("trend", 0.0)), 8),
            "annual_volatility": round(float(technical.get("annual_volatility", 0.0)), 8),
            "fundamental_score": technical.get("fundamental_score"),
            "fundamental_coverage": technical.get("fundamental_coverage", 0.0),
            "fundamental_dimensions": technical.get("fundamental_dimensions", {}),
            "fundamental_report_date": technical.get("fundamental_report_date"),
            "fundamental_notice_date": technical.get("fundamental_notice_date"),
            "fundamental_valuation_date": technical.get("fundamental_valuation_date"),
            "rejection_reasons": rejection_reasons,
            "probability_up": probability,
            "predicted_return": learned.get("predicted_return"),
            "confidence": learned.get("confidence"),
            "sentiment_date": learned.get("sentiment_date"),
            "reference_price": float(data["closes"][symbol][-1]),
            "data_asof": data["data_end"],
            "score_components": technical.get("score_components", {}),
            "weighted_score_components": technical.get("weighted_score_components", {}),
            "preference_weights": technical.get("preference_weights", {}),
            "liquidity_amount": metadata.get(symbol, {}).get("liquidity_amount"),
            "liquidity_rank": metadata.get(symbol, {}).get("liquidity_rank"),
            "sector_names": metadata.get(symbol, {}).get("sector_names", []),
            "score_explanation": "五类分项按已保存关注重点加权；细分策略只在受控范围内放大或缩小各类影响",
            "feature_profile": _feature_profile(
                technical, learned, metadata.get(symbol, {}),
                public_information.get(symbol, {}), data["data_end"],
            ),
        })
    ranked = sorted(rows, key=lambda item: item["composite_score"], reverse=True)
    selected = (
        [item for item in ranked if item["eligible"]][
            :strategy["parameters"]["max_positions"]
        ] if allocate_positions else []
    )
    if selected:
        inverse_vol = [1.0 / max(item["annual_volatility"], 0.08) for item in selected]
        exposure = min(0.95, strategy["parameters"]["max_position_pct"] * len(selected))
        total = sum(inverse_vol)
        for item, raw in zip(selected, inverse_vol):
            target_weight = min(strategy["parameters"]["max_position_pct"], exposure * raw / total)
            shares = math.floor(mandate["capital"] * target_weight /
                                item["reference_price"] / mandate["execution"]["lot_size"]) * mandate["execution"]["lot_size"]
            market_value = shares * item["reference_price"]
            item["shares"] = int(shares)
            item["weight"] = round(market_value / mandate["capital"], 6)
            item["amount"] = round(market_value, 2)
            item["stop_price"] = round(item["reference_price"] *
                                       (1 - strategy["parameters"]["stop_loss"]), 4)
            item["take_profit_price"] = round(item["reference_price"] *
                                              (1 + strategy["parameters"]["take_profit"]), 4)
    invested = sum(item.get("amount", 0.0) for item in selected)
    return {
        "profile": strategy["profile"], "profile_label": strategy["label"],
        "model_version": model["model_version"], "data_asof": data["data_end"],
        "positions": selected, "cash_amount": round(mandate["capital"] - invested, 2),
        "cash_weight": round(max(0.0, 1.0 - invested / mandate["capital"]), 6),
        "candidate_ranking": ranked,
        "history": {
            "start": data["data_start"], "end": data["data_end"],
            "rows": data["rows"], "raw_rows": data["raw_rows"],
        },
        "rules": {
            "stop_loss_pct": mandate["stop_loss_pct"],
            "take_profit_pct": mandate["take_profit_pct"],
            "trailing_stop_pct": mandate["trailing_stop_pct"],
            "take_profit_mode": mandate["take_profit_mode"],
            "rebalance_days": strategy["parameters"]["rebalance_days"],
            "risk_lock_drawdown_pct": round(strategy["parameters"]["risk_budget"] * 100, 2),
            "fundamental_weight": strategy["parameters"].get("fundamental_weight", 0.0),
            "fundamental_minimum_score": strategy["parameters"].get(
                "fundamental_minimum_score"),
            "fundamental_minimum_coverage": strategy["parameters"].get(
                "fundamental_minimum_coverage"),
            "fundamental_dimension_weights": strategy["parameters"].get(
                "fundamental_dimension_weights", {}),
            "fundamental_timing": "NOTICE_DATE <= signal_date; valuation_asof <= signal_date",
            "preference_weights": strategy["parameters"].get("preference_weights", {}),
            "preference_version": mandate.get("preference_version"),
            "strategy_style": strategy["parameters"].get("strategy_style", "auto"),
            "strategy_style_label": strategy["parameters"].get("strategy_style_label", "自动选择"),
            "backtest_window_years": mandate.get("backtest_window_years", 3),
        },
    }


def recommendation_methodology_payload() -> dict:
    """Expose the live recommendation contract in plain, structured form."""
    from .continuous_learning import FEATURE_NAMES, INITIAL_COEFFICIENTS
    from .fundamentals import PROFILE_RULES
    from .strategy_evolution import BASE_PARAMETERS

    dimension_labels = {
        "growth": "成长能力", "quality": "盈利质量", "cashflow": "现金流",
        "safety": "偿债安全", "value": "估值水平",
    }
    feature_labels = {
        "bias": "基础偏置", "mom_5": "近5日涨跌", "mom_20": "近20日涨跌",
        "ma_spread_5_20": "5日与20日均线差", "vol_20": "近20日波动",
        "volume_z20": "成交量是否异常", "sentiment": "公开信息情绪",
        "market_mom_5": "大盘近5日涨跌",
    }
    profiles = {}
    for key, rules in PROFILE_RULES.items():
        profiles[key] = {
            "label": PROFILE_LABELS[key],
            "fundamental_weight": rules["fundamental_weight"],
            "minimum_score": rules["minimum_score"],
            "minimum_coverage": rules["minimum_coverage"],
            "dimension_weights": [
                {"key": name, "label": dimension_labels[name], "weight": weight}
                for name, weight in rules["dimensions"].items()
            ],
            "base_parameters": dict(BASE_PARAMETERS[key]),
        }
    return {
        "research_only": True,
        "order_execution": False,
        "decision_steps": [
            {"key": "mandate", "label": "读取你的条件", "detail": "本金、期限、板块、止盈止损和持仓数量"},
            {"key": "universe", "label": "建立候选池", "detail": "按板块匹配并优先保留成交活跃的股票"},
            {"key": "score", "label": "逐只计算分数", "detail": "走势、波动、财务数据和上涨概率共同评分"},
            {"key": "risk", "label": "检查风险条件", "detail": "趋势、上涨概率、财务完整度和回撤要求"},
            {"key": "output", "label": "给出推荐", "detail": "正式推荐优先，其次显示重点关注对象"},
        ],
        "ranking_formula": {
            "display": "综合分 = 走势分×走势权重 + 财务分×财务权重 + 概率分×概率权重 + 活跃度分×活跃度权重 + 稳定分×稳定权重；系统会把权重自动换算成合计100%",
            "momentum": "动量 = 上一交易日收盘价 ÷ N日前收盘价 - 1",
            "trend": "均线趋势 = 短期均价 ÷ 长期均价 - 1",
            "volatility": "年化波动 = 最近20个交易日日收益标准差 × √252",
            "eligibility": "正式入选 = 价格和短期均线都高于长期均线，且动量>0，并同时通过上涨概率和财务条件",
        },
        "profiles": profiles,
        "strategy_styles": [
            {"key": key, "label": item["label"],
             "plain_description": item["plain_description"]}
            for key, item in STRATEGY_STYLES.items()
        ],
        "rolling_backtest": {
            "default_years": 3,
            "choices": [1, 3, 5],
            "default_trading_days": 756,
            "split": "每126个交易日为一期；每期开始前只用已结束时期选择参数，本期结果只用于下一期更新，最后一期只做一次最终检查",
            "cross_validation": "锚定起点、每半年向前递推的样本外交叉验证",
            "period_trading_days": 126,
            "final_period_is_untouched": True,
            "queue_rule": "每个交易日收盘后加入最新一天，并移除窗口外最旧一天",
        },
        "online_model": {
            "label": "每日更新的上涨概率模型",
            "features": [
                {"key": name, "label": feature_labels[name],
                 "initial_coefficient": INITIAL_COEFFICIENTS[name]}
                for name in FEATURE_NAMES
            ],
            "coefficient_note": "这里显示初始系数；开盘日更新时会用已实现结果继续训练，当前生效版本以页面结果编号为准。",
        },
        "adjustability": [
            {"item": "本金、期限、板块、股票池、止盈止损、最大回撤、持仓数量", "mode": "可直接修改", "effect": "重新读取匹配的已评测结果"},
            {"item": "激进/中立/保守风险偏好", "mode": "可直接切换", "effect": "切换财务权重、均线窗口和风险参数预设"},
            {"item": "模型系数、上涨概率下限、正式评分权重", "mode": "需重新评测", "effect": "通过样本外回测和稳定性检查后才能成为正式版本"},
            {"item": "五类关注重点、细分策略和回测年限", "mode": "保存后立即影响当前排序", "effect": "生成独立参数版本；盘后用同一参数完整回测，通过门禁后发布正式版本"},
        ],
        "plain_language_glossary": [
            {"term": "最多尝试方案数", "meaning": "系统在每个风险档位最多比较多少组受控参数；数量越大，盘后回测越久，不代表收益一定更高。"},
            {"term": "样本外", "meaning": "这段历史没有参与挑参数，用来检查策略遇到陌生数据时是否仍然有效。"},
            {"term": "最大回撤", "meaning": "历史资金从某个高点到之后低点的最大跌幅。"},
            {"term": "Brier分数", "meaning": "上涨概率和真实结果之间的误差，越低越好。"},
            {"term": "AUC", "meaning": "模型把后来上涨的股票排在下跌股票前面的能力，0.5约等于随机。"},
        ],
    }


def _result_change_summary(previous_result: dict, current_result: dict) -> dict:
    previous = previous_result.get("recommendation", {})
    current = current_result.get("recommendation", {})
    previous_positions = {str(item.get("symbol")): item for item in previous.get("positions", [])}
    current_positions = {str(item.get("symbol")): item for item in current.get("positions", [])}
    previous_ranking = {str(item.get("symbol")): index + 1
                        for index, item in enumerate(previous.get("candidate_ranking", []))}
    ranking_changes = []
    for index, item in enumerate(current.get("candidate_ranking", []), 1):
        symbol = str(item.get("symbol"))
        old_rank = previous_ranking.get(symbol)
        if old_rank is not None and old_rank != index:
            ranking_changes.append({"symbol": symbol, "name": item.get("name"),
                                    "from": old_rank, "to": index, "change": old_rank - index})
    previous_parameters = previous.get("parameters", {})
    current_parameters = current.get("parameters", {})
    parameter_changes = [
        {"parameter": key, "before": previous_parameters.get(key),
         "after": current_parameters.get(key)}
        for key in sorted(set(previous_parameters) | set(current_parameters))
        if previous_parameters.get(key) != current_parameters.get(key)
        and key not in {"fundamental_dimension_weights", "style_multipliers"}
    ]
    return {
        "comparison_available": bool(previous_result),
        "previous_data_asof": previous.get("data_asof"),
        "current_data_asof": current.get("data_asof"),
        "added_positions": [current_positions[symbol] for symbol in current_positions
                            if symbol not in previous_positions],
        "removed_positions": [previous_positions[symbol] for symbol in previous_positions
                              if symbol not in current_positions],
        "ranking_changes": sorted(ranking_changes, key=lambda item: abs(item["change"]),
                                  reverse=True)[:10],
        "parameter_changes": parameter_changes,
        "plain_reason": "变化可能来自新交易日行情、最新公告日基本面、模型更新或本参数版本；逐项差异见本区。",
    }


def _version_quant_result(conn, mandate_id: int, run_id: int, result: dict) -> dict:
    recommendation = result["recommendation"]
    expectation = recommendation["expectation"]
    metrics = recommendation["holdout_metrics"]
    score = expectation["p50"] - abs(metrics["max_drawdown"]) * 0.5
    previous = conn.execute(
        """SELECT * FROM quant_portfolio_versions
           WHERE mandate_id=? AND status='ACTIVE' ORDER BY id DESC LIMIT 1""",
        (mandate_id,),
    ).fetchone()
    previous_result = _load(previous["result_json"], {}) if previous else {}
    result["change_summary"] = _result_change_summary(previous_result, result)
    same_snapshot = bool(previous and
        previous_result.get("data", {}).get("end") == result.get("data", {}).get("end") and
        previous_result.get("fundamental_time_policy") == result.get("fundamental_time_policy") and
        previous_result.get("recommendation", {}).get("model_version") == recommendation.get("model_version") and
        [item["symbol"] for item in previous_result.get("candidates", [])] ==
        [item["symbol"] for item in result.get("candidates", [])])
    risk_pass = bool(metrics["risk_pass"] and recommendation["non_regression_pass"])
    non_regression = not previous or score >= float(previous["score"]) - 0.005
    gate = {"risk_pass": risk_pass, "non_regression_vs_active": non_regression,
            "new_snapshot": not same_snapshot,
            "thresholds": {"score_tolerance": 0.005,
                           "max_drawdown_pct": result["request"]["max_drawdown_pct"]}}
    if same_snapshot and risk_pass and non_regression:
        status = "UNCHANGED"
        version_key = str(previous["version_key"])
    elif risk_pass and non_regression:
        status = "ACTIVE"
        version_key = f"quant-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        conn.execute(
            "UPDATE quant_portfolio_versions SET status='ARCHIVED' WHERE mandate_id=? AND status='ACTIVE'",
            (mandate_id,),
        )
    else:
        status = "REJECTED"
        version_key = f"quant-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    if status != "UNCHANGED":
        conn.execute(
            """INSERT INTO quant_portfolio_versions
               (version_key,mandate_id,run_id,parent_version,status,score,gate_json,
                result_json,created_at,activated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (version_key, mandate_id, run_id, previous["version_key"] if previous else None,
             status, score, _dump(gate), _dump(result), _now(),
             _now() if status == "ACTIVE" else None),
        )
    return {"version_key": version_key, "status": status, "score": round(score, 6),
            "gate": gate, "automatic_research_promotion": True,
            "order_execution": False}


def run_quant_portfolio(inputs: dict, normalized: bool = False, trigger_kind: str = "manual",
                        learning_cycle_id: int | None = None,
                        mandate_id: int | None = None) -> dict:
    initialize()
    from .fundamentals import VALUATION_TIME_POLICY
    request = inputs if normalized else normalize_quant_request(inputs)
    stamp = _now()
    with closing(connect()) as conn:
        if mandate_id is None:
            conn.execute("BEGIN IMMEDIATE")
            mandate_id, mandate_key, _ = _upsert_quant_mandate(conn, request, stamp)
        else:
            row = conn.execute("SELECT * FROM quant_mandates WHERE id=?", (mandate_id,)).fetchone()
            if not row:
                raise ValueError("量化授权书不存在")
            mandate_key = str(row["mandate_key"])
        run_key = f"quant-run-{uuid.uuid4().hex}"
        cursor = conn.execute(
            """INSERT INTO quant_portfolio_runs
               (run_key,mandate_id,learning_cycle_id,trigger_kind,status,started_at)
               VALUES(?,?,?,?, 'RUNNING',?)""",
            (run_key, mandate_id, learning_cycle_id, trigger_kind, stamp),
        )
        run_id = int(cursor.lastrowid)
        conn.commit()
    try:
        prepared = _prepare_candidates(request)
        candidates = prepared["candidates"]
        if request["collect_sentiment"]:
            from .continuous_learning import SHANGHAI, collect_a_share_sentiment
            sentiment = collect_a_share_sentiment(
                [item["symbol"] for item in candidates], datetime.now(SHANGHAI).date()
            )
        else:
            sentiment = {"status": "SKIPPED", "documents": 0, "errors": []}
        mandate = {
            "name": request["name"], "capital": request["capital"],
            "horizon_months": request["horizon_months"],
            "target_return_pct": request["target_return_pct"],
            "max_drawdown_pct": request["max_drawdown_pct"],
            "stop_loss_pct": request["stop_loss_pct"],
            "take_profit_pct": request["take_profit_pct"],
            "trailing_stop_pct": request["trailing_stop_pct"],
            "sectors": request["sectors"],
            "universe": [{"input": item["symbol"], "symbol": item["symbol"],
                          "name": item["name"],
                          "sector_names": item.get("sector_names", [])}
                         for item in candidates],
            "max_positions": request["max_positions"],
            "take_profit_mode": request["take_profit_mode"],
            "max_iterations": request["max_iterations"],
            "backtest_window_years": request["backtest_window_years"],
            "strategy_style": request["strategy_style"],
            "preference_weights": request["preference_weights"],
            "preference_version": request["preference_version"],
            "execution": request["execution"],
            "target_semantics": "soft_objective_not_guarantee",
            "risk_semantics": "hard_maximum_peak_to_trough_drawdown",
            "universe_semantics": prepared["source"].lower(),
        }
        experiment = run_strategy_evolution(mandate, normalized=True)
        for strategy in experiment["strategies"]:
            strategy["expectation"] = expected_return_distribution(
                strategy["holdout"]["curve"], request["horizon_months"],
                request["target_return_pct"] / 100,
                f"{experiment['experiment_key']}|{strategy['profile']}",
            )
        selected, selection = _choose_strategy(
            experiment["strategies"], request["risk_profile"],
            request["target_return_pct"] / 100,
        )
        current = _current_portfolio(mandate, selected, candidates)
        if not selection["publish_gate_passed"]:
            current["positions"] = []
            current["cash_amount"] = request["capital"]
            current["cash_weight"] = 1.0
        research_recommendations = (
            [] if current["positions"] else
            _research_recommendations(current["candidate_ranking"], mandate, selected)
        )
        current["candidate_ranking"] = _display_candidate_ranking(
            current["candidate_ranking"], current["positions"]
        )
        research_watchlist = research_recommendations or _research_watchlist(
            current["candidate_ranking"], request["max_positions"]
        )
        research_names = "、".join(item["name"] for item in research_recommendations)
        decision_label = selection["label"]
        if research_names:
            decision_label += f"；试探推荐：{research_names}（非正式持仓）"
        recommendation = {
            "profile": selected["profile"], "profile_label": selected["label"],
            "selection_reason": selection["label"],
            "decision_status": selection["status"],
            "decision_label": decision_label,
            "target_met": selection["target_met"],
            "risk_constraints_met": selection["risk_constraints_met"],
            "non_regression_met": selection["non_regression_met"],
            "publish_gate_passed": selection["publish_gate_passed"],
            "potential_target_met": selection["potential_target_met"],
            "target_return": selection["target_return"],
            "target_gap": selection["target_gap"],
            "fallback_used": selection["fallback_used"],
            "expectation": selected["expectation"],
            "holdout_metrics": selected["holdout"]["metrics"],
            "non_regression_pass": selected["non_regression_pass"],
            "parameters": selected["parameters"],
            "model_version": current["model_version"],
            "data_asof": current["data_asof"],
            "positions": current["positions"],
            "cash_amount": current["cash_amount"],
            "cash_weight": current["cash_weight"],
            "rules": current["rules"],
            "candidate_ranking": current["candidate_ranking"],
            "research_recommendations": research_recommendations,
            "research_watchlist": research_watchlist,
            "research_cash_weight": round(max(
                0.0, 1.0 - sum(item["weight"] for item in research_recommendations)
            ), 6),
            "research_selection_policy": {
                "formal_gates_unchanged": True,
                "minimum_pillars": 2, "pillar_total": 3,
                "maximum_probability_relaxation": 0.03,
                "lot_aware": True, "order_execution": False,
            },
            "holdout_curve": selected["holdout"]["curve"][::max(1, len(selected["holdout"]["curve"]) // 160)],
            "credibility": {
                "validation_status": "HALF_YEAR_WALK_FORWARD_AND_UNTOUCHED_HOLDOUT",
                "model": experiment.get("prediction_audit", {}),
                "strategy": selected["holdout"]["metrics"],
                "rolling_stability": selected["validation"],
                "walk_forward_history": selected.get("walk_forward_history", []),
                "data_ranges": experiment["data"],
                "stocks": experiment["data"].get("universe_audit", []),
                "execution_assumptions": selected["holdout"].get("assumptions", {}),
                "costs_included": True,
            },
        }
        result = {
            "status": "SUCCESS", "run_key": run_key, "mandate_key": mandate_key,
            "request": request, "candidate_source": prepared["source"],
            "sector_resolution": prepared["sector_resolution"],
            "candidates": candidates, "recommendation": recommendation,
            "strategies": experiment["strategies"], "data": experiment["data"],
            "strategy_experiment_key": experiment["experiment_key"],
            "model_evaluation": {
                "version": current["model_version"],
                "method": "daily_prequential_prediction_then_outcome_update",
                "live_selection_uses_latest_completed_bar_only": True,
            },
            "fundamental_time_policy": VALUATION_TIME_POLICY,
            "data_refresh": {"universe": prepared["metadata"],
                             "market": prepared["market_refresh"], "sentiment": sentiment},
            "limitations": [
                "预期收益为历史留出集净收益的区块自助分布，不是保证收益。",
                "行业映射来自通达信本地盘后文件；上游不可用时使用带时间戳缓存。",
                "代码函数不会自行改写；每日更新的是可回滚的模型权重、参数和组合版本。",
                "研究组合不连接券商，不执行真实交易。",
            ],
            "research_only": True, "order_execution": False,
        }
        _ensure_recommendation_forecasts(result)
        with closing(connect()) as conn:
            version = _version_quant_result(conn, mandate_id, run_id, result)
            result["version"] = version
            conn.executemany(
                """INSERT INTO quant_portfolio_candidates
                   (run_id,symbol,name,sector_names_json,liquidity_amount,liquidity_rank)
                   VALUES(?,?,?,?,?,?)""",
                [(run_id, item["symbol"], item["name"], _dump(item["sector_names"]),
                  item.get("liquidity_amount"), item.get("liquidity_rank"))
                 for item in candidates],
            )
            conn.executemany(
                """INSERT INTO quant_portfolio_positions
                   (run_id,symbol,name,weight,shares,reference_price,probability_up,
                    predicted_return,composite_score) VALUES(?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["symbol"], item["name"], item["weight"], item["shares"],
                  item["reference_price"], item.get("probability_up"),
                  item.get("predicted_return"), item["composite_score"])
                 for item in recommendation["positions"]],
            )
            conn.execute(
                """UPDATE quant_portfolio_runs SET status='SUCCESS',error=NULL,data_asof=?,
                   prediction_model_version=?,strategy_experiment_key=?,result_json=?,
                   finished_at=? WHERE id=?""",
                (result["data"]["end"], current["model_version"],
                 experiment["experiment_key"], _dump(result), _now(), run_id),
            )
            conn.commit()
        try:
            from .signal_service import materialize_decision_signals
            signal_publication = materialize_decision_signals({
                "mandate": {
                    "id": mandate_id, "mandate_key": mandate_key,
                    "name": request["name"], "input": request,
                },
                "version": result["version"], "result": result,
            }, dispatch=trigger_kind == "daily_post_close", conn_factory=connect)
        except Exception as exc:
            signal_publication = {
                "created": 0, "queued": 0, "status": "FAILED",
                "error": str(exc), "research_only": True, "order_execution": False,
            }
        result["signal_publication"] = signal_publication
        output = DATA_LAKE / "research" / "quant_portfolios" / f"{run_key}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_dump(result), encoding="utf-8")
        result["artifact_path"] = str(output)
        return result
    except Exception as exc:
        with closing(connect()) as conn:
            conn.execute(
                "UPDATE quant_portfolio_runs SET status='FAILED',error=?,finished_at=? WHERE id=?",
                (str(exc), _now(), run_id),
            )
            conn.commit()
        raise


def refresh_active_quant_portfolios(learning_cycle_id: int | None = None,
                                    trigger_kind: str = "daily_post_close",
                                    only_mandate_keys: list[str] | None = None) -> dict:
    initialize()
    from .fundamentals import VALUATION_TIME_POLICY
    with closing(connect()) as conn:
        candidates = conn.execute(
            "SELECT * FROM quant_mandates WHERE status='ACTIVE' ORDER BY id DESC"
        ).fetchall()
        cycle = (
            conn.execute(
                "SELECT cycle_date FROM harness_learning_cycles WHERE id=?",
                (learning_cycle_id,),
            ).fetchone()
            if learning_cycle_id is not None else None
        )
    cache_complete_asof = str(cycle["cycle_date"]) if cycle else None
    completed = {}
    active_model_version = None
    if learning_cycle_id is not None:
        with closing(connect()) as conn:
            active_model = conn.execute(
                "SELECT version_key FROM prediction_model_versions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            active_model_version = active_model[0] if active_model else None
            for saved in conn.execute(
                """SELECT mandate_id,result_json,prediction_model_version FROM quant_portfolio_runs
                   WHERE learning_cycle_id=? AND trigger_kind=? AND status='SUCCESS'
                   ORDER BY id DESC""", (learning_cycle_id, trigger_kind),
            ):
                completed.setdefault(int(saved["mandate_id"]), {
                    **_load(saved["result_json"], {}),
                    "checkpoint_model_version": saved["prediction_model_version"],
                })
    rows, identities = [], set()
    for row in candidates:
        saved = completed.get(int(row["id"]), {})
        # Return this cycle's checkpoints too, so retry summaries replace stale versions.
        # The loop below validates their model, policy, date and normalized constraints.
        if (only_mandate_keys is not None and row["mandate_key"] not in only_mandate_keys
                and not saved):
            continue
        identity = _mandate_identity(_load(row["input_json"], {}))
        if identity in identities:
            continue
        identities.add(identity)
        rows.append(row)
    results, errors = [], []
    reused = 0
    for row in rows:
        try:
            # Stored mandates can predate newer optional fields; normalize them again
            # before each scheduled run so schema defaults evolve safely.
            request = normalize_quant_request(_load(row["input_json"], {}))
            request["refresh_data"] = True
            request["collect_sentiment"] = True
            if cache_complete_asof:
                request["_daily_cache_asof"] = cache_complete_asof
            saved = completed.get(int(row["id"]), {})
            if (saved.get("run_key") and saved.get("version")
                    and saved.get("request")
                    and saved.get("checkpoint_model_version") == active_model_version
                    and saved.get("fundamental_time_policy") == VALUATION_TIME_POLICY
                    and saved.get("data", {}).get("end") == cache_complete_asof
                    and _mandate_identity(normalize_quant_request(saved["request"]))
                    == _mandate_identity(normalize_quant_request(request))):
                results.append({"mandate_key": row["mandate_key"], "run_key": saved["run_key"],
                                "version": saved["version"], "reused_checkpoint": True})
                reused += 1
                continue
            result = run_quant_portfolio(
                request, normalized=True, trigger_kind=trigger_kind,
                learning_cycle_id=learning_cycle_id, mandate_id=int(row["id"]),
            )
            results.append({"mandate_key": row["mandate_key"],
                            "run_key": result["run_key"],
                            "version": result["version"]})
        except Exception as exc:
            errors.append({"mandate_key": row["mandate_key"], "error": repr(exc)})
    return {"mandates": len(rows), "updated": len(results), "reused": reused,
            "results": results, "errors": errors}


def recover_interrupted_quant_runs() -> int:
    """Close only expired runs left behind by a previous service instance."""
    initialize()
    stamp = _now()
    lease_seconds = max(
        30, int(os.environ.get("ARGUS_QUANT_RUN_LEASE_MINUTES", "240"))
    ) * 60
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT id,started_at FROM quant_portfolio_runs
               WHERE status='RUNNING' ORDER BY id"""
        ).fetchall()
        rows = [row for row in rows if (
            (age := _timestamp_age_seconds(row["started_at"])) is None or
            age >= lease_seconds
        )]
        for row in rows:
            conn.execute(
                """UPDATE quant_portfolio_runs SET status='INTERRUPTED',
                   error=COALESCE(error,?),finished_at=? WHERE id=? AND status='RUNNING'""",
                ("服务重启前的量化运行未完成，可从 Harness 检查点重新运行", stamp, row["id"]),
            )
        conn.commit()
    return len(rows)


def quant_portfolio_payload(limit: int = 20) -> dict:
    initialize()
    with closing(connect()) as conn:
        runs = []
        for row in conn.execute(
            """SELECT r.*,m.mandate_key,m.name FROM quant_portfolio_runs r
               JOIN quant_mandates m ON m.id=r.mandate_id
               ORDER BY r.id DESC LIMIT ?""", (max(1, min(int(limit), 100)),),
        ):
            item = dict(row)
            result = _load(item.pop("result_json"), {})
            recommendation = result.get("recommendation", {})
            item["summary"] = {
                "profile_label": recommendation.get("profile_label"),
                "data_asof": recommendation.get("data_asof"),
                "positions": recommendation.get("positions", []),
                "expectation": recommendation.get("expectation", {}),
            }
            runs.append(item)
        versions = []
        for row in conn.execute(
            """SELECT v.*,m.mandate_key,m.name FROM quant_portfolio_versions v
               JOIN quant_mandates m ON m.id=v.mandate_id
               ORDER BY v.id DESC LIMIT ?""", (max(1, min(int(limit), 100)),),
        ):
            item = dict(row)
            item["gate"] = _load(item.pop("gate_json"), {})
            item.pop("result_json", None)
            versions.append(item)
    return {"runs": runs, "versions": versions,
            "automatic_research_promotion": True,
            "function_source_self_modification": True,
            "source_modification_scope": "gated intraday strategy recipes",
            "order_execution": False}
