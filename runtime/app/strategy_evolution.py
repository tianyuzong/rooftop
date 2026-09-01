"""Deterministic portfolio-strategy evolution with auditable out-of-sample tests.

This module simulates research strategies only. It has no broker, account, or
order API and cannot place real trades. Parameters may evolve inside a fixed
allowlist; executable code and risk limits never mutate themselves.
"""

from __future__ import annotations

import json
import math
import statistics
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import DATA_LAKE, connect, initialize


PROFILE_LABELS = {
    "aggressive": "激进",
    "balanced": "中立",
    "conservative": "保守",
}

PREFERENCE_LABELS = {
    "trend": "近期走势",
    "fundamental": "公司经营与财务",
    "probability": "模型估算上涨概率",
    "liquidity": "成交活跃度",
    "stability": "价格稳定性",
}

DEFAULT_PREFERENCE_WEIGHTS = {
    "aggressive": {"trend": 0.30, "fundamental": 0.20, "probability": 0.25,
                   "liquidity": 0.15, "stability": 0.10},
    "balanced": {"trend": 0.25, "fundamental": 0.30, "probability": 0.20,
                 "liquidity": 0.10, "stability": 0.15},
    "conservative": {"trend": 0.15, "fundamental": 0.40, "probability": 0.15,
                     "liquidity": 0.05, "stability": 0.25},
}

STRATEGY_STYLES = {
    "trend_following": {
        "label": "趋势跟随", "plain_description": "更看重价格是否持续走强",
        "multipliers": {"trend": 1.35, "fundamental": 0.80, "probability": 1.05,
                        "liquidity": 1.00, "stability": 0.75},
    },
    "quality_growth": {
        "label": "优质成长", "plain_description": "兼顾公司增长和盈利质量",
        "multipliers": {"trend": 0.90, "fundamental": 1.35, "probability": 1.00,
                        "liquidity": 0.85, "stability": 1.00},
    },
    "garp": {
        "label": "合理价格成长", "plain_description": "寻找成长不错且估值不过分的公司",
        "multipliers": {"trend": 0.85, "fundamental": 1.25, "probability": 1.05,
                        "liquidity": 0.90, "stability": 1.05},
    },
    "low_volatility": {
        "label": "低波动", "plain_description": "优先选择价格相对稳定的公司",
        "multipliers": {"trend": 0.75, "fundamental": 1.05, "probability": 0.85,
                        "liquidity": 0.85, "stability": 1.55},
    },
    "cashflow_value": {
        "label": "现金流与估值", "plain_description": "更看重现金流、安全性和估值",
        "multipliers": {"trend": 0.70, "fundamental": 1.45, "probability": 0.80,
                        "liquidity": 0.75, "stability": 1.25},
    },
    "catalyst_momentum": {
        "label": "催化与动量", "plain_description": "关注近期强势、放量和公开信息变化",
        "multipliers": {"trend": 1.25, "fundamental": 0.75, "probability": 1.25,
                        "liquidity": 1.20, "stability": 0.65},
    },
}

BASE_PARAMETERS = {
    "aggressive": {
        "fast_window": 10, "slow_window": 40, "momentum_window": 20,
        "rebalance_days": 5, "stop_loss": 0.08, "trailing_stop": 0.10,
        "take_profit": 0.28, "switch_threshold": 0.025,
        "min_holding_days": 3, "cooldown_days": 10,
    },
    "balanced": {
        "fast_window": 20, "slow_window": 60, "momentum_window": 60,
        "rebalance_days": 10, "stop_loss": 0.07, "trailing_stop": 0.08,
        "take_profit": 0.22, "switch_threshold": 0.035,
        "min_holding_days": 5, "cooldown_days": 15,
    },
    "conservative": {
        "fast_window": 60, "slow_window": 120, "momentum_window": 120,
        "rebalance_days": 20, "stop_loss": 0.05, "trailing_stop": 0.06,
        "take_profit": 0.16, "switch_threshold": 0.05,
        "min_holding_days": 10, "cooldown_days": 20,
    },
}

DEFAULT_EXECUTION = {
    "commission_rate": 0.0003,
    "minimum_commission": 5.0,
    "sell_stamp_tax_rate": 0.0005,
    "slippage_rate": 0.001,
    "lot_size": 100,
    "max_participation_rate": 0.05,
    "price_limit_pct": 0.10,
    "t_plus_one": True,
    "price_limit_model": "previous-close approximation; board/security exceptions not inferred",
}

HALF_YEAR_TRADING_DAYS = 126
MIN_FORMATION_TRADING_DAYS = 190


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


def _bounded_number(value, name: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}必须是数字") from exc
    if not math.isfinite(number) or number < low or number > high:
        raise ValueError(f"{name}必须在 {low:g} 到 {high:g} 之间")
    return number


def normalize_mandate(inputs: dict) -> dict:
    """Validate and freeze the user's investment authorization contract."""
    if not isinstance(inputs, dict):
        raise ValueError("投资授权书必须是对象")
    from .stock_compare import resolve_stock, split_stock_inputs

    requested = split_stock_inputs(inputs.get("stocks", []))
    resolved = [resolve_stock(item) for item in requested]
    symbols = [item["symbol"] for item in resolved]
    if len(symbols) != len(set(symbols)):
        raise ValueError("股票池中存在重复标的")

    capital = _bounded_number(inputs.get("capital", 100000), "本金", 1000, 1_000_000_000)
    horizon = int(_bounded_number(inputs.get("horizon_months", 12), "投资期限（月）", 3, 120))
    target = _bounded_number(inputs.get("target_return_pct", 20), "目标收益率", 0, 300)
    max_drawdown = _bounded_number(inputs.get("max_drawdown_pct", 20), "最大回撤", 1, 80)
    stop_loss = _bounded_number(inputs.get("stop_loss_pct", 8), "止损", 1, max_drawdown)
    take_profit = _bounded_number(inputs.get("take_profit_pct", 20), "止盈", 1, 300)
    trailing_stop = _bounded_number(
        inputs.get("trailing_stop_pct", min(8, max_drawdown)),
        "移动止盈回撤", 1, max_drawdown,
    )
    max_iterations = int(_bounded_number(inputs.get("max_iterations", 10), "迭代次数", 1, 20))
    backtest_window_years = int(_bounded_number(
        inputs.get("backtest_window_years", 3), "滚动回测年限", 1, 5
    ))
    if backtest_window_years not in {1, 3, 5}:
        raise ValueError("滚动回测年限只能选择 1、3 或 5 年")
    strategy_style = str(inputs.get("strategy_style", "auto")).strip().lower()
    if strategy_style != "auto" and strategy_style not in STRATEGY_STYLES:
        raise ValueError("不支持的细分策略类型")
    raw_preferences = inputs.get("preference_weights") or {}
    if raw_preferences and not isinstance(raw_preferences, dict):
        raise ValueError("关注重点必须是对象")
    preferences = {}
    for key in PREFERENCE_LABELS:
        if key in raw_preferences:
            preferences[key] = _bounded_number(
                raw_preferences[key], PREFERENCE_LABELS[key], 0, 100
            )
    if preferences:
        total = sum(preferences.values())
        if total <= 0:
            raise ValueError("关注重点至少有一项大于 0")
        preferences = {key: round(preferences.get(key, 0.0) / total, 6)
                       for key in PREFERENCE_LABELS}
    max_positions = int(_bounded_number(inputs.get("max_positions", min(3, len(symbols))),
                                        "最大同时持仓数", 1, len(symbols)))
    take_profit_mode = str(inputs.get("take_profit_mode", "trailing")).strip().lower()
    if take_profit_mode not in {"trailing", "fixed"}:
        raise ValueError("止盈方式只能是 trailing 或 fixed")
    sectors_value = inputs.get("sectors", [])
    if isinstance(sectors_value, str):
        sectors = [item.strip() for item in sectors_value.replace("，", ",").split(",") if item.strip()]
    elif isinstance(sectors_value, list):
        sectors = [str(item).strip() for item in sectors_value if str(item).strip()]
    else:
        raise ValueError("关注板块必须是文本或列表")

    execution = dict(DEFAULT_EXECUTION)
    overrides = inputs.get("execution", {})
    if overrides:
        if not isinstance(overrides, dict):
            raise ValueError("成交假设必须是对象")
        allowed = set(DEFAULT_EXECUTION) - {"price_limit_model"}
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError("不支持的成交假设：" + "、".join(sorted(unknown)))
        execution.update(overrides)
    execution["commission_rate"] = _bounded_number(
        execution["commission_rate"], "佣金率", 0, 0.02)
    execution["minimum_commission"] = _bounded_number(
        execution["minimum_commission"], "最低佣金", 0, 100)
    execution["sell_stamp_tax_rate"] = _bounded_number(
        execution["sell_stamp_tax_rate"], "卖出印花税率", 0, 0.02)
    execution["slippage_rate"] = _bounded_number(
        execution["slippage_rate"], "滑点率", 0, 0.05)
    execution["lot_size"] = int(_bounded_number(execution["lot_size"], "整手股数", 1, 10000))
    execution["max_participation_rate"] = _bounded_number(
        execution["max_participation_rate"], "最大成交量参与率", 0.0001, 1)
    execution["price_limit_pct"] = _bounded_number(
        execution["price_limit_pct"], "涨跌停比例", 0.01, 0.5)
    execution["t_plus_one"] = bool(execution["t_plus_one"])

    return {
        "name": str(inputs.get("name") or "投资策略实验").strip()[:80],
        "capital": round(capital, 2),
        "horizon_months": horizon,
        "target_return_pct": round(target, 4),
        "max_drawdown_pct": round(max_drawdown, 4),
        "stop_loss_pct": round(stop_loss, 4),
        "take_profit_pct": round(take_profit, 4),
        "trailing_stop_pct": round(trailing_stop, 4),
        "sectors": sectors[:20],
        "universe": resolved,
        "max_positions": max_positions,
        "take_profit_mode": take_profit_mode,
        "max_iterations": max_iterations,
        "backtest_window_years": backtest_window_years,
        "strategy_style": strategy_style,
        "preference_weights": preferences,
        "execution": execution,
        "target_semantics": "soft_objective_not_guarantee",
        "risk_semantics": "hard_maximum_peak_to_trough_drawdown",
        "universe_semantics": "explicit_stock_pool_is_authoritative; sectors_are_audit_labels",
    }


def _risk_budget(profile: str, mandate: dict) -> float:
    user_limit = mandate["max_drawdown_pct"] / 100
    scale = {"conservative": 0.55, "balanced": 0.80, "aggressive": 1.0}[profile]
    ceiling = {"conservative": 0.10, "balanced": 0.16, "aggressive": user_limit}[profile]
    return min(user_limit * scale, ceiling)


def _load_aligned_universe(mandate: dict, data_asof: str | None = None) -> dict:
    from .stock_compare import _load_bars
    from .fundamentals import load_fundamental_timelines

    raw = {}
    for stock in mandate["universe"]:
        rows = _load_bars(stock["symbol"])
        if not rows:
            raise RuntimeError(f"{stock['name']}（{stock['symbol']}）没有前复权日线")
        raw[stock["symbol"]] = {str(row["trade_date"]): row for row in rows}
    dates = sorted(set.intersection(*(set(rows) for rows in raw.values())))
    if data_asof:
        dates = [item for item in dates if item <= str(data_asof)]
    if len(dates) < 420:
        raise RuntimeError(f"共同交易日只有 {len(dates)} 天，至少需要 420 天才能进行滚动样本外验证")
    raw_start, raw_rows = dates[0], len(dates)
    requested_days = int(mandate.get("backtest_window_years", 3)) * 252
    # A one-year evaluation still needs earlier bars for indicators and three
    # validation folds. Those warm-up bars are disclosed separately.
    effective_days = max(504, requested_days)
    if len(dates) > effective_days:
        dates = dates[-effective_days:]
    bars = {symbol: [rows[day] for day in dates] for symbol, rows in raw.items()}
    return {
        "dates": dates,
        "bars": bars,
        "closes": {symbol: [float(row["close"]) for row in aligned]
                   for symbol, aligned in bars.items()},
        "symbols": list(raw),
        "data_start": dates[0],
        "data_end": dates[-1],
        "rows": len(dates),
        "raw_data_start": raw_start,
        "raw_rows": raw_rows,
        "requested_window_years": int(mandate.get("backtest_window_years", 3)),
        "requested_window_days": requested_days,
        "effective_window_days": len(dates),
        "fundamental_timelines": load_fundamental_timelines(list(raw), conn_factory=connect),
    }


def _candidate_parameters(profile: str, mandate: dict, iteration: int) -> dict:
    from .fundamentals import PROFILE_RULES

    base = BASE_PARAMETERS[profile]
    variants = (
        (1.00, 1.00, 1.00, 0, 0),
        (0.80, 0.90, 0.90, -2, -1),
        (1.20, 1.10, 1.10, 2, 1),
        (0.70, 1.00, 0.80, -1, 1),
        (1.30, 0.85, 1.20, 1, -1),
        (0.90, 1.15, 1.00, 3, 0),
        (1.10, 0.95, 0.85, -3, 1),
        (0.75, 1.05, 1.15, 1, 0),
        (1.25, 0.90, 0.95, -1, -1),
        (1.00, 1.10, 1.05, 2, 1),
    )
    window_scale, stop_scale, trail_scale, rebalance_delta, position_delta = variants[
        iteration % len(variants)]
    cycle = iteration // len(variants)
    slow = max(20, int(round(base["slow_window"] * window_scale + cycle * 5)))
    fast = min(slow - 5, max(5, int(round(base["fast_window"] * window_scale))))
    momentum = max(10, int(round(base["momentum_window"] * window_scale)))
    risk_cap = _risk_budget(profile, mandate)
    hard_stop = mandate.get("stop_loss_pct")
    hard_trailing = mandate.get("trailing_stop_pct")
    hard_take_profit = mandate.get("take_profit_pct")
    stop_loss = (float(hard_stop) / 100 if hard_stop is not None else
                 min(risk_cap * 0.65, base["stop_loss"] * stop_scale))
    trailing = (float(hard_trailing) / 100 if hard_trailing is not None else
                min(risk_cap * 0.80, base["trailing_stop"] * trail_scale))
    max_positions = max(1, min(mandate["max_positions"],
                               mandate["max_positions"] + position_delta))
    fundamental = PROFILE_RULES[profile]
    requested_style = str(mandate.get("strategy_style", "auto"))
    style_key = (requested_style if requested_style in STRATEGY_STYLES else
                 tuple(STRATEGY_STYLES)[iteration % len(STRATEGY_STYLES)])
    style = STRATEGY_STYLES[style_key]
    custom_preferences = mandate.get("preference_weights") or {}
    preference_weights = dict(custom_preferences or DEFAULT_PREFERENCE_WEIGHTS[profile])
    return {
        "profile": profile,
        "fast_window": fast,
        "slow_window": slow,
        "momentum_window": momentum,
        "rebalance_days": max(3, base["rebalance_days"] + rebalance_delta),
        "stop_loss": round(max(0.01, stop_loss), 4),
        "trailing_stop": round(max(0.01, trailing), 4),
        "take_profit": round(float(hard_take_profit) / 100, 4)
        if hard_take_profit is not None else base["take_profit"],
        "take_profit_mode": mandate["take_profit_mode"],
        "switch_threshold": base["switch_threshold"],
        "min_holding_days": base["min_holding_days"],
        "cooldown_days": base["cooldown_days"],
        "max_positions": max_positions,
        "max_position_pct": round(min(1 / max_positions, {
            "aggressive": 0.60, "balanced": 0.45, "conservative": 0.35,
        }[profile]), 4),
        "prediction_weight": round((0.18, 0.24, 0.30, 0.36)[iteration % 4], 4),
        "prediction_floor": round((0.47, 0.49, 0.50)[iteration % 3], 4),
        "fundamental_weight": fundamental["fundamental_weight"],
        "fundamental_minimum_score": fundamental["minimum_score"],
        "fundamental_minimum_coverage": fundamental["minimum_coverage"],
        "fundamental_dimension_weights": fundamental["dimensions"],
        "risk_budget": round(risk_cap, 4),
        "strategy_style": style_key,
        "strategy_style_label": style["label"],
        "strategy_style_description": style["plain_description"],
        "style_multipliers": dict(style["multipliers"]),
        "preference_weights": preference_weights,
        "custom_preferences": bool(custom_preferences),
    }


def _signal(data: dict, symbol: str, index: int, params: dict) -> dict:
    from .fundamentals import fundamental_snapshot

    prior = index - 1
    slow = params["slow_window"]
    momentum_window = params["momentum_window"]
    if prior < max(slow, momentum_window):
        return {"eligible": False, "score": -1.0}
    closes = data["closes"][symbol]
    fast_ma = statistics.mean(closes[index - params["fast_window"]:index])
    slow_ma = statistics.mean(closes[index - slow:index])
    previous_close = closes[prior]
    anchor = closes[index - momentum_window - 1]
    momentum = previous_close / anchor - 1 if anchor else -1.0
    recent = closes[max(0, index - 21):index]
    returns = [recent[i] / recent[i - 1] - 1 for i in range(1, len(recent)) if recent[i - 1]]
    annual_vol = statistics.stdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    trend = fast_ma / slow_ma - 1 if slow_ma else -1.0
    model_probability = data.get("prediction_scores", {}).get(symbol, {}).get(
        data["dates"][prior]
    )
    model_edge = ((float(model_probability) - 0.5) * params.get("prediction_weight", 0.0)
                  if model_probability is not None else 0.0)
    timelines = data.get("fundamental_timelines") or {
        "reports": {}, "valuations": {}, "has_data": False,
    }
    fundamental = fundamental_snapshot(
        timelines, symbol, data["dates"][prior], params.get("profile", "balanced")
    )
    # Empty databases remain usable for isolated/synthetic tests. Once a
    # fundamental dataset exists, missing coverage is explicit and blocks entry.
    fundamental_pass = (not timelines.get("has_data") or fundamental["eligible"])
    fundamental_score = (-1.0 if timelines.get("has_data") and
                         fundamental.get("score") is None else
                         float(fundamental.get("score") or 0.0))
    effective_fundamental_weight = float(
        fundamental.get("fundamental_weight", params.get("fundamental_weight", 0.0))
    )
    bars = data.get("bars", {}).get(symbol, [])
    amounts = [float(row.get("amount") or 0.0)
               for row in bars[max(0, index - 60):index]]
    recent_amount = statistics.mean(amounts[-20:]) if amounts[-20:] else 0.0
    longer_amount = statistics.mean(amounts) if amounts else 0.0
    liquidity = (max(-1.0, min(1.0, math.log(max(recent_amount, 1.0) /
                  max(longer_amount, 1.0)))) if longer_amount else 0.0)
    probability_component = ((float(model_probability) - 0.5) * 2.0
                             if model_probability is not None else 0.0)
    components = {
        "trend": max(-1.0, min(1.0, momentum + trend * 2.0)),
        "fundamental": max(-1.0, min(1.0, fundamental_score)),
        "probability": max(-1.0, min(1.0, probability_component)),
        "liquidity": liquidity,
        "stability": max(-1.0, min(1.0, (0.45 - annual_vol) / 0.35)),
    }
    weights = params.get("preference_weights") or DEFAULT_PREFERENCE_WEIGHTS[
        params.get("profile", "balanced")
    ]
    multipliers = params.get("style_multipliers") or {}
    weighted = {
        key: components[key] * float(weights.get(key, 0.0)) *
             float(multipliers.get(key, 1.0))
        for key in components
    }
    score = sum(weighted.values()) + model_edge * 0.15
    if timelines.get("has_data") and fundamental.get("score") is None:
        score -= max(0.0, effective_fundamental_weight -
                      abs(weighted["fundamental"]))
    model_pass = (model_probability is None or
                  float(model_probability) >= params.get("prediction_floor", 0.0))
    reasons = []
    if not (previous_close > slow_ma and fast_ma > slow_ma and momentum > 0):
        reasons.append("趋势或动量未通过")
    if not model_pass:
        reasons.append("在线模型上涨概率未通过")
    if not fundamental_pass:
        reasons.extend(fundamental.get("reasons") or ["基本面门禁未通过"])
    return {
        "eligible": (previous_close > slow_ma and fast_ma > slow_ma and momentum > 0 and
                     model_pass and fundamental_pass),
        "score": score,
        "momentum": momentum,
        "trend": trend,
        "annual_volatility": annual_vol,
        "model_probability_up": model_probability,
        "fundamental_score": fundamental.get("score"),
        "fundamental_coverage": fundamental.get("coverage", 0.0),
        "fundamental_dimensions": fundamental.get("dimensions", {}),
        "fundamental_report_date": fundamental.get("report_date"),
        "fundamental_notice_date": fundamental.get("notice_date"),
        "fundamental_valuation_date": fundamental.get("valuation_date"),
        "fundamental_models": fundamental.get("models", {}),
        "effective_fundamental_weight": effective_fundamental_weight,
        "rejection_reasons": reasons,
        "score_components": components,
        "weighted_score_components": weighted,
        "preference_weights": weights,
    }


def _commission(gross: float, execution: dict) -> float:
    return max(execution["minimum_commission"], gross * execution["commission_rate"])


def _drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def _metrics(curve: list[dict], trades: list[dict], initial_cash: float,
             params: dict, mandate: dict, costs: float, turnover_value: float,
             exposure_values: list[float], violations: list[dict],
             benchmark_return: float = 0.0) -> dict:
    equities = [float(item["equity"]) for item in curve]
    daily = [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities))
             if equities[i - 1]]
    years = max(1 / 252, (len(equities) - 1) / 252)
    total_return = equities[-1] / initial_cash - 1
    annual_return = (equities[-1] / initial_cash) ** (1 / years) - 1
    volatility = statistics.stdev(daily) * math.sqrt(252) if len(daily) > 1 else 0.0
    sharpe = statistics.mean(daily) * 252 / volatility if volatility else 0.0
    maximum_drawdown = _drawdown(equities)
    calmar = annual_return / abs(maximum_drawdown) if maximum_drawdown else 0.0
    closed = [item for item in trades if item.get("side") == "SELL" and item.get("status") == "FILLED"]
    wins = [item for item in closed if float(item.get("pnl") or 0) > 0]
    losses = [item for item in closed if float(item.get("pnl") or 0) < 0]
    average_win = (statistics.mean(float(item["pnl"]) for item in wins) if wins else 0.0)
    average_loss = (abs(statistics.mean(float(item["pnl"]) for item in losses))
                    if losses else 0.0)
    monthly = {}
    for item in curve:
        monthly[str(item["date"])[:7]] = float(item["equity"])
    month_values = list(monthly.values())
    monthly_returns = [month_values[i] / month_values[i - 1] - 1
                       for i in range(1, len(month_values)) if month_values[i - 1]]
    horizon_return = ((1 + annual_return) ** (mandate["horizon_months"] / 12) - 1
                      if annual_return > -1 else -1.0)
    risk_pass = abs(maximum_drawdown) <= params["risk_budget"] + 1e-9 and not violations
    return {
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(equities[-1], 2),
        "total_return": round(total_return, 6),
        "annualized_return": round(annual_return, 6),
        "benchmark_total_return": round(benchmark_return, 6),
        "excess_return": round(total_return - benchmark_return, 6),
        "horizon_equivalent_return": round(horizon_return, 6),
        "annualized_volatility": round(volatility, 6),
        "sharpe_ratio": round(sharpe, 4),
        "calmar_ratio": round(calmar, 4),
        "max_drawdown": round(maximum_drawdown, 6),
        "risk_budget": params["risk_budget"],
        "risk_pass": risk_pass,
        "target_return": mandate["target_return_pct"] / 100,
        "target_reached": horizon_return >= mandate["target_return_pct"] / 100,
        "turnover": round(turnover_value / initial_cash, 4),
        "transaction_costs": round(costs, 2),
        "closed_trades": len(closed),
        "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
        "average_win": round(average_win, 2),
        "average_loss": round(average_loss, 2),
        "profit_loss_ratio": round(average_win / average_loss, 4)
        if average_loss else None,
        "worst_month": round(min(monthly_returns), 6) if monthly_returns else 0.0,
        "average_exposure": round(statistics.mean(exposure_values), 4) if exposure_values else 0.0,
        "blocked_executions": sum(item.get("status") == "BLOCKED" for item in trades),
        "violations": violations,
    }


def simulate_portfolio(data: dict, mandate: dict, params: dict,
                       start_index: int, end_index: int) -> dict:
    """Run a lagged-signal, next-open, multi-asset portfolio simulation."""
    execution = mandate["execution"]
    dates = data["dates"]
    symbols = data["symbols"]
    warmup = max(params["slow_window"], params["momentum_window"]) + 2
    start_index = max(start_index, warmup)
    end_index = min(end_index, len(dates) - 1)
    if end_index - start_index < 20:
        raise ValueError("回测区间过短")
    cash = float(mandate["capital"])
    initial_cash = cash
    positions: dict[str, dict] = {}
    cooldown_until: dict[str, int] = {}
    trades: list[dict] = []
    curve: list[dict] = []
    costs = turnover_value = 0.0
    peak_equity = initial_cash
    risk_locked = False
    violations: list[dict] = []
    exposure_values: list[float] = []

    def blocked(day: str, symbol: str, side: str, reason: str) -> None:
        trades.append({"date": day, "symbol": symbol, "side": side,
                       "status": "BLOCKED", "reason": reason})

    def sell(symbol: str, index: int, reason: str) -> bool:
        nonlocal cash, costs, turnover_value
        position = positions[symbol]
        bar = data["bars"][symbol][index]
        previous_close = float(data["bars"][symbol][index - 1]["close"])
        open_price = float(bar["open"] or 0)
        volume = float(bar.get("volume") or 0)
        day = dates[index]
        if open_price <= 0 or volume <= 0:
            blocked(day, symbol, "SELL", "SUSPENDED_OR_NO_VOLUME")
            return False
        if open_price <= previous_close * (1 - execution["price_limit_pct"] + 1e-6):
            blocked(day, symbol, "SELL", "LIMIT_DOWN_APPROXIMATION")
            return False
        if execution["t_plus_one"] and position["entry_index"] >= index:
            blocked(day, symbol, "SELL", "T_PLUS_ONE")
            return False
        lot = execution["lot_size"]
        capacity = math.floor(volume * execution["max_participation_rate"] / lot) * lot
        shares = min(position["shares"], capacity)
        if shares < lot:
            blocked(day, symbol, "SELL", "INSUFFICIENT_LIQUIDITY")
            return False
        price = open_price * (1 - execution["slippage_rate"])
        gross = shares * price
        fee = _commission(gross, execution) + gross * execution["sell_stamp_tax_rate"]
        cash += gross - fee
        costs += fee
        turnover_value += gross
        allocated_cost = position["cost_basis"] * (shares / position["shares"])
        pnl = gross - fee - allocated_cost
        position["shares"] -= shares
        position["cost_basis"] -= allocated_cost
        trades.append({"date": day, "symbol": symbol, "side": "SELL", "status": "FILLED",
                       "reason": reason, "shares": shares, "price": round(price, 4),
                       "fees": round(fee, 2), "pnl": round(pnl, 2)})
        if pnl < 0:
            cooldown_until[symbol] = index + params["cooldown_days"]
        if position["shares"] < lot:
            positions.pop(symbol, None)
        return True

    def buy(symbol: str, index: int, budget: float) -> bool:
        nonlocal cash, costs, turnover_value
        bar = data["bars"][symbol][index]
        previous_close = float(data["bars"][symbol][index - 1]["close"])
        open_price = float(bar["open"] or 0)
        volume = float(bar.get("volume") or 0)
        day = dates[index]
        if open_price <= 0 or volume <= 0:
            blocked(day, symbol, "BUY", "SUSPENDED_OR_NO_VOLUME")
            return False
        if open_price >= previous_close * (1 + execution["price_limit_pct"] - 1e-6):
            blocked(day, symbol, "BUY", "LIMIT_UP_APPROXIMATION")
            return False
        lot = execution["lot_size"]
        price = open_price * (1 + execution["slippage_rate"])
        capacity = math.floor(volume * execution["max_participation_rate"] / lot) * lot
        affordable = math.floor(min(budget, cash) / price / lot) * lot
        shares = min(capacity, affordable)
        if shares < lot:
            blocked(day, symbol, "BUY", "CASH_OR_LIQUIDITY_BELOW_ONE_LOT")
            return False
        gross = shares * price
        fee = _commission(gross, execution)
        while shares >= lot and gross + fee > cash:
            shares -= lot
            gross = shares * price
            fee = _commission(gross, execution) if shares else 0
        if shares < lot:
            blocked(day, symbol, "BUY", "CASH_BELOW_COST_INCLUSIVE_LOT")
            return False
        cash -= gross + fee
        costs += fee
        turnover_value += gross
        positions[symbol] = {"shares": shares, "entry_price": price, "peak": price,
                             "entry_index": index, "cost_basis": gross + fee}
        trades.append({"date": day, "symbol": symbol, "side": "BUY", "status": "FILLED",
                       "reason": "RANKED_ENTRY", "shares": shares, "price": round(price, 4),
                       "fees": round(fee, 2)})
        return True

    for index in range(start_index, end_index + 1):
        day = dates[index]
        previous_equity = cash + sum(
            position["shares"] * float(data["bars"][symbol][index - 1]["close"])
            for symbol, position in positions.items())
        peak_equity = max(peak_equity, previous_equity)
        current_drawdown = previous_equity / peak_equity - 1
        if current_drawdown <= -params["risk_budget"] * 0.92:
            risk_locked = True
            for symbol in list(positions):
                sell(symbol, index, "PORTFOLIO_DRAWDOWN_GUARD")

        signals = {symbol: _signal(data, symbol, index, params) for symbol in symbols}
        ranked = sorted((symbol for symbol in symbols if signals[symbol]["eligible"]),
                        key=lambda symbol: signals[symbol]["score"], reverse=True)
        targets = ranked[:params["max_positions"]]

        for symbol in list(positions):
            position = positions.get(symbol)
            if not position:
                continue
            previous_close = float(data["bars"][symbol][index - 1]["close"])
            position["peak"] = max(position["peak"], previous_close)
            held_days = index - position["entry_index"]
            reason = None
            if previous_close <= position["entry_price"] * (1 - params["stop_loss"]):
                reason = "STOP_LOSS"
            elif previous_close <= position["peak"] * (1 - params["trailing_stop"]):
                reason = "TRAILING_STOP"
            elif (params["take_profit_mode"] == "fixed" and
                  previous_close >= position["entry_price"] * (1 + params["take_profit"])):
                reason = "FIXED_TAKE_PROFIT"
            elif held_days >= params["min_holding_days"] and not signals[symbol]["eligible"]:
                reason = "TREND_EXIT"
            elif (index - start_index) % params["rebalance_days"] == 0 and targets and symbol not in targets:
                challenger = targets[0]
                if signals[challenger]["score"] - signals[symbol]["score"] >= params["switch_threshold"]:
                    reason = "ROTATE_TO_STRONGER_ASSET"
            if reason:
                sell(symbol, index, reason)

        if not risk_locked and (index - start_index) % params["rebalance_days"] == 0:
            close_equity = cash + sum(
                position["shares"] * float(data["bars"][symbol][index - 1]["close"])
                for symbol, position in positions.items())
            budget = close_equity * params["max_position_pct"]
            for symbol in targets:
                if len(positions) >= params["max_positions"] or symbol in positions:
                    continue
                if index < cooldown_until.get(symbol, -1):
                    continue
                buy(symbol, index, budget)

        invested = 0.0
        for symbol, position in positions.items():
            close_price = float(data["bars"][symbol][index]["close"])
            position["peak"] = max(position["peak"], close_price)
            invested += position["shares"] * close_price
        equity = cash + invested
        peak_equity = max(peak_equity, equity)
        drawdown = equity / peak_equity - 1
        if drawdown < -params["risk_budget"] - 1e-9 and not violations:
            violations.append({"date": day, "type": "MAX_DRAWDOWN",
                               "observed": round(drawdown, 6),
                               "limit": -params["risk_budget"]})
            risk_locked = True
        curve.append({"date": day, "equity": round(equity, 2),
                      "cash": round(cash, 2), "drawdown": round(drawdown, 6)})
        exposure_values.append(invested / equity if equity else 0.0)

    benchmark_returns = []
    for symbol in symbols:
        start_price = float(data["bars"][symbol][start_index]["open"] or 0.0)
        end_price = float(data["bars"][symbol][end_index]["close"] or 0.0)
        if start_price > 0 and end_price > 0:
            benchmark_returns.append(end_price / start_price - 1.0)
    metrics = _metrics(curve, trades, initial_cash, params, mandate, costs,
                       turnover_value, exposure_values, violations,
                       statistics.mean(benchmark_returns) if benchmark_returns else 0.0)
    return {
        "data_start": dates[start_index],
        "data_end": dates[end_index],
        "metrics": metrics,
        "curve": curve,
        "trades": trades,
        "open_positions": [{"symbol": symbol, **position}
                           for symbol, position in positions.items()],
        "assumptions": execution,
    }


def _folds(data: dict, mandate: dict) -> tuple[list[tuple[int, int]], tuple[int, int]]:
    """Build non-overlapping half-year folds ending at the latest common bar."""
    rows = len(data["dates"])
    period_count = (rows - MIN_FORMATION_TRADING_DAYS) // HALF_YEAR_TRADING_DAYS
    if period_count < 2:
        required = MIN_FORMATION_TRADING_DAYS + HALF_YEAR_TRADING_DAYS * 2
        raise RuntimeError(
            f"历史数据不足以建立半年递推验证和独立最终检验，至少需要 {required} 个共同交易日"
        )
    first = rows - period_count * HALF_YEAR_TRADING_DAYS
    periods = [
        (
            first + index * HALF_YEAR_TRADING_DAYS,
            first + (index + 1) * HALF_YEAR_TRADING_DAYS - 1,
        )
        for index in range(period_count)
    ]
    return periods[:-1], periods[-1]


def _validation_summary(folds: list[dict], params: dict) -> dict:
    metrics = [item["metrics"] for item in folds]
    returns = [item["horizon_equivalent_return"] for item in metrics]
    drawdowns = [abs(item["max_drawdown"]) for item in metrics]
    turnovers = [item["turnover"] for item in metrics]
    feasible = all(item["risk_pass"] for item in metrics)
    score = (statistics.median(returns) * 100
             - statistics.median(turnovers) * 0.35
             - max(drawdowns) * 20)
    if not feasible:
        score -= 100 + max(0.0, max(drawdowns) - params["risk_budget"]) * 1000
    return {
        "feasible": feasible,
        "score": round(score, 6),
        "fold_count": len(folds),
        "risk_pass_count": sum(item["risk_pass"] for item in metrics),
        "median_horizon_return": round(statistics.median(returns), 6),
        "worst_drawdown": round(-max(drawdowns), 6),
        "median_turnover": round(statistics.median(turnovers), 4),
        "fold_metrics": metrics,
    }


def _select_from_completed_periods(candidates: list[dict], completed_count: int) -> tuple[dict, dict]:
    """Select parameters using only half-year periods that have already ended."""
    if completed_count <= 0:
        return candidates[0], {
            "basis": "fixed_profile_baseline_before_first_half_year",
            "completed_periods": 0,
            "feasible_candidate_count": 1,
        }
    scored = []
    for candidate in candidates:
        summary = _validation_summary(
            candidate["fold_results"][:completed_count], candidate["params"]
        )
        scored.append((candidate, summary))
    feasible = [item for item in scored if item[1]["feasible"]]
    pool = feasible or scored
    selected, summary = max(pool, key=lambda item: item[1]["score"])
    return selected, {
        "basis": "completed_prior_half_years_only",
        "completed_periods": completed_count,
        "feasible_candidate_count": len(feasible),
        "selection_summary": summary,
    }


def _recursive_walk_forward_history(candidates: list[dict], data: dict,
                                    validation_windows: list[tuple[int, int]]) -> tuple[list[dict], dict]:
    """Replay which parameters would have been frozen at each half-year boundary."""
    history = []
    simulations = []
    previous_iteration = None
    for period_index, (start, end) in enumerate(validation_windows):
        selected, selection = _select_from_completed_periods(candidates, period_index)
        simulation = selected["fold_results"][period_index]
        simulations.append(simulation)
        history.append({
            "sequence": period_index + 1,
            "role": "ROLLING_OUT_OF_SAMPLE",
            "formation_start": data["dates"][0],
            "formation_end": data["dates"][start - 1],
            "evaluation_start": data["dates"][start],
            "evaluation_end": data["dates"][end],
            "selected_iteration": selected["iteration"],
            "parameter_updated": (
                previous_iteration is not None and previous_iteration != selected["iteration"]
            ),
            "selection": selection,
            "oos_metrics": simulation["metrics"],
        })
        previous_iteration = selected["iteration"]
    summary = _validation_summary(simulations, candidates[0]["params"])
    summary.update({
        "method": "anchored_expanding_half_year_walk_forward",
        "period_trading_days": HALF_YEAR_TRADING_DAYS,
        "no_future_data_in_selection": True,
    })
    return history, summary


def _activation_eligible(result: dict) -> bool:
    strategies = result.get("strategies", [])
    return len(strategies) == 3 and all(
        item.get("validation", {}).get("feasible")
        and item.get("validation", {}).get("recursive_walk_forward", {}).get("feasible")
        and item.get("holdout", {}).get("metrics", {}).get("risk_pass")
        and item.get("non_regression_pass")
        for item in strategies
    )


def _prediction_audit(data: dict, top_k: int, universe: list[dict] | None = None) -> dict:
    """Audit prequential probabilities without fitting on future outcomes."""
    probabilities = data.get("prediction_scores", {})
    dates = data["dates"]
    horizons = {1: [], 5: [], 20: []}
    one_day_points = []
    per_symbol: dict[str, list[int]] = {symbol: [] for symbol in data["symbols"]}
    for day_index, day in enumerate(dates[:-1]):
        if day_index >= 20:
            market_change = statistics.mean(
                data["closes"][symbol][day_index] /
                data["closes"][symbol][day_index - 20] - 1.0
                for symbol in data["symbols"]
            )
        else:
            market_change = 0.0
        regime = ("上涨阶段" if market_change > 0.03 else
                  "下跌阶段" if market_change < -0.03 else "震荡阶段")
        ranked = []
        for symbol in data["symbols"]:
            probability = probabilities.get(symbol, {}).get(day)
            if probability is not None:
                ranked.append((float(probability), symbol))
        ranked.sort(reverse=True)
        for probability, symbol in ranked:
            next_index = day_index + 1
            actual_up = data["closes"][symbol][next_index] > data["closes"][symbol][day_index]
            correct = int((probability >= 0.5) == actual_up)
            one_day_points.append({"probability": probability, "actual": int(actual_up),
                                   "correct": correct, "symbol": symbol, "regime": regime})
            per_symbol[symbol].append(correct)
        selected = ranked[:max(1, int(top_k))]
        for horizon in horizons:
            target_index = day_index + horizon
            if target_index >= len(dates):
                continue
            for _probability, symbol in selected:
                start_price = data["closes"][symbol][day_index]
                end_price = data["closes"][symbol][target_index]
                horizons[horizon].append(int(end_price > start_price))

    positives = [point for point in one_day_points if point["actual"]]
    negatives = [point for point in one_day_points if not point["actual"]]
    comparisons = wins = 0.0
    for positive in positives:
        for negative in negatives:
            comparisons += 1
            wins += (1.0 if positive["probability"] > negative["probability"] else
                     0.5 if positive["probability"] == negative["probability"] else 0.0)
    def interval(values: list[int]) -> dict | None:
        if not values:
            return None
        total, success, z = len(values), sum(values), 1.96
        rate = success / total
        denominator = 1 + z * z / total
        center = (rate + z * z / (2 * total)) / denominator
        margin = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / denominator
        return {"low": round(max(0.0, center - margin), 6),
                "high": round(min(1.0, center + margin), 6), "method": "wilson_95pct"}

    calibration = []
    for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
        bucket = [point for point in one_day_points
                  if lower <= point["probability"] < lower + 0.2]
        calibration.append({
            "probability_range": [lower, round(lower + 0.2, 1)],
            "sample_count": len(bucket),
            "average_probability": round(statistics.mean(
                point["probability"] for point in bucket
            ), 6) if bucket else None,
            "actual_up_rate": round(statistics.mean(
                point["actual"] for point in bucket
            ), 6) if bucket else None,
        })
    sector_map = {str(item.get("symbol")): list(item.get("sector_names") or ["未标明板块"])
                  for item in (universe or [])}
    sector_groups: dict[str, list[int]] = {}
    regime_groups: dict[str, list[int]] = {}
    for point in one_day_points:
        regime_groups.setdefault(point["regime"], []).append(point["correct"])
        for sector in sector_map.get(point["symbol"], ["未标明板块"]):
            sector_groups.setdefault(str(sector), []).append(point["correct"])

    def grouped_rows(groups: dict[str, list[int]]) -> list[dict]:
        return [
            {"name": name, "sample_count": len(values),
             "directional_accuracy": round(statistics.mean(values), 6) if values else None,
             "confidence_interval": interval(values)}
            for name, values in sorted(groups.items())
        ]

    return {
        "method": "prequential_probability_then_future_return_audit",
        "probability_horizon_days": 1,
        "sample_count": len(one_day_points),
        "directional_accuracy": round(statistics.mean(
            point["correct"] for point in one_day_points
        ), 6) if one_day_points else None,
        "brier_score": round(statistics.mean(
            (point["probability"] - point["actual"]) ** 2 for point in one_day_points
        ), 6) if one_day_points else None,
        "auc": round(wins / comparisons, 6) if comparisons else None,
        "directional_accuracy_confidence_interval": interval(
            [point["correct"] for point in one_day_points]
        ),
        "calibration_bins": calibration,
        "top_k_hit_rates": {
            f"{horizon}d": {
                "sample_count": len(values),
                "hit_rate": round(statistics.mean(values), 6) if values else None,
                "confidence_interval": interval(values),
                "definition": f"每日按当时可得概率选前 {max(1, int(top_k))} 只，{horizon} 个交易日后上涨",
            }
            for horizon, values in horizons.items()
        },
        "per_stock": [
            {"symbol": symbol, "sample_count": len(values),
             "directional_accuracy": round(statistics.mean(values), 6) if values else None}
            for symbol, values in per_symbol.items()
        ],
        "per_sector": grouped_rows(sector_groups),
        "per_market_regime": grouped_rows(regime_groups),
        "date_range": {"start": dates[0], "end": dates[-1]},
        "note": "5日和20日命中率衡量入选后是否上涨，不是5日或20日概率校准值。",
    }


def _insert_simulation(conn, experiment_id: int, candidate_id: int | None,
                       phase: str, profile: str, simulation: dict) -> None:
    conn.execute(
        """INSERT INTO strategy_simulations
           (experiment_id,candidate_id,phase,profile,data_start,data_end,metrics_json,
            curve_json,trades_json,assumptions_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (experiment_id, candidate_id, phase, profile, simulation["data_start"],
         simulation["data_end"], _dump(simulation["metrics"]), _dump(simulation["curve"]),
         _dump(simulation["trades"]), _dump(simulation["assumptions"]), _now()),
    )


def run_strategy_evolution(inputs: dict, normalized: bool = False) -> dict:
    """Evolve three bounded parameter sets and evaluate on an untouched holdout."""
    initialize()
    mandate = inputs if normalized else normalize_mandate(inputs)
    now = _now()
    mandate_key = f"mandate_{uuid.uuid4().hex}"
    experiment_key = f"experiment_{uuid.uuid4().hex}"
    with closing(connect()) as conn:
        cursor = conn.execute(
            """INSERT INTO investment_mandates
               (mandate_key,name,capital,horizon_months,target_return_pct,max_drawdown_pct,
                stop_loss_pct,take_profit_pct,trailing_stop_pct,
                sectors_json,universe_json,max_positions,take_profit_mode,max_iterations,
                execution_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mandate_key, mandate["name"], mandate["capital"], mandate["horizon_months"],
             mandate["target_return_pct"], mandate["max_drawdown_pct"],
             mandate.get("stop_loss_pct", 8), mandate.get("take_profit_pct", 20),
             mandate.get("trailing_stop_pct", 8),
             _dump(mandate["sectors"]), _dump(mandate["universe"]), mandate["max_positions"],
             mandate["take_profit_mode"], mandate["max_iterations"],
             _dump(mandate["execution"]), now, now),
        )
        mandate_id = int(cursor.lastrowid)
        cursor = conn.execute(
            """INSERT INTO strategy_evolution_runs
               (experiment_key,mandate_id,status,started_at) VALUES(?,?,'RUNNING',?)""",
            (experiment_key, mandate_id, now),
        )
        experiment_id = int(cursor.lastrowid)
        conn.commit()

    try:
        data = _load_aligned_universe(mandate)
        from .continuous_learning import build_online_probability_map
        data["prediction_scores"] = build_online_probability_map(data["symbols"])
        prediction_audit = _prediction_audit(
            data, mandate["max_positions"], mandate.get("universe", [])
        )
        validation_windows, holdout_window = _folds(data, mandate)
        strategies = []
        with closing(connect()) as conn:
            for profile in ("aggressive", "balanced", "conservative"):
                candidates = []
                for iteration in range(mandate["max_iterations"]):
                    params = _candidate_parameters(profile, mandate, iteration)
                    fold_results = [simulate_portfolio(data, mandate, params, start, end)
                                    for start, end in validation_windows]
                    validation = _validation_summary(fold_results, params)
                    cursor = conn.execute(
                        """INSERT INTO strategy_evolution_candidates
                           (experiment_id,profile,iteration,params_json,validation_json,
                            feasible,score,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (experiment_id, profile, iteration + 1, _dump(params), _dump(validation),
                         int(validation["feasible"]), validation["score"], _now()),
                    )
                    candidate_id = int(cursor.lastrowid)
                    for fold_index, simulation in enumerate(fold_results, 1):
                        _insert_simulation(conn, experiment_id, candidate_id,
                                           f"WALK_FORWARD_{fold_index}", profile, simulation)
                    candidates.append({"id": candidate_id, "iteration": iteration + 1,
                                       "params": params, "validation": validation,
                                       "fold_results": fold_results})
                    conn.commit()
                feasible = [item for item in candidates if item["validation"]["feasible"]]
                pool = feasible or candidates
                selected = max(pool, key=lambda item: item["validation"]["score"])
                walk_forward_history, recursive_summary = _recursive_walk_forward_history(
                    candidates, data, validation_windows
                )
                selected["validation"] = {
                    **selected["validation"],
                    "recursive_walk_forward": recursive_summary,
                }
                conn.execute(
                    """UPDATE strategy_evolution_candidates
                       SET selected=1,validation_json=? WHERE id=?""",
                    (_dump(selected["validation"]), selected["id"]),
                )
                final = simulate_portfolio(data, mandate, selected["params"], *holdout_window)
                _insert_simulation(conn, experiment_id, selected["id"], "FINAL_HOLDOUT", profile, final)
                baseline_candidate = candidates[0]
                baseline_holdout = (final if selected["id"] == baseline_candidate["id"] else
                                    simulate_portfolio(data, mandate, baseline_candidate["params"],
                                                       *holdout_window))
                _insert_simulation(conn, experiment_id, baseline_candidate["id"],
                                   "BASELINE_HOLDOUT", profile, baseline_holdout)
                selected_metrics = final["metrics"]
                baseline_metrics = baseline_holdout["metrics"]
                non_regression = bool(
                    selected_metrics["risk_pass"] and (
                        not baseline_metrics["risk_pass"] or (
                            selected_metrics["horizon_equivalent_return"] >=
                            baseline_metrics["horizon_equivalent_return"] - 0.005 and
                            abs(selected_metrics["max_drawdown"]) <=
                            abs(baseline_metrics["max_drawdown"]) + 0.01
                        )
                    )
                )
                _final_selected, final_selection = _select_from_completed_periods(
                    candidates, len(validation_windows)
                )
                walk_forward_history.append({
                    "sequence": len(walk_forward_history) + 1,
                    "role": "FINAL_UNTOUCHED_HOLDOUT",
                    "formation_start": data["dates"][0],
                    "formation_end": data["dates"][holdout_window[0] - 1],
                    "evaluation_start": data["dates"][holdout_window[0]],
                    "evaluation_end": data["dates"][holdout_window[1]],
                    "selected_iteration": selected["iteration"],
                    "parameter_updated": bool(
                        walk_forward_history and
                        walk_forward_history[-1]["selected_iteration"] != selected["iteration"]
                    ),
                    "selection": final_selection,
                    "oos_metrics": final["metrics"],
                    "opened_once_after_parameter_selection": True,
                })
                strategies.append({
                    "profile": profile,
                    "label": PROFILE_LABELS[profile],
                    "selected_iteration": selected["iteration"],
                    "parameters": selected["params"],
                    "validation": selected["validation"],
                    "walk_forward_history": walk_forward_history,
                    "holdout": final,
                    "baseline": {
                        "iteration": baseline_candidate["iteration"],
                        "parameters": baseline_candidate["params"],
                        "validation": baseline_candidate["validation"],
                        "holdout": baseline_holdout,
                    },
                    "non_regression_pass": non_regression,
                    "validation_score_improvement": round(
                        selected["validation"]["score"] -
                        baseline_candidate["validation"]["score"], 6),
                    "candidate_count": len(candidates),
                    "feasible_candidate_count": len(feasible),
                })
                conn.commit()

            activation_eligible = _activation_eligible({"strategies": strategies})
            result = {
                "status": "SUCCESS",
                "experiment_key": experiment_key,
                "mandate_key": mandate_key,
                "mandate": mandate,
                "data": {
                    "start": data["data_start"], "end": data["data_end"], "rows": data["rows"],
                    "snapshot_at": now,
                    "rolling_window": {
                        "requested_years": data.get("requested_window_years", 3),
                        "requested_trading_days": data.get("requested_window_days", 756),
                        "effective_trading_days": data["rows"],
                        "raw_available_start": data.get("raw_data_start", data["data_start"]),
                        "raw_available_rows": data.get("raw_rows", data["rows"]),
                        "queue_rule": "append_latest_trading_day_and_drop_oldest_outside_window",
                        "warmup_disclosed": True,
                    },
                    "training_range": {
                        "start": data["dates"][0],
                        "end": data["dates"][validation_windows[0][0] - 1],
                    },
                    "validation_windows": [{"start": data["dates"][start],
                                            "end": data["dates"][end]}
                                           for start, end in validation_windows],
                    "final_holdout": {"start": data["dates"][holdout_window[0]],
                                      "end": data["dates"][holdout_window[1]]},
                    "half_year_walk_forward": {
                        "method": "anchored_expanding_half_year_walk_forward",
                        "period_trading_days": HALF_YEAR_TRADING_DAYS,
                        "period_count": len(validation_windows) + 1,
                        "validation_period_count": len(validation_windows),
                        "final_period_is_untouched_holdout": True,
                        "selection_uses_completed_periods_only": True,
                        "update_rule": (
                            "每个半年开始前只使用已经结束的半年结果选择参数；本期结果只参与下一期更新"
                        ),
                    },
                    "universe_audit": [
                        {"symbol": item["symbol"], "name": item.get("name", item["symbol"]),
                         "included": True, "reason": "共同交易日与数据覆盖满足回测要求"}
                        for item in mandate["universe"]
                    ],
                },
                "strategies": strategies,
                "prediction_audit": prediction_audit,
                "activation_eligible": activation_eligible,
                "activation_requires_human_approval": True,
                "order_execution": False,
                "limitations": [
                    "目标收益是软目标，不是收益承诺或硬卖出线。",
                    "本层只接收已冻结的候选池；板块扩展和候选筛选由量化组合工作流完成。",
                    "预测因子采用逐日先预测、后见结果再更新的 prequential 序列，不使用未来标签。",
                    "策略参数按126个交易日一期递推，本期结果只能用于下一期选参。",
                    "涨跌停采用授权书中的统一比例近似，未自动识别不同板块和证券例外。",
                    "历史回测不能代表未来表现；最终留出集只用于一次策略审查。",
                ],
            }
            conn.execute(
                """UPDATE strategy_evolution_runs SET status='SUCCESS',data_snapshot_at=?,
                   iteration_count=?,result_json=?,finished_at=? WHERE id=?""",
                (data["data_end"], mandate["max_iterations"] * 3, _dump(result), _now(),
                 experiment_id),
            )
            conn.commit()

        output = Path(DATA_LAKE) / "research" / "strategy_evolution" / f"{experiment_key}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_dump(result), encoding="utf-8")
        result["artifact_path"] = str(output)
        return result
    except Exception as exc:
        with closing(connect()) as conn:
            conn.execute(
                "UPDATE strategy_evolution_runs SET status='FAILED',error=?,finished_at=? WHERE id=?",
                (str(exc), _now(), experiment_id),
            )
            conn.commit()
        raise


def review_strategy_experiment(experiment_key: str) -> dict:
    initialize()
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT r.*,m.mandate_key,m.name FROM strategy_evolution_runs r
               JOIN investment_mandates m ON m.id=r.mandate_id
               WHERE r.experiment_key=?""", (str(experiment_key),),
        ).fetchone()
    if not row:
        raise ValueError("策略实验不存在")
    item = dict(row)
    result = _load(item.pop("result_json"), {})
    result["activation_eligible"] = _activation_eligible(result)
    return {
        "experiment_key": item["experiment_key"], "status": item["status"],
        "mandate_key": item["mandate_key"], "name": item["name"],
        "started_at": item["started_at"], "finished_at": item["finished_at"],
        "result": result,
    }


def activate_strategy_experiment(experiment_key: str, approved_by: str) -> dict:
    """Activate a fully risk-qualified experiment after Harness approval."""
    actor = str(approved_by or "").strip()
    if not actor:
        raise PermissionError("策略版本激活必须记录批准人")
    initialize()
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT r.*,m.mandate_key,m.id AS mandate_id,m.name,m.capital,m.horizon_months,
                      m.target_return_pct,m.max_drawdown_pct,m.stop_loss_pct,m.take_profit_pct,
                      m.trailing_stop_pct,m.sectors_json,m.universe_json,
                      m.max_positions,m.take_profit_mode,m.max_iterations,m.execution_json
               FROM strategy_evolution_runs r JOIN investment_mandates m ON m.id=r.mandate_id
               WHERE r.experiment_key=?""", (str(experiment_key),),
        ).fetchone()
        if not row:
            raise ValueError("策略实验不存在")
        result = _load(row["result_json"], {})
        if row["status"] != "SUCCESS" or not _activation_eligible(result):
            raise ValueError("该实验未同时通过三档策略的样本外风险门禁，不能激活")
        existing = conn.execute(
            """SELECT * FROM strategy_evolution_versions
               WHERE experiment_id=? AND status='ACTIVE'""", (row["id"],),
        ).fetchone()
        if existing:
            return _decode_version(existing)
        mandate = {
            "mandate_key": row["mandate_key"], "name": row["name"],
            "capital": row["capital"], "horizon_months": row["horizon_months"],
            "target_return_pct": row["target_return_pct"],
            "max_drawdown_pct": row["max_drawdown_pct"],
            "stop_loss_pct": row["stop_loss_pct"],
            "take_profit_pct": row["take_profit_pct"],
            "trailing_stop_pct": row["trailing_stop_pct"],
            "sectors": _load(row["sectors_json"], []),
            "universe": _load(row["universe_json"], []),
            "max_positions": row["max_positions"], "take_profit_mode": row["take_profit_mode"],
            "max_iterations": row["max_iterations"],
            "execution": _load(row["execution_json"], {}),
        }
        version_key = f"strategy_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = _now()
        conn.execute(
            "UPDATE strategy_evolution_versions SET status='ARCHIVED' WHERE mandate_id=? AND status='ACTIVE'",
            (row["mandate_id"],),
        )
        cursor = conn.execute(
            """INSERT INTO strategy_evolution_versions
               (version_key,mandate_id,experiment_id,status,mandate_json,strategies_json,
                approved_by,created_at,activated_at)
               VALUES(?,?,?,'ACTIVE',?,?,?,?,?)""",
            (version_key, row["mandate_id"], row["id"], _dump(mandate),
             _dump(result["strategies"]), actor[:160], now, now),
        )
        created = conn.execute(
            "SELECT * FROM strategy_evolution_versions WHERE id=?", (cursor.lastrowid,),
        ).fetchone()
        conn.commit()
    return _decode_version(created)


def _decode_version(row) -> dict:
    item = dict(row)
    item["mandate"] = _load(item.pop("mandate_json"), {})
    item["strategies"] = _load(item.pop("strategies_json"), [])
    return item


def strategy_evolution_payload(limit: int = 20) -> dict:
    initialize()
    with closing(connect()) as conn:
        experiments = []
        for row in conn.execute(
            """SELECT r.experiment_key,r.status,r.data_snapshot_at,r.iteration_count,
                      r.started_at,r.finished_at,r.error,m.mandate_key,m.name,m.universe_json,
                      r.result_json FROM strategy_evolution_runs r
               JOIN investment_mandates m ON m.id=r.mandate_id
               ORDER BY r.id DESC LIMIT ?""", (max(1, min(int(limit), 100)),),
        ):
            item = dict(row)
            result = _load(item.pop("result_json"), {})
            item["universe"] = _load(item.pop("universe_json"), [])
            item["activation_eligible"] = _activation_eligible(result)
            item["strategies"] = [{
                "profile": strategy.get("profile"), "label": strategy.get("label"),
                "holdout_metrics": strategy.get("holdout", {}).get("metrics", {}),
                "selected_iteration": strategy.get("selected_iteration"),
            } for strategy in result.get("strategies", [])]
            experiments.append(item)
        versions = [_decode_version(row) for row in conn.execute(
            "SELECT * FROM strategy_evolution_versions ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        )]
    return {
        "experiments": experiments,
        "versions": versions,
        "execution_defaults": DEFAULT_EXECUTION,
        "automatic_activation": False,
        "order_execution": False,
    }
