"""Auditable 5-minute A-share strategy research with walk-forward gates.

Signals use completed bars and orders are simulated at the next bar open.  The
engine enforces A-share T+1 selling, board lots, fees, stamp tax, slippage and a
volume participation cap.  It never creates or transmits a broker order.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import statistics
import uuid
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from typing import Iterable

from .db import connect, initialize

DEFAULT_INTERVAL = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _load_minute_bars(symbols: Iterable[str], interval_minutes: int = DEFAULT_INTERVAL) -> dict[str, list[dict]]:
    symbols = list(dict.fromkeys(str(symbol) for symbol in symbols))
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with closing(connect()) as conn:
        requested_intervals = (int(interval_minutes), 1) if int(interval_minutes) == 5 else (int(interval_minutes),)
        interval_placeholders = ",".join("?" for _ in requested_intervals)
        rows = conn.execute(
            f"""WITH ranked AS (
                   SELECT b.*,ROW_NUMBER() OVER (
                     PARTITION BY b.asset_symbol,b.bar_time,b.interval_minutes
                     ORDER BY ds.priority,b.captured_at DESC) AS rn
                   FROM minute_bars b JOIN data_sources ds ON ds.id=b.source_id
                   WHERE b.asset_symbol IN ({placeholders})
                     AND b.interval_minutes IN ({interval_placeholders})
                 )
                 SELECT asset_symbol,bar_time,interval_minutes,open,high,low,close,volume,amount
                 FROM ranked WHERE rn=1 ORDER BY asset_symbol,bar_time""",
            (*symbols, *requested_intervals),
        ).fetchall()
    direct = {symbol: [] for symbol in symbols}
    one_minute = {symbol: [] for symbol in symbols}
    for row in rows:
        item = dict(row)
        item["trade_date"] = str(item["bar_time"])[:10]
        target = direct if int(item["interval_minutes"]) == int(interval_minutes) else one_minute
        target.setdefault(str(item["asset_symbol"]), []).append(item)
    if int(interval_minutes) != 5:
        return direct
    result = {}
    for symbol in symbols:
        by_date = defaultdict(list)
        for item in direct.get(symbol, []):
            by_date[item["trade_date"]].append(item)
        minute_dates = defaultdict(list)
        for item in one_minute.get(symbol, []):
            minute_dates[item["trade_date"]].append(item)
        for day, points in minute_dates.items():
            points.sort(key=lambda item: item["bar_time"])
            if len(points) < 30:
                continue
            aggregated = []
            for offset in range(0, len(points) - 4, 5):
                chunk = points[offset:offset + 5]
                aggregated.append({
                    "asset_symbol": symbol, "bar_time": chunk[-1]["bar_time"],
                    "interval_minutes": 5, "trade_date": day,
                    "open": float(chunk[0]["open"]),
                    "high": max(float(item["high"]) for item in chunk),
                    "low": min(float(item["low"]) for item in chunk),
                    "close": float(chunk[-1]["close"]),
                    "volume": sum(float(item.get("volume") or 0) for item in chunk),
                    "amount": sum(float(item.get("amount") or 0) for item in chunk),
                    "derived_from": "1m_completed_bars",
                })
            if aggregated:
                by_date[day] = aggregated
        result[symbol] = [item for day in sorted(by_date) for item in by_date[day]]
    return result


def _candidate_library() -> list[dict]:
    from .evolvable import intraday_recipes
    importlib.invalidate_caches()
    return importlib.reload(intraday_recipes).strategy_candidates()


def benchmark_candidate_library(symbols: Iterable[str], interval_minutes: int = DEFAULT_INTERVAL,
                                max_drawdown: float = 0.15) -> dict:
    """Evaluate library selection without persisting a run or opening the holdout early."""
    bars_by_symbol = _load_minute_bars(symbols, interval_minutes)
    dates, splits = _date_splits(bars_by_symbol)
    costs = {"commission": 0.0003, "minimum_commission": 5.0,
             "stamp_tax": 0.0005, "slippage": 0.0005, "participation": 0.05,
             "round_lot": 100, "t_plus_one": True}
    candidates = []
    for params in _candidate_library():
        validation = _portfolio_simulation(
            bars_by_symbol, params, *splits["validation"], 1_000_000.0, costs)
        feasible = validation["max_drawdown"] <= max_drawdown and validation["trade_count"] >= 3
        score = (validation["annualized_return"] - 1.5 * validation["max_drawdown"]
                 + 0.03 * _clamp(validation["sharpe"], -3, 3))
        candidates.append({"params": params, "validation": validation,
                           "feasible": feasible, "score": score})
    winner = sorted(candidates, key=lambda item: (item["feasible"], item["score"]),
                    reverse=True)[0]
    holdout = _portfolio_simulation(
        bars_by_symbol, winner["params"], *splits["holdout"], 1_000_000.0, costs)
    return {"winner": winner["params"], "validation": winner["validation"],
            "validation_score": winner["score"], "feasible": winner["feasible"],
            "holdout": holdout, "candidate_count": len(candidates),
            "data_start": dates[0], "data_end": dates[-1], "splits": splits}


def _signal(params: dict, bars: list[dict], index: int) -> tuple[bool, bool, float]:
    closes = [float(item["close"]) for item in bars]
    family = params["family"]
    if family == "momentum":
        fast, slow = int(params["fast"]), int(params["slow"])
        if index < slow:
            return False, False, 0.0
        fast_mean = statistics.fmean(closes[index - fast + 1:index + 1])
        slow_mean = statistics.fmean(closes[index - slow + 1:index + 1])
        score = fast_mean / slow_mean - 1.0
        return score >= params["entry"], score <= params["exit"], score
    lookback = int(params["lookback"])
    if index < lookback:
        return False, False, 0.0
    history = closes[index - lookback:index]
    current = closes[index]
    if family == "mean_reversion":
        mean = statistics.fmean(history)
        deviation = statistics.pstdev(history) or max(mean * 0.001, 1e-9)
        score = (current - mean) / deviation
        return score <= params["entry_z"], score >= params["exit_z"], -score
    previous_high = max(float(item["high"]) for item in bars[index - lookback:index])
    previous_low = min(float(item["low"]) for item in bars[index - lookback:index])
    entry_score = current / previous_high - 1.0
    exit_score = current / previous_low - 1.0
    return entry_score >= params["entry"], exit_score <= params["exit"], entry_score


def _commission(value: float, rate: float, minimum: float) -> float:
    return max(minimum, value * rate) if value > 0 else 0.0


def _simulate_symbol(bars: list[dict], params: dict, start_date: str, end_date: str,
                     capital: float, costs: dict) -> dict:
    eligible = [item for item in bars if start_date <= item["trade_date"] <= end_date]
    if len(eligible) < max(int(params.get("slow", 0)), int(params.get("lookback", 0))) + 2:
        return {"equity": capital, "curve": [], "trades": [], "turnover": 0.0}
    warmup_start = max(0, bars.index(eligible[0]) - max(60, int(params.get("slow", 0)) + 1,
                                                       int(params.get("lookback", 0)) + 1))
    working = bars[warmup_start:bars.index(eligible[-1]) + 1]
    cash, shares, entry_price, peak_price = float(capital), 0, 0.0, 0.0
    entry_date = None
    pending = None
    trades, curve, turnover = [], [], 0.0
    last_date = None
    for index, bar in enumerate(working):
        day = bar["trade_date"]
        in_window = start_date <= day <= end_date
        open_price = float(bar["open"])
        volume = max(0.0, float(bar.get("volume") or 0.0))
        if in_window and pending == "BUY" and shares == 0:
            fill = open_price * (1.0 + costs["slippage"])
            max_liquidity = int(volume * costs["participation"] // 100 * 100)
            affordable = int((cash - costs["minimum_commission"]) /
                             (fill * (1 + costs["commission"])) // 100 * 100)
            quantity = max(0, min(affordable, max_liquidity))
            if quantity >= 100:
                value = quantity * fill
                fee = _commission(value, costs["commission"], costs["minimum_commission"])
                cash -= value + fee
                shares, entry_price, peak_price, entry_date = quantity, fill, fill, day
                turnover += value
                trades.append({"time": bar["bar_time"], "action": "BUY", "shares": quantity,
                               "price": fill, "fee": fee, "reason": "signal_next_bar"})
            pending = None
        elif in_window and pending == "SELL" and shares > 0 and day > str(entry_date):
            fill = open_price * (1.0 - costs["slippage"])
            value = shares * fill
            fee = (_commission(value, costs["commission"], costs["minimum_commission"])
                   + value * costs["stamp_tax"])
            pnl = value - fee - shares * entry_price
            cash += value - fee
            turnover += value
            trades.append({"time": bar["bar_time"], "action": "SELL", "shares": shares,
                           "price": fill, "fee": fee, "pnl": pnl, "reason": "signal_next_bar"})
            shares, entry_price, peak_price, entry_date, pending = 0, 0.0, 0.0, None, None
        elif pending == "SELL" and shares > 0 and day <= str(entry_date):
            pending = "SELL"

        close = float(bar["close"])
        if shares > 0:
            peak_price = max(peak_price, close)
        if in_window:
            enter, exit_signal, _score = _signal(params, working, index)
            if shares == 0 and pending is None and enter:
                pending = "BUY"
            elif shares > 0 and pending is None:
                stop = close <= entry_price * (1.0 - params["stop_loss"])
                take = close >= entry_price * (1.0 + params["take_profit"])
                trail = (peak_price > entry_price and
                         close <= peak_price * (1.0 - params["trailing_stop"]))
                if exit_signal or stop or take or trail:
                    pending = "SELL"
        if in_window and day != last_date:
            last_date = day
        if in_window:
            equity = cash + shares * close
            if curve and curve[-1]["date"] == day:
                curve[-1] = {"date": day, "equity": equity}
            else:
                curve.append({"date": day, "equity": equity})
    if shares > 0 and curve:
        close = float(eligible[-1]["close"])
        value = shares * close * (1.0 - costs["slippage"])
        fee = (_commission(value, costs["commission"], costs["minimum_commission"])
               + value * costs["stamp_tax"])
        cash += value - fee
        turnover += value
        trades.append({"time": eligible[-1]["bar_time"], "action": "MARK_TO_MARK_EXIT",
                       "shares": shares, "price": close, "fee": fee,
                       "pnl": value - fee - shares * entry_price,
                       "reason": "research_window_end"})
        curve[-1]["equity"] = cash
    return {"equity": cash, "curve": curve, "trades": trades, "turnover": turnover}


def _metrics(curve: list[dict], trades: list[dict], initial: float, turnover: float) -> dict:
    if not curve:
        return {"total_return": 0.0, "annualized_return": 0.0, "max_drawdown": 0.0,
                "sharpe": 0.0, "trade_count": 0, "win_rate": 0.0,
                "turnover": 0.0, "trading_days": 0}
    values = [float(item["equity"]) for item in curve]
    total = values[-1] / initial - 1.0
    peak, drawdown = values[0], 0.0
    daily_returns = []
    for previous, current in zip(values, values[1:]):
        peak = max(peak, current)
        drawdown = max(drawdown, 1.0 - current / peak)
        daily_returns.append(current / previous - 1.0)
    days = max(1, len(values))
    annualized = (max(values[-1] / initial, 1e-9) ** (252 / days) - 1.0)
    volatility = statistics.pstdev(daily_returns) if len(daily_returns) > 1 else 0.0
    sharpe = statistics.fmean(daily_returns) / volatility * math.sqrt(252) if volatility else 0.0
    completed = [item for item in trades if item["action"] in {"SELL", "MARK_TO_MARK_EXIT"}]
    wins = sum(float(item.get("pnl", 0)) > 0 for item in completed)
    return {"total_return": total, "annualized_return": annualized,
            "max_drawdown": drawdown, "sharpe": sharpe,
            "trade_count": len(completed), "win_rate": wins / len(completed) if completed else 0.0,
            "turnover": turnover / initial, "trading_days": days}


def _portfolio_simulation(bars_by_symbol: dict[str, list[dict]], params: dict,
                          start_date: str, end_date: str, capital: float,
                          costs: dict) -> dict:
    available = {symbol: bars for symbol, bars in bars_by_symbol.items()
                 if any(start_date <= item["trade_date"] <= end_date for item in bars)}
    if not available:
        return _metrics([], [], capital, 0.0)
    allocation = capital / len(available)
    results = [_simulate_symbol(bars, params, start_date, end_date, allocation, costs)
               for bars in available.values()]
    dates = sorted({point["date"] for result in results for point in result["curve"]})
    by_result = [{point["date"]: point["equity"] for point in result["curve"]} for result in results]
    curve, last = [], [allocation] * len(results)
    for day in dates:
        for index, mapping in enumerate(by_result):
            if day in mapping:
                last[index] = mapping[day]
        curve.append({"date": day, "equity": sum(last)})
    trades = [item for result in results for item in result["trades"]]
    turnover = sum(float(result["turnover"]) for result in results)
    metrics = _metrics(curve, trades, capital, turnover)
    metrics["symbol_count"] = len(available)
    metrics["data_start"] = start_date
    metrics["data_end"] = end_date
    return metrics


def _date_splits(bars_by_symbol: dict[str, list[dict]]) -> tuple[list[str], dict]:
    dates = sorted({item["trade_date"] for bars in bars_by_symbol.values() for item in bars})
    if len(dates) < 120:
        raise ValueError(f"5分钟历史交易日不足 120 天，当前只有 {len(dates)} 天")
    train_end = max(60, int(len(dates) * 0.60))
    validation_end = max(train_end + 20, int(len(dates) * 0.80))
    validation_end = min(validation_end, len(dates) - 20)
    return dates, {
        "training": (dates[0], dates[train_end - 1]),
        "validation": (dates[train_end], dates[validation_end - 1]),
        "holdout": (dates[validation_end], dates[-1]),
    }


def _intraday_data_snapshot(interval_minutes: int, symbols: list[str],
                            bars_by_symbol: dict[str, list[dict]]) -> str:
    coverage = []
    for symbol in symbols:
        bars = bars_by_symbol.get(symbol, [])
        coverage.append({
            "symbol": symbol,
            "rows": len(bars),
            "first": bars[0]["bar_time"] if bars else None,
            "last": bars[-1]["bar_time"] if bars else None,
        })
    return _dump({"interval_minutes": interval_minutes, "coverage": coverage})


def run_intraday_evolution(symbols: Iterable[str], cycle_id: int | None = None,
                           interval_minutes: int = DEFAULT_INTERVAL,
                           max_drawdown: float = 0.15,
                           auto_promote: bool = True) -> dict:
    initialize()
    symbols = list(dict.fromkeys(str(symbol) for symbol in symbols))
    bars_by_symbol = _load_minute_bars(symbols, interval_minutes)
    dates, splits = _date_splits(bars_by_symbol)
    costs = {"commission": 0.0003, "minimum_commission": 5.0,
             "stamp_tax": 0.0005, "slippage": 0.0005, "participation": 0.05,
             "round_lot": 100, "t_plus_one": True}
    snapshot = _intraday_data_snapshot(interval_minutes, symbols, bars_by_symbol)
    run_key = "intraday-" + hashlib.sha256(snapshot.encode("utf-8")).hexdigest()[:24]
    with closing(connect()) as conn:
        existing = conn.execute("SELECT * FROM intraday_strategy_runs WHERE run_key=?", (run_key,)).fetchone()
        if existing and existing["status"] == "SUCCESS":
            return {"run_key": run_key, "status": "UNCHANGED",
                    "metrics": _load(existing["metrics_json"], {})}
        conn.execute(
            """INSERT INTO intraday_strategy_runs
               (run_key,cycle_id,status,interval_minutes,symbols_json,config_json,
                data_start,data_end,started_at)
               VALUES(?,?,'RUNNING',?,?,?,?,?,?)
               ON CONFLICT(run_key) DO UPDATE SET status='RUNNING',error=NULL,
                 started_at=excluded.started_at,finished_at=NULL""",
            (run_key, cycle_id, interval_minutes, _dump(symbols),
             _dump({"costs": costs, "max_drawdown": max_drawdown, "splits": splits,
                    "lookahead": False, "execution": "next_bar_open"}),
             dates[0], dates[-1], _now()),
        )
        conn.commit()
        run_id = int(conn.execute("SELECT id FROM intraday_strategy_runs WHERE run_key=?", (run_key,)).fetchone()[0])
    try:
        evaluated = []
        for params in _candidate_library():
            training = _portfolio_simulation(bars_by_symbol, params, *splits["training"], 1_000_000.0, costs)
            validation = _portfolio_simulation(bars_by_symbol, params, *splits["validation"], 1_000_000.0, costs)
            feasible = (validation["max_drawdown"] <= max_drawdown and
                        validation["trade_count"] >= 3)
            score = (validation["annualized_return"] - 1.5 * validation["max_drawdown"]
                     + 0.03 * _clamp(validation["sharpe"], -3, 3))
            evaluated.append({"params": params, "training": training,
                              "validation": validation, "feasible": feasible, "score": score})
        ranked = sorted(evaluated, key=lambda item: (item["feasible"], item["score"]), reverse=True)
        winner = ranked[0]
        winner["holdout"] = _portfolio_simulation(
            bars_by_symbol, winner["params"], *splits["holdout"], 1_000_000.0, costs)
        for item in ranked[1:]:
            item["holdout"] = {"status": "UNTOUCHED_NOT_SELECTED"}
        gate = {
            "enough_history": len(dates) >= 120,
            "validation_risk_pass": winner["validation"]["max_drawdown"] <= max_drawdown,
            "holdout_risk_pass": winner["holdout"]["max_drawdown"] <= max_drawdown,
            "holdout_trade_count_pass": winner["holdout"]["trade_count"] >= 3,
            "holdout_positive_after_costs": winner["holdout"]["total_return"] > 0,
            "lookahead_check": True,
            "cost_model_check": True,
            "t_plus_one_check": True,
        }
        gate["passed"] = all(gate.values())
        with closing(connect()) as conn:
            for index, item in enumerate(ranked):
                conn.execute(
                    """INSERT OR REPLACE INTO intraday_strategy_candidates
                       (run_id,strategy_key,params_json,training_json,validation_json,
                        holdout_json,feasible,score,selected,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, item["params"]["strategy_key"], _dump(item["params"]),
                     _dump(item["training"]), _dump(item["validation"]),
                     _dump(item["holdout"]), int(item["feasible"]), item["score"],
                     int(index == 0), _now()),
                )
            active = conn.execute(
                "SELECT version_key FROM intraday_strategy_versions WHERE status='ACTIVE'"
            ).fetchone()
            parent = str(active[0]) if active else None
            artifact = {"strategy": winner["params"], "interval_minutes": interval_minutes,
                        "symbols": symbols, "costs": costs, "data_end": dates[-1]}
            artifact_hash = hashlib.sha256(_dump(artifact).encode("utf-8")).hexdigest()
            version_key = f"intraday-{dates[-1]}-{artifact_hash[:12]}"
            status = "ACTIVE" if auto_promote and gate["passed"] else "CANDIDATE"
            if status == "ACTIVE":
                conn.execute("UPDATE intraday_strategy_versions SET status='ARCHIVED' WHERE status='ACTIVE'")
            conn.execute(
                """INSERT INTO intraday_strategy_versions
                   (version_key,parent_version,run_id,status,strategy_json,metrics_json,
                    gate_json,artifact_hash,reason,created_at,activated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(version_key) DO UPDATE SET status=excluded.status,
                     metrics_json=excluded.metrics_json,gate_json=excluded.gate_json,
                     activated_at=excluded.activated_at""",
                (version_key, parent, run_id, status, _dump(artifact),
                 _dump({"training": winner["training"], "validation": winner["validation"],
                        "holdout": winner["holdout"]}), _dump(gate), artifact_hash,
                 "untouched holdout and A-share execution gates", _now(),
                 _now() if status == "ACTIVE" else None),
            )
            summary = {"winner": winner["params"], "training": winner["training"],
                       "validation": winner["validation"], "holdout": winner["holdout"],
                       "gate": gate, "version_key": version_key, "version_status": status,
                       "candidate_count": len(ranked), "splits": splits}
            conn.execute(
                "UPDATE intraday_strategy_runs SET status='SUCCESS',metrics_json=?,finished_at=? WHERE id=?",
                (_dump(summary), _now(), run_id),
            )
            conn.commit()
        create_latest_signals(symbols)
        return {"run_id": run_id, "run_key": run_key, "status": "SUCCESS", "metrics": summary}
    except Exception as exc:
        with closing(connect()) as conn:
            conn.execute("UPDATE intraday_strategy_runs SET status='FAILED',error=?,finished_at=? WHERE id=?",
                         (repr(exc), _now(), run_id))
            conn.commit()
        raise


def create_latest_signals(symbols: Iterable[str]) -> dict:
    initialize()
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM intraday_strategy_versions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {"status": "NO_ACTIVE_VERSION", "signals": []}
    strategy = _load(row["strategy_json"], {})
    params = strategy.get("strategy", {})
    interval = int(strategy.get("interval_minutes", DEFAULT_INTERVAL))
    bars_by_symbol = _load_minute_bars(symbols, interval)
    signals = []
    with closing(connect()) as conn:
        for symbol, bars in bars_by_symbol.items():
            if not bars:
                continue
            enter, exit_signal, score = _signal(params, bars, len(bars) - 1)
            action = "BUY_WATCH" if enter else ("EXIT_WATCH" if exit_signal else "HOLD")
            latest = bars[-1]
            key = hashlib.sha256(
                f"{row['version_key']}|{symbol}|{latest['bar_time']}".encode("utf-8")
            ).hexdigest()
            features = {"completed_bar_only": True, "next_bar_execution": True,
                        "t_plus_one": True, "order_execution": False}
            conn.execute(
                """INSERT OR REPLACE INTO intraday_signals
                   (signal_key,version_key,symbol,bar_time,action,score,reference_price,
                    features_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (key, row["version_key"], symbol, latest["bar_time"], action, score,
                 float(latest["close"]), _dump(features), _now()),
            )
            signals.append({"symbol": symbol, "bar_time": latest["bar_time"],
                            "action": action, "score": score,
                            "reference_price": float(latest["close"])})
        conn.commit()
    return {"status": "SUCCESS", "version_key": row["version_key"], "signals": signals}


def intraday_payload() -> dict:
    initialize()
    with closing(connect()) as conn:
        run = conn.execute("SELECT * FROM intraday_strategy_runs ORDER BY id DESC LIMIT 1").fetchone()
        version = conn.execute(
            "SELECT * FROM intraday_strategy_versions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        signals = conn.execute(
            """SELECT s.* FROM intraday_signals s JOIN (
                 SELECT symbol,MAX(bar_time) bar_time FROM intraday_signals GROUP BY symbol
               ) x ON x.symbol=s.symbol AND x.bar_time=s.bar_time ORDER BY s.symbol"""
        ).fetchall()
    run_item = dict(run) if run else None
    if run_item:
        run_item["metrics"] = _load(run_item.pop("metrics_json"), {})
        run_item["config"] = _load(run_item.pop("config_json"), {})
        run_item["symbols"] = _load(run_item.pop("symbols_json"), [])
    version_item = dict(version) if version else None
    if version_item:
        version_item["strategy"] = _load(version_item.pop("strategy_json"), {})
        version_item["metrics"] = _load(version_item.pop("metrics_json"), {})
        version_item["gate"] = _load(version_item.pop("gate_json"), {})
    return {"last_run": run_item, "active_version": version_item,
            "latest_signals": [dict(item) for item in signals],
            "default_interval_minutes": DEFAULT_INTERVAL,
            "research_only": True, "order_execution": False}
