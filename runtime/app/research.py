"""Local factor research and analysis-only backtesting.

AKShare-derived market bars are read from the local system of record. AKQuant
provides the expression engine and event-driven backtester. Nothing in this
module can connect to a broker or submit a real order.
"""

from __future__ import annotations

import json
import math
import statistics
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import DATA_LAKE, connect
from .strategy_evolution import DEFAULT_EXECUTION


STRATEGIES = (
    {
        "key": "ma_trend_20_60", "name": "20/60 日均线趋势", "category": "趋势",
        "description": "只用前一交易日及更早数据；MA20 高于 MA60 入场，反向退出。",
        "implementation": "akquant_functional_ma", "factors": ("momentum_20d",),
        "risk": dict(stop_loss_pct=.07, take_profit_pct=.20, trailing_stop_pct=.08,
                     max_position_pct=.35, max_drawdown_pct=.16, max_daily_loss_pct=.035,
                     turnover_limit=8.0, liquidity_rule="近20日平均成交额不足2000万元时禁止产生新信号"),
    },
    {
        "key": "mean_reversion_z20", "name": "20 日价格偏离反转", "category": "均值回归",
        "description": "昨日价格低于 MA20 一个标准差时研究入场，回到均线退出。",
        "implementation": "akquant_functional_zscore", "factors": ("price_reversion_z20", "volatility_20d"),
        "risk": dict(stop_loss_pct=.05, take_profit_pct=.10, trailing_stop_pct=.04,
                     max_position_pct=.25, max_drawdown_pct=.10, max_daily_loss_pct=.025,
                     turnover_limit=12.0, liquidity_rule="近20日平均成交额不足3000万元时禁止产生新信号"),
    },
    {
        "key": "breakout_20", "name": "20 日高点突破", "category": "突破",
        "description": "昨日收盘突破此前20日高点时研究入场，跌破10日低点退出。",
        "implementation": "akquant_functional_breakout", "factors": ("momentum_20d", "volume_acceleration_5d"),
        "risk": dict(stop_loss_pct=.06, take_profit_pct=.18, trailing_stop_pct=.07,
                     max_position_pct=.30, max_drawdown_pct=.14, max_daily_loss_pct=.03,
                     turnover_limit=10.0, liquidity_rule="突破日成交量需高于20日均量，否则不产生新信号"),
    },
)

FACTORS = (
    ("momentum_20d", "20 日动量", "动量", "Delta(Close, 20) / Delay(Close, 20)", 1,
     "20 日价格变化率，正向因子。"),
    ("price_reversion_z20", "20 日价格反转 Z 值", "反转", "(Ts_Mean(Close, 20) - Close) / Ts_Std(Close, 20)", 1,
     "价格越低于20日均线，数值越高。"),
    ("volatility_20d", "20 日实现波动", "风险", "Ts_Std(Close / Delay(Close, 1) - 1, 20)", -1,
     "日收益率20日标准差，风险惩罚方向。"),
    ("volume_acceleration_5d", "5 日量能加速度", "量价", "Delta(Volume, 5) / Ts_Mean(Volume, 20)", 1,
     "5日成交量变化相对20日均量。"),
)

FACTOR_PROFILES = {
    "momentum_20d": {
        "advantages": ["趋势延续期逻辑直观", "表达式简单、可审计", "与趋势策略容易组合"],
        "limitations": ["震荡期容易反复失效", "急跌时回撤可能放大", "对回看窗口较敏感"],
    },
    "price_reversion_z20": {
        "advantages": ["适合寻找短期过度悲观", "与逆向情绪纪律一致", "信号尺度经过波动标准化"],
        "limitations": ["下跌趋势中可能持续接刀", "均值本身会移动", "必须配合止损和基本面排雷"],
    },
    "volatility_20d": {
        "advantages": ["可直接作为风险惩罚项", "有助于控制组合波动", "不依赖价格绝对水平"],
        "limitations": ["低波动不等于低风险", "突发事件前可能失真", "单独使用通常不是买入信号"],
    },
    "volume_acceleration_5d": {
        "advantages": ["能识别资金关注度变化", "可作为突破信号确认", "和纯价格因子互补"],
        "limitations": ["放量也可能对应出货", "不同标的成交口径不完全一致", "容易受单日异常成交干扰"],
    },
}

FACTOR_LIBRARY = (
    {"group": "technical", "group_label": "技术因子", "key": "ma_trend_20_60",
     "name": "20/60 日均线趋势", "status": "CALCULATED", "status_label": "可计算",
     "formula": "MA20 / MA60 - 1", "data": "前复权日线"},
    {"group": "technical", "group_label": "技术因子", "key": "momentum_20d",
     "name": "20 日动量", "status": "CALCULATED", "status_label": "可计算",
     "formula": "Close / Delay(Close, 20) - 1", "data": "前复权日线"},
    {"group": "technical", "group_label": "技术因子", "key": "rsi_14",
     "name": "14 日 RSI", "status": "CALCULATED", "status_label": "可计算",
     "formula": "RSI(Close, 14)", "data": "前复权日线"},
    {"group": "technical", "group_label": "技术因子", "key": "volume_ratio_5_20",
     "name": "5/20 日量比", "status": "CALCULATED", "status_label": "可计算",
     "formula": "Mean(Volume, 5) / Mean(Volume, 20)", "data": "成交量"},
    {"group": "fundamental", "group_label": "基本面因子", "key": "growth_quality",
     "name": "收入与利润成长", "status": "CONDITIONAL", "status_label": "有财报时计算",
     "formula": "收入增速、净利润增速、扣非增速", "data": "公告日可见财报"},
    {"group": "fundamental", "group_label": "基本面因子", "key": "valuation_quality",
     "name": "估值与盈利质量", "status": "CONDITIONAL", "status_label": "有估值时计算",
     "formula": "PE、PB、ROE、ROIC", "data": "历史估值快照"},
    {"group": "fundamental", "group_label": "基本面因子", "key": "cashflow_safety",
     "name": "现金流与偿债安全", "status": "CONDITIONAL", "status_label": "有财报时计算",
     "formula": "经营现金流/利润、负债率、流动比率", "data": "公告日可见财报"},
    {"group": "alternative", "group_label": "另类因子", "key": "public_sentiment",
     "name": "公开信息情绪", "status": "CONDITIONAL", "status_label": "有材料时计算",
     "formula": "情绪分 × 置信度", "data": "新闻、公告和公开内容"},
    {"group": "alternative", "group_label": "另类因子", "key": "spatiotemporal_relation",
     "name": "时序与横截面关系", "status": "PLANNED", "status_label": "研究规划",
     "formula": "时序表示 + 股票关系图", "data": "扩大后的股票池"},
    {"group": "risk", "group_label": "风险因子", "key": "volatility_20d",
     "name": "20 日实现波动", "status": "CALCULATED", "status_label": "可计算",
     "formula": "Std(Return, 20) × √252", "data": "前复权日线"},
    {"group": "risk", "group_label": "风险因子", "key": "atr_14",
     "name": "14 日 ATR 比例", "status": "CALCULATED", "status_label": "可计算",
     "formula": "ATR(14) / Close", "data": "高低开收"},
    {"group": "risk", "group_label": "风险因子", "key": "liquidity_20d",
     "name": "20 日平均成交额", "status": "CALCULATED", "status_label": "可计算",
     "formula": "Mean(Amount, 20)", "data": "成交额"},
)

COMPOSITE_BASE_WEIGHTS = {
    "aggressive": {"technical": .45, "fundamental": .25, "alternative": .15, "risk": .15},
    "balanced": {"technical": .35, "fundamental": .40, "alternative": .10, "risk": .15},
    "conservative": {"technical": .20, "fundamental": .50, "alternative": .05, "risk": .25},
}

PROFILE_LABELS = {"aggressive": "激进", "balanced": "均衡", "conservative": "保守"}
GROUP_LABELS = {"technical": "技术", "fundamental": "基本面", "alternative": "情绪", "risk": "风险"}
FUNDAMENTAL_DIMENSION_LABELS = {
    "growth": "成长性", "quality": "盈利质量", "cashflow": "现金流",
    "safety": "财务安全", "value": "估值",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_research_catalog() -> None:
    now = utcnow()
    with closing(connect()) as conn:
        for item in STRATEGIES:
            conn.execute(
                """INSERT INTO strategies(strategy_key,name,category,description,implementation,
                   source_framework,version,status,created_at,updated_at) VALUES(?,?,?,?,?,'AKQuant 0.3.38','0.1','RESEARCH',?,?)
                   ON CONFLICT(strategy_key) DO UPDATE SET name=excluded.name,description=excluded.description,
                   implementation=excluded.implementation,updated_at=excluded.updated_at""",
                (item["key"], item["name"], item["category"], item["description"], item["implementation"], now, now),
            )
            strategy_id = conn.execute("SELECT id FROM strategies WHERE strategy_key=?", (item["key"],)).fetchone()[0]
            risk = item["risk"]
            stress = json.dumps({"gap_down": "按-8%跳空重算", "liquidity_freeze": "无法成交时不假定红线成交",
                                 "correlation_spike": "相关性升至0.9时重算组合回撤"}, ensure_ascii=False)
            conn.execute(
                """INSERT INTO strategy_risk_policies(strategy_id,stop_loss_pct,take_profit_pct,trailing_stop_pct,
                   max_position_pct,max_drawdown_pct,max_daily_loss_pct,turnover_limit,liquidity_rule,
                   stress_rules_json,version,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'0.1',?)
                   ON CONFLICT(strategy_id) DO UPDATE SET stop_loss_pct=excluded.stop_loss_pct,
                   take_profit_pct=excluded.take_profit_pct,trailing_stop_pct=excluded.trailing_stop_pct,
                   max_position_pct=excluded.max_position_pct,max_drawdown_pct=excluded.max_drawdown_pct,
                   max_daily_loss_pct=excluded.max_daily_loss_pct,turnover_limit=excluded.turnover_limit,
                   liquidity_rule=excluded.liquidity_rule,stress_rules_json=excluded.stress_rules_json,
                   updated_at=excluded.updated_at""",
                (strategy_id, risk["stop_loss_pct"], risk["take_profit_pct"], risk["trailing_stop_pct"],
                 risk["max_position_pct"], risk["max_drawdown_pct"], risk["max_daily_loss_pct"],
                 risk["turnover_limit"], risk["liquidity_rule"], stress, now),
            )
        for key, name, family, expression, direction, description in FACTORS:
            conn.execute(
                """INSERT INTO factors(factor_key,name,family,expression,direction,description,
                   source_framework,version,status,created_at) VALUES(?,?,?,?,?,?,'AKQuant FactorEngine 0.3.38','0.1','RESEARCH',?)
                   ON CONFLICT(factor_key) DO UPDATE SET name=excluded.name,expression=excluded.expression,
                   description=excluded.description""", (key, name, family, expression, direction, description, now),
            )
        for item in STRATEGIES:
            strategy_id = conn.execute("SELECT id FROM strategies WHERE strategy_key=?", (item["key"],)).fetchone()[0]
            for factor_key in item["factors"]:
                factor_id = conn.execute("SELECT id FROM factors WHERE factor_key=?", (factor_key,)).fetchone()[0]
                conn.execute("INSERT OR REPLACE INTO strategy_factors(strategy_id,factor_id,weight,role) VALUES(?,?,1,'SIGNAL')",
                             (strategy_id, factor_id))
        conn.commit()


def _daily_frame(symbol: str):
    import pandas as pd
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT trade_date AS date,open,high,low,close,COALESCE(volume,0) AS volume,
                      COALESCE(amount,0) AS amount FROM market_daily_bars
               WHERE asset_symbol=? ORDER BY trade_date""", (symbol,),
        ).fetchall()
    if not rows:
        raise ValueError(f"没有 {symbol} 的本地日线")
    frame = pd.DataFrame([dict(row) for row in rows])
    frame["date"] = pd.to_datetime(frame["date"])
    frame["symbol"] = symbol
    return frame


def _strategy_callbacks(strategy_key: str, risk: dict[str, float]):
    def initialize(ctx):
        ctx.strategy_key = strategy_key
        ctx.warmup_period = 62
        ctx.entry_price = 0.0
        ctx.peak_price = 0.0

    def on_bar(ctx, bar):
        history = ctx.get_history(count=62, symbol=bar.symbol, field="close")
        if len(history) < 62:
            return
        prior = history[:-1]
        position = ctx.get_position(bar.symbol)
        if position > 0:
            ctx.peak_price = max(ctx.peak_price, float(bar.close))
            pnl = float(bar.close) / ctx.entry_price - 1 if ctx.entry_price else 0
            trailing = float(bar.close) / ctx.peak_price - 1 if ctx.peak_price else 0
            if pnl <= -risk["stop_loss_pct"] or pnl >= risk["take_profit_pct"] or trailing <= -risk["trailing_stop_pct"]:
                ctx.close_position(bar.symbol)
                return
        enter = exit_ = False
        if strategy_key == "ma_trend_20_60":
            ma20, ma60 = prior[-20:].mean(), prior[-60:].mean()
            enter, exit_ = ma20 > ma60, ma20 < ma60
        elif strategy_key == "mean_reversion_z20":
            mean, std = prior[-20:].mean(), prior[-20:].std()
            enter, exit_ = prior[-1] < mean - std, prior[-1] >= mean
        elif strategy_key == "breakout_20":
            enter, exit_ = prior[-1] >= prior[-21:-1].max(), prior[-1] <= prior[-11:-1].min()
        if position <= 0 and enter:
            ctx.order_target_percent(symbol=bar.symbol, target_percent=risk["max_position_pct"])
            ctx.entry_price = float(bar.close)
            ctx.peak_price = float(bar.close)
        elif position > 0 and exit_:
            ctx.close_position(bar.symbol)
    return initialize, on_bar


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if hasattr(value, "item"):
        return _json_value(value.item())
    return str(value)


def _curve_points(series, max_points: int = 260) -> list[dict]:
    if series is None or len(series) == 0:
        return []
    step = max(1, math.ceil(len(series) / max_points))
    indices = list(range(0, len(series), step))
    if indices[-1] != len(series) - 1:
        indices.append(len(series) - 1)
    return [{"time": str(series.index[index])[:10], "value": round(float(series.iloc[index]), 6)} for index in indices]


def _normalized_buy_hold(frame) -> list[dict]:
    close = frame.set_index("date")["close"].astype(float)
    return _curve_points(close / close.iloc[0] * 100000)


def run_backtest(strategy_key: str, symbol: str = "512400") -> dict:
    import akquant as aq
    seed_research_catalog()
    frame = _daily_frame(symbol)
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT s.id,s.strategy_key,r.* FROM strategies s JOIN strategy_risk_policies r ON r.strategy_id=s.id
               WHERE s.strategy_key=?""", (strategy_key,),
        ).fetchone()
        if not row:
            raise ValueError("未知策略")
        policy = dict(row)
        started = utcnow()
        config = {
            "initial_cash": 100000,
            "commission_rate": DEFAULT_EXECUTION["commission_rate"],
            "minimum_commission": DEFAULT_EXECUTION["minimum_commission"],
            "sell_stamp_tax_rate": DEFAULT_EXECUTION["sell_stamp_tax_rate"],
            "slippage_rate": DEFAULT_EXECUTION["slippage_rate"],
            "lot_size": DEFAULT_EXECUTION["lot_size"],
            "t_plus_one": True,
            "signal_lag": "只用上一交易日及更早数据",
            "research_pipeline": "EXPERIMENTAL_SINGLE_ASSET",
            "comparable_to_formal_portfolio": False,
            "order_execution": False,
        }
        cursor = conn.execute(
            """INSERT INTO backtest_runs(strategy_id,asset_symbol,framework,data_start,data_end,started_at,status,config_json)
               VALUES(?,?,?,?,?,?,'RUNNING',?)""",
            (row["id"], symbol, "AKQuant 0.3.38", frame.date.min().date().isoformat(),
             frame.date.max().date().isoformat(), started, json.dumps(config, ensure_ascii=False)),
        )
        run_id = cursor.lastrowid
        conn.commit()
    try:
        initialize, on_bar = _strategy_callbacks(strategy_key, policy)
        result = aq.run_backtest(
            data=frame, strategy=on_bar, initialize=initialize, initial_cash=100000,
            commission_rate=DEFAULT_EXECUTION["commission_rate"],
            min_commission=DEFAULT_EXECUTION["minimum_commission"],
            stamp_tax_rate=DEFAULT_EXECUTION["sell_stamp_tax_rate"],
            slippage=DEFAULT_EXECUTION["slippage_rate"],
            lot_size=DEFAULT_EXECUTION["lot_size"],
            t_plus_one=True, show_progress=False,
            risk_config={"max_position_pct": policy["max_position_pct"],
                         "max_account_drawdown": policy["max_drawdown_pct"],
                         "max_daily_loss": policy["max_daily_loss_pct"]},
        )
        metrics = {str(index): _json_value(value) for index, value in result.metrics_df["value"].items()}
        curve = _curve_points(result.equity_curve)
        benchmark_curve = _normalized_buy_hold(frame)
        output = {"run_id": run_id, "strategy_key": strategy_key, "symbol": symbol, "metrics": metrics,
                  "data_start": frame.date.min().date().isoformat(), "data_end": frame.date.max().date().isoformat(),
                  "equity_curve": curve, "benchmark_curve": benchmark_curve}
        raw_path = DATA_LAKE / "research" / "backtests" / f"backtest_{run_id}.json"
        raw_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        with closing(connect()) as conn:
            conn.execute("UPDATE backtest_runs SET finished_at=?,status='SUCCESS',metrics_json=?,raw_path=? WHERE id=?",
                         (utcnow(), json.dumps(metrics, ensure_ascii=False), str(raw_path), run_id))
            conn.execute("UPDATE backtest_runs SET equity_json=? WHERE id=?",
                         (json.dumps({"strategy": curve, "benchmark": benchmark_curve}, ensure_ascii=False), run_id))
            conn.commit()
        return output
    except Exception as exc:
        with closing(connect()) as conn:
            conn.execute("UPDATE backtest_runs SET finished_at=?,status='FAILED',error=? WHERE id=?", (utcnow(), str(exc), run_id))
            conn.commit()
        raise


def run_factor(factor_key: str) -> dict:
    import pandas as pd
    from akquant.data import ParquetDataCatalog
    from akquant.factor import FactorEngine
    seed_research_catalog()
    catalog_path = DATA_LAKE / "research" / "factors" / "akquant_catalog"
    catalog = ParquetDataCatalog(root_path=str(catalog_path))
    with closing(connect()) as conn:
        factor = conn.execute("SELECT * FROM factors WHERE factor_key=?", (factor_key,)).fetchone()
        symbols = [row[0] for row in conn.execute("SELECT DISTINCT asset_symbol FROM market_daily_bars ORDER BY asset_symbol")]
        if not factor:
            raise ValueError("未知因子")
        cursor = conn.execute(
            "INSERT INTO factor_runs(factor_id,universe,started_at,status,params_json) VALUES(?,?,?,'RUNNING',?)",
            (factor["id"], ",".join(symbols), utcnow(), json.dumps({"forward_days": 5}, ensure_ascii=False)),
        )
        run_id = cursor.lastrowid
        conn.commit()
    try:
        market = []
        for symbol in symbols:
            frame = _daily_frame(symbol)
            market.append(frame[["date", "symbol", "close"]].copy())
            catalog.write(symbol, frame.set_index("date"))
        result = FactorEngine(catalog).run(factor["expression"]).to_pandas()
        prices = pd.concat(market, ignore_index=True).sort_values(["symbol", "date"])
        prices["forward_return_5d"] = prices.groupby("symbol")["close"].shift(-5) / prices["close"] - 1
        prices["forward_return_1d"] = prices.groupby("symbol")["close"].shift(-1) / prices["close"] - 1
        evaluated = result.merge(prices[["date", "symbol", "forward_return_5d", "forward_return_1d"]],
                                 on=["date", "symbol"], how="left").dropna()
        daily_ic = evaluated.groupby("date").apply(
            lambda group: group["factor_value"].corr(group["forward_return_5d"]) if len(group) >= 3 else float("nan"),
            include_groups=False,
        ).dropna()
        daily_rank_ic = evaluated.groupby("date").apply(
            lambda group: group["factor_value"].rank().corr(group["forward_return_5d"].rank()) if len(group) >= 3 else float("nan"),
            include_groups=False,
        ).dropna()
        direction = int(factor["direction"])
        evaluated["score"] = evaluated["factor_value"] * direction
        evaluated["rank"] = evaluated.groupby("date")["score"].rank(method="first", ascending=False)
        selected = evaluated[evaluated["rank"] == 1].sort_values("date").copy()
        selected["strategy_return"] = selected["forward_return_1d"].clip(-.15, .15)
        selected["equity"] = (1 + selected["strategy_return"].fillna(0)).cumprod() * 100
        factor_curve = _curve_points(selected.set_index("date")["equity"])
        simulated_return = float(selected["equity"].iloc[-1] - 100) if len(selected) else None
        selected_win_rate = float((selected["strategy_return"] > 0).mean() * 100) if len(selected) else None
        metrics = {"ic": _json_value(daily_ic.mean()), "rank_ic": _json_value(daily_rank_ic.mean()),
                   "ic_observations": int(len(daily_ic)), "forward_days": 5,
                   "simulated_return_pct": simulated_return, "selected_win_rate_pct": selected_win_rate,
                   "note": "曲线为每日持有因子排名第一标的至下一交易日的研究模拟；仅4标的小样本，不足以支持投资结论"}
        metrics = {key: _json_value(value) for key, value in metrics.items()}
        output = {"run_id": run_id, "factor_key": factor_key, "expression": factor["expression"],
                  "row_count": int(len(result)), "metrics": metrics, "equity_curve": factor_curve}
        raw_path = DATA_LAKE / "research" / "factors" / f"factor_{run_id}.json"
        raw_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        coverage = float(result["factor_value"].notna().mean()) if len(result) else 0
        with closing(connect()) as conn:
            conn.execute(
                """UPDATE factor_runs SET finished_at=?,status='SUCCESS',row_count=?,ic=?,rank_ic=?,coverage=?,
                   metrics_json=?,raw_path=? WHERE id=?""",
                (utcnow(), len(result), metrics["ic"], metrics["rank_ic"], coverage,
                 json.dumps(metrics, ensure_ascii=False), str(raw_path), run_id),
            )
            conn.commit()
        return output
    except Exception as exc:
        with closing(connect()) as conn:
            conn.execute("UPDATE factor_runs SET finished_at=?,status='FAILED',error=? WHERE id=?", (utcnow(), str(exc), run_id))
            conn.commit()
        raise


def _bounded(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def _mean(values) -> float | None:
    available = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return statistics.fmean(available) if available else None


def _factor_value(key: str, name: str, value: float | None, score: float | None,
                  value_label: str, as_of: str | None, status_note: str = "") -> dict:
    available = value is not None and score is not None
    return {
        "key": key, "name": name, "available": available,
        "value": round(float(value), 8) if value is not None else None,
        "value_label": value_label if available else "暂无数据",
        "score": round(_bounded(score), 6) if score is not None else None,
        "as_of": as_of, "status_note": status_note or ("已计算" if available else "数据不足"),
    }


def _pct(value: float | None) -> str:
    return f"{float(value) * 100:.1f}%" if value is not None else "暂无数据"


def _amount(value: float | None) -> str:
    if value is None:
        return "暂无数据"
    if value >= 1e8:
        return f"{value / 1e8:.2f} 亿元"
    if value >= 1e4:
        return f"{value / 1e4:.0f} 万元"
    return f"{value:.0f} 元"


def _market_factor_groups(rows: list[dict]) -> dict:
    closes = [float(row["close"]) for row in rows]
    volumes = [float(row.get("volume") or 0) for row in rows]
    amounts = [float(row.get("amount") or 0) for row in rows]
    as_of = rows[-1]["trade_date"] if rows else None
    enough_60 = len(rows) >= 60

    ma20 = _mean(closes[-20:]) if len(closes) >= 20 else None
    ma60 = _mean(closes[-60:]) if enough_60 else None
    trend = ma20 / ma60 - 1 if ma20 and ma60 else None
    momentum = closes[-1] / closes[-21] - 1 if len(closes) >= 21 and closes[-21] else None

    rsi = None
    if len(closes) >= 15:
        changes = [closes[index] - closes[index - 1] for index in range(len(closes) - 14, len(closes))]
        gain = _mean(max(change, 0) for change in changes) or 0
        loss = _mean(max(-change, 0) for change in changes) or 0
        rsi = 100.0 if gain and not loss else (50.0 if not gain and not loss else 100 - 100 / (1 + gain / loss))

    volume_5 = _mean(volumes[-5:]) if len(volumes) >= 5 else None
    volume_20 = _mean(volumes[-20:]) if len(volumes) >= 20 else None
    volume_ratio = volume_5 / volume_20 if volume_5 is not None and volume_20 else None

    returns = [closes[index] / closes[index - 1] - 1 for index in range(max(1, len(closes) - 20), len(closes))
               if closes[index - 1]]
    annualized_volatility = statistics.pstdev(returns) * math.sqrt(252) if len(returns) >= 10 else None

    true_ranges = []
    for index in range(max(1, len(rows) - 14), len(rows)):
        high, low, previous = float(rows[index]["high"]), float(rows[index]["low"]), closes[index - 1]
        true_ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    atr_ratio = (_mean(true_ranges) / closes[-1] if true_ranges and closes[-1] else None)
    positive_amounts = [value for value in amounts[-20:] if value > 0]
    average_amount = _mean(positive_amounts) if positive_amounts else None
    liquidity_score = None
    if average_amount:
        low, high = math.log10(2e7), math.log10(2e8)
        liquidity_score = _bounded(2 * (math.log10(average_amount) - low) / (high - low) - 1)

    technical_factors = [
        _factor_value("ma_trend_20_60", "20/60 日均线趋势", trend,
                      _bounded(trend / .10) if trend is not None else None, _pct(trend), as_of),
        _factor_value("momentum_20d", "20 日动量", momentum,
                      _bounded(momentum / .20) if momentum is not None else None, _pct(momentum), as_of),
        _factor_value("rsi_14", "14 日 RSI", rsi,
                      _bounded((rsi - 50) / 25) if rsi is not None else None,
                      f"{rsi:.1f}" if rsi is not None else "暂无数据", as_of),
        _factor_value("volume_ratio_5_20", "5/20 日量比", volume_ratio,
                      _bounded(volume_ratio - 1) if volume_ratio is not None else None,
                      f"{volume_ratio:.2f}" if volume_ratio is not None else "暂无数据", as_of),
    ]
    risk_factors = [
        _factor_value("volatility_20d", "20 日实现波动", annualized_volatility,
                      _bounded(1 - 2 * (annualized_volatility - .10) / .40)
                      if annualized_volatility is not None else None,
                      _pct(annualized_volatility), as_of, "低波动得分更高"),
        _factor_value("atr_14", "14 日 ATR 比例", atr_ratio,
                      _bounded(1 - 2 * (atr_ratio - .01) / .04) if atr_ratio is not None else None,
                      _pct(atr_ratio), as_of, "波幅越低，风险得分越高"),
        _factor_value("liquidity_20d", "20 日平均成交额", average_amount, liquidity_score,
                      _amount(average_amount), as_of, "低于 2000 万元不通过流动性门槛"),
    ]
    return {
        "as_of": as_of, "row_count": len(rows), "momentum": momentum, "trend": trend,
        "annualized_volatility": annualized_volatility, "average_amount": average_amount,
        "groups": {
            "technical": {"score": _mean(item["score"] for item in technical_factors),
                          "factors": technical_factors},
            "risk": {"score": _mean(item["score"] for item in risk_factors),
                     "factors": risk_factors},
        },
    }


def _market_rows(conn, symbol: str) -> list[dict]:
    rows = conn.execute(
        """SELECT b.trade_date,b.open,b.high,b.low,b.close,b.volume,b.amount
           FROM market_daily_bars b
           WHERE b.asset_symbol=? AND b.adjust_mode='qfq'
             AND b.source_id=(SELECT b2.source_id FROM market_daily_bars b2
               JOIN data_sources d2 ON d2.id=b2.source_id
               WHERE b2.asset_symbol=b.asset_symbol AND b2.trade_date=b.trade_date
                 AND b2.adjust_mode=b.adjust_mode ORDER BY d2.priority LIMIT 1)
           ORDER BY b.trade_date DESC LIMIT 120""", (symbol,),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def _scorecard_universe(conn, limit: int = 8) -> list[dict]:
    output, seen = [], set()
    watched = conn.execute(
        """SELECT symbol,name FROM comparison_watchlist
           WHERE instr(symbol,'.')=0 ORDER BY last_compared_at DESC LIMIT ?""", (limit,),
    ).fetchall()
    for row in watched:
        if conn.execute("SELECT 1 FROM market_daily_bars WHERE asset_symbol=? LIMIT 1", (row["symbol"],)).fetchone():
            output.append({"symbol": row["symbol"], "name": row["name"]})
            seen.add(row["symbol"])
    for symbol in ("600519", "000858", "300750"):
        if len(output) >= limit or symbol in seen:
            continue
        if not conn.execute("SELECT 1 FROM market_daily_bars WHERE asset_symbol=? LIMIT 1", (symbol,)).fetchone():
            continue
        asset = conn.execute("SELECT name FROM assets WHERE symbol=?", (symbol,)).fetchone()
        output.append({"symbol": symbol, "name": asset[0] if asset else symbol})
        seen.add(symbol)
    if not output:
        for row in conn.execute(
            """SELECT b.asset_symbol,COALESCE(a.name,b.asset_symbol) AS name
               FROM market_daily_bars b LEFT JOIN assets a ON a.symbol=b.asset_symbol
               WHERE instr(b.asset_symbol,'.')=0 GROUP BY b.asset_symbol ORDER BY b.asset_symbol LIMIT ?""", (limit,),
        ):
            output.append({"symbol": row["asset_symbol"], "name": row["name"]})
    return output


def _market_regime(items: list[dict]) -> dict:
    volatilities = [item["annualized_volatility"] for item in items if item["annualized_volatility"] is not None]
    momentums = [item["momentum"] for item in items if item["momentum"] is not None]
    trends = [item["trend"] for item in items if item["trend"] is not None]
    median_volatility = statistics.median(volatilities) if volatilities else None
    median_momentum = statistics.median(momentums) if momentums else None
    median_trend = statistics.median(trends) if trends else None
    if median_volatility is not None and median_volatility > .35:
        key, label, note = "high_volatility", "高波动环境", "提高风险因子权重，降低技术信号权重"
    elif median_momentum is not None and median_trend is not None and median_momentum > .05 and median_trend > 0:
        key, label, note = "trend", "趋势环境", "适度提高趋势与动量权重"
    else:
        key, label, note = "neutral", "中性 / 震荡环境", "沿用风险档位的基础权重"
    return {"key": key, "label": label, "note": note,
            "median_volatility": round(median_volatility, 6) if median_volatility is not None else None,
            "median_momentum": round(median_momentum, 6) if median_momentum is not None else None}


def _composite_weights(profile: str, regime: str) -> dict:
    weights = dict(COMPOSITE_BASE_WEIGHTS[profile])
    if regime == "high_volatility":
        weights["technical"] -= .05
        weights["risk"] += .05
    elif regime == "trend":
        weights["technical"] += .05
        weights["fundamental"] -= .03
        weights["risk"] -= .02
    return {key: round(value, 4) for key, value in weights.items()}


def _fundamental_group(snapshot: dict) -> dict:
    as_of = snapshot.get("notice_date") or snapshot.get("valuation_date")
    factors = []
    for key, label in FUNDAMENTAL_DIMENSION_LABELS.items():
        value = snapshot.get("dimensions", {}).get(key)
        factors.append(_factor_value(
            f"fundamental_{key}", label, value, value,
            f"标准分 {value:+.2f}" if value is not None else "暂无数据", as_of,
            "仅使用当时已公告的数据" if value is not None else "缺少对应财务或估值字段",
        ))
    return {"score": snapshot.get("score"), "factors": factors,
            "snapshot": {key: snapshot.get(key) for key in
                         ("report_date", "notice_date", "valuation_date", "coverage", "eligible", "reasons")}}


def _sentiment_group(conn, symbol: str, as_of: str) -> dict:
    row = conn.execute(
        """SELECT * FROM sentiment_daily WHERE symbol=? AND trade_date<=?
           ORDER BY trade_date DESC LIMIT 1""", (symbol, as_of),
    ).fetchone()
    if not row or not row["document_count"]:
        factor = _factor_value("public_sentiment", "公开信息情绪", None, None, "暂无数据", None,
                               "没有可核验的公开材料，不用 0 分代替")
        return {"score": None, "factors": [factor]}
    score = _bounded(float(row["score"]))
    confidence = max(0.0, min(1.0, float(row["confidence"])))
    effective = score * confidence
    factor = _factor_value(
        "public_sentiment", "公开信息情绪", score, effective,
        f"情绪 {score:+.2f} · 置信度 {confidence:.0%} · {row['document_count']} 条材料",
        row["trade_date"], "情绪分按置信度折减",
    )
    return {"score": effective, "factors": [factor]}


def composite_factor_payload(conn_factory=None) -> dict:
    from .fundamentals import fundamental_snapshot, load_fundamental_timelines

    conn_factory = conn_factory or connect
    with closing(conn_factory()) as conn:
        universe = _scorecard_universe(conn)
        market_items = []
        for item in universe:
            rows = _market_rows(conn, item["symbol"])
            if rows:
                market_items.append({**item, **_market_factor_groups(rows)})
    symbols = [item["symbol"] for item in market_items]
    timelines = load_fundamental_timelines(symbols, conn_factory) if symbols else {
        "reports": {}, "valuations": {}, "has_data": False,
    }
    regime = _market_regime(market_items)
    models = {}
    for profile in COMPOSITE_BASE_WEIGHTS:
        weights = _composite_weights(profile, regime["key"])
        scorecards = []
        with closing(conn_factory()) as conn:
            for item in market_items:
                fundamental = fundamental_snapshot(timelines, item["symbol"], item["as_of"], profile)
                groups = {
                    "technical": item["groups"]["technical"],
                    "fundamental": _fundamental_group(fundamental),
                    "alternative": _sentiment_group(conn, item["symbol"], item["as_of"]),
                    "risk": item["groups"]["risk"],
                }
                available = {key: value for key, value in groups.items() if value.get("score") is not None}
                coverage = sum(weights[key] for key in available)
                weighted = sum(float(value["score"]) * weights[key] for key, value in available.items())
                provisional = 50 + 50 * weighted / coverage if coverage else None
                missing_required = [key for key in ("technical", "fundamental", "risk") if key not in available]
                liquidity = item.get("average_amount")
                gates = [
                    {"key": "history", "label": "至少 60 个交易日", "status": "PASS" if item["row_count"] >= 60 else "FAIL",
                     "detail": f"当前 {item['row_count']} 日"},
                    {"key": "liquidity", "label": "20 日平均成交额不低于 2000 万元",
                     "status": "PASS" if liquidity is not None and liquidity >= 2e7 else ("FAIL" if liquidity is not None else "UNKNOWN"),
                     "detail": _amount(liquidity)},
                    {"key": "fundamental", "label": "基本面覆盖和质量门槛",
                     "status": "PASS" if fundamental.get("eligible") else ("FAIL" if fundamental.get("available") else "UNKNOWN"),
                     "detail": "；".join(fundamental.get("reasons") or ["已通过"])},
                ]
                gates_pass = all(gate["status"] == "PASS" for gate in gates)
                eligible = coverage >= .75 and not missing_required and gates_pass
                if eligible:
                    status_label = "可进入研究排序"
                elif "fundamental" in missing_required:
                    status_label = "缺少基本面数据"
                elif coverage < .75:
                    status_label = "数据覆盖不足"
                elif any(gate["status"] == "UNKNOWN" for gate in gates):
                    status_label = "风险数据不足"
                else:
                    status_label = "风险门槛未通过"
                group_rows = []
                for key in ("technical", "fundamental", "alternative", "risk"):
                    value = groups[key]
                    group_rows.append({
                        "key": key, "label": GROUP_LABELS[key], "weight": weights[key],
                        "available": value.get("score") is not None,
                        "score": round(float(value["score"]), 6) if value.get("score") is not None else None,
                        "contribution": round(float(value["score"]) * weights[key], 6)
                        if value.get("score") is not None else None,
                        "factors": value.get("factors", []), "snapshot": value.get("snapshot"),
                    })
                scorecards.append({
                    "symbol": item["symbol"], "name": item["name"], "data_asof": item["as_of"],
                    "score": round(provisional, 2) if eligible and provisional is not None else None,
                    "provisional_score": round(provisional, 2) if provisional is not None else None,
                    "coverage": round(coverage, 4), "eligible": eligible, "status_label": status_label,
                    "missing_groups": [GROUP_LABELS[key] for key in missing_required],
                    "groups": group_rows, "risk_gates": gates,
                })
        scorecards.sort(key=lambda card: (not card["eligible"], -(card["provisional_score"] or -1)))
        models[profile] = {
            "profile": profile, "profile_label": PROFILE_LABELS[profile], "weights": weights,
            "coverage_minimum": .75, "required_groups": ["技术", "基本面", "风险"],
            "regime": regime, "scorecards": scorecards,
            "note": "正式分数要求技术、基本面和风险三组齐全，并通过历史长度、流动性和基本面门槛。",
        }
    return {"models": models, "factor_catalog": list(FACTOR_LIBRARY),
            "regime": regime, "universe": symbols, "order_execution": False}


def strategy_lab_payload() -> dict:
    seed_research_catalog()
    with closing(connect()) as conn:
        strategies = [dict(row) for row in conn.execute(
            """SELECT s.*,r.stop_loss_pct,r.take_profit_pct,r.trailing_stop_pct,r.max_position_pct,
                      r.max_drawdown_pct,r.max_daily_loss_pct,r.turnover_limit,r.liquidity_rule,r.stress_rules_json,
                      (SELECT metrics_json FROM backtest_runs b WHERE b.strategy_id=s.id AND b.status='SUCCESS'
                       ORDER BY b.id DESC LIMIT 1) AS latest_metrics_json,
                      (SELECT asset_symbol FROM backtest_runs b WHERE b.strategy_id=s.id AND b.status='SUCCESS'
                       ORDER BY b.id DESC LIMIT 1) AS latest_symbol
               FROM strategies s JOIN strategy_risk_policies r ON r.strategy_id=s.id ORDER BY s.id"""
        )]
        factors = [dict(row) for row in conn.execute(
            """SELECT f.*,(SELECT ic FROM factor_runs x WHERE x.factor_id=f.id AND x.status='SUCCESS' ORDER BY x.id DESC LIMIT 1) AS ic,
                      (SELECT rank_ic FROM factor_runs x WHERE x.factor_id=f.id AND x.status='SUCCESS' ORDER BY x.id DESC LIMIT 1) AS rank_ic,
                      (SELECT row_count FROM factor_runs x WHERE x.factor_id=f.id AND x.status='SUCCESS' ORDER BY x.id DESC LIMIT 1) AS row_count,
                      (SELECT metrics_json FROM factor_runs x WHERE x.factor_id=f.id AND x.status='SUCCESS' ORDER BY x.id DESC LIMIT 1) AS latest_metrics_json,
                      (SELECT raw_path FROM factor_runs x WHERE x.factor_id=f.id AND x.status='SUCCESS' ORDER BY x.id DESC LIMIT 1) AS latest_raw_path
               FROM factors f ORDER BY f.id"""
        )]
        recent = [dict(row) for row in conn.execute(
            """SELECT b.id,s.strategy_key,s.name AS strategy,b.asset_symbol,b.data_start,b.data_end,b.status,
                      b.metrics_json,b.equity_json,b.error,b.started_at
               FROM backtest_runs b JOIN strategies s ON s.id=b.strategy_id ORDER BY b.id DESC LIMIT 20"""
        )]
    for item in strategies:
        item["latest_metrics"] = json.loads(item.pop("latest_metrics_json")) if item.get("latest_metrics_json") else None
        item["stress_rules"] = json.loads(item.pop("stress_rules_json"))
        latest_run = next((run for run in recent if run["strategy_key"] == item["strategy_key"] and run["status"] == "SUCCESS"), None)
        item["latest_curve"] = json.loads(latest_run["equity_json"]) if latest_run and latest_run.get("equity_json") else None
    for item in factors:
        item["latest_metrics"] = json.loads(item.pop("latest_metrics_json")) if item.get("latest_metrics_json") else None
        raw_path = item.pop("latest_raw_path", None)
        item["equity_curve"] = None
        if raw_path and Path(raw_path).exists():
            try:
                item["equity_curve"] = json.loads(Path(raw_path).read_text(encoding="utf-8")).get("equity_curve")
            except (OSError, json.JSONDecodeError):
                pass
        item.update(FACTOR_PROFILES.get(item["factor_key"], {"advantages": [], "limitations": []}))
    for item in recent:
        item["metrics"] = json.loads(item.pop("metrics_json")) if item.get("metrics_json") else None
        item["equity"] = json.loads(item.pop("equity_json")) if item.get("equity_json") else None
    composite = composite_factor_payload()
    return {"strategies": strategies, "factors": factors, "backtests": recent,
            "frameworks": ["AKQuant 0.3.38 FactorEngine", "AKQuant 0.3.38 run_backtest", "AKShare/本地落库行情"],
            "composite_models": composite["models"], "factor_catalog": composite["factor_catalog"],
            "market_regime": composite["regime"], "order_execution": False,
            "universe": composite["universe"]}
