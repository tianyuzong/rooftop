"""Deterministic analysis primitives.

The module intentionally contains no order execution capability.  Signals are
decision-support outputs and retain their model inputs for auditability.
"""

from dataclasses import asdict, dataclass
from math import sqrt
from statistics import mean, pstdev
from typing import Iterable, List, Mapping


@dataclass(frozen=True)
class RiskPolicy:
    market: str
    asset_type: str
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    max_position_pct: float


POLICIES = {
    ("CN", "ETF"): RiskPolicy("CN", "ETF", 0.08, 0.15, 0.07, 0.25),
    ("US", "EQUITY"): RiskPolicy("US", "EQUITY", 0.10, 0.20, 0.08, 0.15),
    ("HK", "EQUITY"): RiskPolicy("HK", "EQUITY", 0.12, 0.22, 0.10, 0.15),
    ("JP", "EQUITY"): RiskPolicy("JP", "EQUITY", 0.10, 0.18, 0.08, 0.15),
    ("GLOBAL", "COMMODITY"): RiskPolicy("GLOBAL", "COMMODITY", 0.09, 0.18, 0.08, 0.20),
}
DEFAULT_POLICY = RiskPolicy("DEFAULT", "DEFAULT", 0.10, 0.18, 0.08, 0.15)


def policy_for(market: str, asset_type: str) -> RiskPolicy:
    return POLICIES.get((market.upper(), asset_type.upper()), DEFAULT_POLICY)


def calculate_risk_lines(
    cost_price: float,
    current_price: float,
    highest_since_entry: float,
    market: str,
    asset_type: str,
) -> Mapping[str, float]:
    if min(cost_price, current_price, highest_since_entry) <= 0:
        raise ValueError("price inputs must be positive")
    policy = policy_for(market, asset_type)
    fixed_stop = cost_price * (1 - policy.stop_loss_pct)
    trailing_stop = highest_since_entry * (1 - policy.trailing_stop_pct)
    return {
        "cost": round(cost_price, 4),
        "stop_loss": round(max(fixed_stop, trailing_stop), 4),
        "take_profit": round(cost_price * (1 + policy.take_profit_pct), 4),
        "buy_watch": round(cost_price * (1 - policy.stop_loss_pct / 2), 4),
        "current": round(current_price, 4),
    }


def evaluate_discipline(current_price: float, lines: Mapping[str, float]) -> Mapping[str, str]:
    if current_price <= lines["stop_loss"]:
        return {"action": "SELL_REVIEW", "severity": "critical", "reason": "已触发止损红线"}
    if current_price >= lines["take_profit"]:
        return {"action": "TAKE_PROFIT_REVIEW", "severity": "warning", "reason": "已触发止盈线"}
    if current_price <= lines["buy_watch"]:
        return {"action": "BUY_RESEARCH", "severity": "opportunity", "reason": "进入绿线研究区，须完成验证后再决策"}
    return {"action": "HOLD_DISCIPLINE", "severity": "normal", "reason": "价格位于纪律区间内"}


def market_risk(prices: Iterable[float]) -> Mapping[str, float]:
    values: List[float] = list(prices)
    if len(values) < 3 or any(v <= 0 for v in values):
        raise ValueError("at least three positive prices are required")
    returns = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)
    volatility = pstdev(returns) * sqrt(252) if len(returns) > 1 else 0.0
    momentum = values[-1] / values[max(0, len(values) - 21)] - 1
    return {
        "annualized_volatility": round(volatility, 4),
        "max_drawdown": round(max_drawdown, 4),
        "momentum_20d": round(momentum, 4),
        "mean_daily_return": round(mean(returns), 6),
    }


def policy_catalog():
    return [asdict(policy) for policy in POLICIES.values()]
