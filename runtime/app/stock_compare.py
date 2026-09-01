"""A-share comparison engine used by the visual stock-comparison plugin.

The module keeps recommendation logic deterministic and auditable. Market data
comes from the existing public-source adapters and all backtests use lagged
signals. It never connects to a broker or submits an order.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import threading
import time
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import date, datetime, timezone
from typing import Iterable

from .data_sources.market import (fetch_tencent_quotes, market_provider_mode,
                                  normalize_symbol, refresh_market_data)
from .data_sources.reports import register_report_equities
from .db import connect


_refresh_lock = threading.Lock()
_refresh_last_attempt: dict[str, float] = {}
_REFRESH_COALESCE_SECONDS = 10.0
_history_refresh_lock = threading.Lock()
_history_refreshing: set[str] = set()


def register_compared_stocks(stocks: list[dict]) -> None:
    """Persist every distinct comparison input for overview chart selection."""
    compared_at = datetime.now(timezone.utc).isoformat()
    with closing(connect()) as conn:
        for stock in stocks:
            symbol = str(stock.get("symbol") or "").strip()
            if not symbol:
                continue
            name = str(stock.get("name") or symbol).strip()
            conn.execute(
                """INSERT INTO comparison_watchlist
                   (symbol,name,first_compared_at,last_compared_at,compare_count)
                   VALUES(?,?,?,?,1) ON CONFLICT(symbol) DO UPDATE SET
                   name=excluded.name,last_compared_at=excluded.last_compared_at,
                   compare_count=comparison_watchlist.compare_count+1""",
                (symbol, name, compared_at, compared_at),
            )
        conn.commit()


PROFILE_DEFINITIONS = {
    "aggressive": {
        "label": "激进派",
        "horizon": "6-18 个月",
        "strategy": "20 日突破 + 10 日退出",
        "weights": (
            ("catalyst", "上行催化与重估", 30),
            ("growth", "增长加速", 20),
            ("momentum", "价格动量", 15),
            ("optionality", "叙事与弹性", 15),
            ("valuation", "成长估值", 10),
            ("runway", "资金续航与风控", 10),
        ),
        "risk": {"stop_loss": 0.08, "trailing_stop": 0.10},
    },
    "balanced": {
        "label": "中间派",
        "horizon": "12-36 个月",
        "strategy": "20/60 日趋势 + 风险退出",
        "weights": (
            ("quality", "商业质量与执行", 20),
            ("growth", "成长与催化", 20),
            ("valuation", "估值与安全边际", 20),
            ("financial", "财务健康", 15),
            ("risk", "风险画像", 15),
            ("shareholder", "股东回报", 10),
        ),
        "risk": {"stop_loss": 0.07, "trailing_stop": 0.08},
    },
    "conservative": {
        "label": "保守派",
        "horizon": "2-5 年",
        "strategy": "60/120 日防守趋势 + 严格退出",
        "weights": (
            ("balance_sheet", "资产负债表", 25),
            ("cashflow", "现金流与盈利韧性", 20),
            ("valuation", "估值与安全边际", 20),
            ("shareholder", "股东回报可持续性", 15),
            ("resilience", "商业韧性", 10),
            ("downside", "波动与下行保护", 10),
        ),
        "risk": {"stop_loss": 0.05, "trailing_stop": 0.06},
    },
}

PROFILE_ALIASES = {
    "aggressive": "aggressive", "激进": "aggressive", "激进派": "aggressive",
    "balanced": "balanced", "neutral": "balanced", "中立": "balanced",
    "中间": "balanced", "中间派": "balanced", "均衡": "balanced",
    "conservative": "conservative", "保守": "conservative", "保守派": "conservative",
}

STOCK_NAME_ALIASES = {
    "中行": ("601988", "中国银行"),
    "东方航空": ("600115", "中国东方航空"),
    "中国东航": ("600115", "中国东方航空"),
    "建行": ("601939", "中国建设银行"),
    "中国建行": ("601939", "中国建设银行"),
}

SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
PROFILE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
EASTMONEY_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"


def normalize_profile(value: str) -> str:
    key = PROFILE_ALIASES.get(str(value or "").strip().lower())
    if not key:
        raise ValueError("投资态度必须是激进派、中间派或保守派")
    return key


def split_stock_inputs(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[,，、;；\s]+", value.strip())
    else:
        parts = []
        for item in value:
            parts.extend(re.split(r"[,，、;；\s]+", str(item).strip()))
    unique = []
    for item in parts:
        if item and item not in unique:
            unique.append(item)
    if len(unique) < 2:
        raise ValueError("请至少输入两只股票的名称或代码")
    if len(unique) > 8:
        raise ValueError("一次最多对比 8 只股票")
    return unique


def _request_json(url: str, params: dict, timeout: float = 8.0) -> dict:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url + "?" + query,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ArgusStockComparison/1.0",
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _local_name_match(token: str) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT symbol,name FROM assets
               WHERE name=? OR name LIKE ?
               ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END,id LIMIT 1""",
            (token, f"%{token}%", token),
        ).fetchone()
        if not row:
            row = conn.execute(
                """SELECT asset_symbol AS symbol,asset_name AS name FROM quote_snapshots
                   WHERE asset_name=? OR asset_name LIKE ?
                   ORDER BY CASE WHEN asset_name=? THEN 0 ELSE 1 END,
                            julianday(captured_at) DESC,observed_at DESC LIMIT 1""",
                (token, f"%{token}%", token),
            ).fetchone()
    return dict(row) if row else None


def _online_name_match(token: str) -> dict | None:
    payload = _request_json(SUGGEST_URL, {
        "input": token, "type": "14", "token": EASTMONEY_TOKEN,
    })
    data = ((payload.get("QuotationCodeTable") or {}).get("Data") or [])
    if isinstance(data, dict):
        data = [data]
    candidates = [item for item in data if re.fullmatch(r"\d{6}", str(item.get("Code", "")))]
    if not candidates:
        return None
    exact = next((item for item in candidates if str(item.get("Name", "")) == token), candidates[0])
    return {"symbol": str(exact["Code"]), "name": str(exact.get("Name") or token)}


def resolve_stock(token: str) -> dict:
    from .harness import active_config, normalize_input
    config = active_config()
    value = normalize_input(token, config)
    learned = config.get("stock_aliases", {}).get(value)
    if isinstance(learned, dict) and learned.get("symbol"):
        return {"input": token, "symbol": normalize_symbol(str(learned["symbol"])),
                "name": str(learned.get("name") or value)}
    if value in STOCK_NAME_ALIASES:
        symbol, name = STOCK_NAME_ALIASES[value]
        return {"input": token, "symbol": symbol, "name": name}
    code_match = re.fullmatch(r"(?:SH|SZ|BJ)?[.]?(\d{6})(?:[.](?:SH|SZ|BJ))?", value.upper())
    if code_match:
        symbol = normalize_symbol(code_match.group(1))
        local = _local_name_match(symbol)
        return {"input": token, "symbol": symbol, "name": (local or {}).get("name", symbol)}
    local = _local_name_match(value)
    if local:
        return {"input": token, "symbol": normalize_symbol(local["symbol"]), "name": local["name"]}
    try:
        online = _online_name_match(value)
    except Exception as exc:
        raise ValueError(f"无法解析股票名称“{value}”：名称服务不可用（{exc}）") from exc
    if not online:
        raise ValueError(f"没有找到股票“{value}”，请改用 6 位 A 股代码")
    return {"input": token, "symbol": normalize_symbol(online["symbol"]), "name": online["name"]}


def _number(value, scale=1.0):
    try:
        result = float(value) / scale
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _positive_number(value, scale=1.0):
    result = _number(value, scale)
    return result if result is not None and result > 0 else None


def _fundamentals(symbol: str) -> dict:
    market = "1" if symbol.startswith(("5", "6", "9")) else ("0" if symbol.startswith(("0", "1", "2", "3")) else "0")
    fields = "f57,f58,f43,f58,f115,f116,f117,f127,f162,f167,f168,f173"
    try:
        payload = _request_json(PROFILE_URL, {"secid": f"{market}.{symbol}", "fields": fields})
        data = payload.get("data") or {}
        return {
            "name": re.sub(r"\s+", "", str(data.get("f58") or symbol)),
            "industry": data.get("f127") or "未提供",
            "market_cap": _positive_number(data.get("f116")),
            "float_market_cap": _positive_number(data.get("f117")),
            "pe_ttm": _positive_number(data.get("f115"), 100.0),
            "pe_dynamic": _positive_number(data.get("f162"), 100.0),
            "pb": _positive_number(data.get("f167"), 100.0),
            "turnover_rate": _positive_number(data.get("f168"), 100.0),
            "roe": _positive_number(data.get("f173"), 100.0),
            "source": "东方财富公开行情（AKShare 同类公开接口）",
            "data_gaps": ["收入与利润增速", "资产负债率", "自由现金流",
                          "分红与回购", "近期催化剂与业绩指引"],
        }
    except Exception:
        try:
            _quotes, raw = fetch_tencent_quotes([symbol])
            fields = raw.decode("gbk", errors="replace").split("~")
            return {
                "name": re.sub(r"\s+", "", fields[1] or symbol),
                "industry": "未提供",
                "market_cap": (_positive_number(fields[44]) or 0) * 100_000_000 or None,
                "float_market_cap": (_positive_number(fields[45]) or 0) * 100_000_000 or None,
                "pe_ttm": _positive_number(fields[39]),
                "pe_dynamic": _positive_number(fields[52]),
                "pb": _positive_number(fields[46]),
                "turnover_rate": (_positive_number(fields[38]) or 0) / 100 or None,
                "roe": None,
                "source": "腾讯公开行情（AKShare 数据链路兜底）",
                "data_gaps": ["行业分类", "收入与利润增速", "资产负债率", "自由现金流",
                              "分红与回购", "近期催化剂与业绩指引"],
            }
        except Exception:
            return {"name": symbol, "industry": "未提供", "market_cap": None,
                    "float_market_cap": None, "pe_ttm": None, "pe_dynamic": None,
                    "pb": None, "turnover_rate": None, "roe": None,
                    "source": "本地缓存",
                    "data_gaps": ["估值", "行业分类", "收入与利润增速", "资产负债率",
                                  "自由现金流", "分红与回购", "近期催化剂与业绩指引"]}


def _load_bars(symbol: str) -> list[dict]:
    source_filter = (" AND d2.code IN ('tdx_local','tdx_public')"
                     if market_provider_mode() == "tdx" else "")
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT trade_date,open,high,low,close,COALESCE(volume,0) AS volume,
                      COALESCE(amount,0) AS amount,captured_at
               FROM market_daily_bars
               WHERE asset_symbol=? AND adjust_mode='qfq' AND source_id=(
                 SELECT m2.source_id FROM market_daily_bars m2
                 JOIN data_sources d2 ON d2.id=m2.source_id
                 WHERE m2.asset_symbol=? AND m2.adjust_mode='qfq'""" + source_filter + """
                 ORDER BY m2.captured_at DESC LIMIT 1)
               ORDER BY trade_date""", (symbol, symbol),
        ).fetchall()
    return [dict(row) for row in rows]


def _latest_quote(symbol: str) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT q.*,d.code AS source FROM quote_snapshots q
               JOIN data_sources d ON d.id=q.source_id WHERE q.asset_symbol=?
               ORDER BY julianday(q.captured_at) DESC,q.observed_at DESC,d.priority LIMIT 1""",
            (symbol,),
        ).fetchone()
    return dict(row) if row else None


def _pct_change(values: list[float], lookback: int) -> float | None:
    if len(values) <= lookback or not values[-lookback - 1]:
        return None
    return values[-1] / values[-lookback - 1] - 1


def _max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def calculate_price_metrics(bars: list[dict]) -> dict:
    closes = [float(row["close"]) for row in bars if float(row["close"]) > 0]
    if len(closes) < 60:
        raise ValueError("历史日线不足 60 个交易日")
    daily_returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
    vol = statistics.stdev(daily_returns) * math.sqrt(252) if len(daily_returns) > 1 else 0.0
    annual_return = (closes[-1] / closes[0]) ** (252 / max(1, len(closes) - 1)) - 1
    sharpe = (statistics.mean(daily_returns) * 252) / vol if vol else 0.0
    recent_amounts = [float(row.get("amount") or 0) for row in bars[-20:]]
    high = max(closes)
    return {
        "last_close": closes[-1],
        "momentum_20d": _pct_change(closes, 20),
        "momentum_60d": _pct_change(closes, 60),
        "momentum_120d": _pct_change(closes, 120),
        "annualized_return": annual_return,
        "annualized_volatility": vol,
        "sharpe_ratio": sharpe,
        "max_drawdown": _max_drawdown(closes),
        "current_drawdown": closes[-1] / high - 1,
        "positive_day_ratio": sum(item > 0 for item in daily_returns) / len(daily_returns),
        "avg_amount_20d": statistics.mean(recent_amounts) if recent_amounts else 0.0,
        "history_days": len(closes),
        "data_start": bars[0]["trade_date"],
        "data_end": bars[-1]["trade_date"],
    }


def _sma(values: list[float], end: int, window: int) -> float | None:
    if end < window:
        return None
    return statistics.mean(values[end - window:end])


def run_profile_backtest(bars: list[dict], profile: str) -> dict:
    definition = PROFILE_DEFINITIONS[profile]
    closes = [float(row["close"]) for row in bars]
    cash = 100000.0
    shares = 0.0
    entry = peak = 0.0
    equities = []
    exposed = 0
    fee = 0.0003

    for index, bar in enumerate(bars):
        open_price = float(bar["open"])
        close_price = float(bar["close"])
        previous_close = closes[index - 1] if index else close_price
        enter = exit_signal = False
        if index:
            if profile == "aggressive":
                prior_high = max(closes[max(0, index - 21):index - 1] or [previous_close])
                prior_low = min(closes[max(0, index - 10):index])
                enter = index >= 21 and previous_close >= prior_high
                exit_signal = previous_close < prior_low
            elif profile == "balanced":
                ma20 = _sma(closes, index, 20)
                ma60 = _sma(closes, index, 60)
                enter = ma20 is not None and ma60 is not None and ma20 > ma60
                exit_signal = ma20 is not None and ma60 is not None and ma20 <= ma60
            else:
                ma60 = _sma(closes, index, 60)
                ma120 = _sma(closes, index, 120)
                enter = ma60 is not None and ma120 is not None and previous_close > ma120 and ma60 > ma120
                exit_signal = ma60 is not None and ma120 is not None and (previous_close < ma120 or ma60 <= ma120)

        if shares and (exit_signal or previous_close <= entry * (1 - definition["risk"]["stop_loss"])
                       or previous_close <= peak * (1 - definition["risk"]["trailing_stop"])):
            cash = shares * open_price * (1 - fee)
            shares = 0.0
            entry = peak = 0.0
        if not shares and enter and open_price > 0:
            shares = cash * (1 - fee) / open_price
            cash = 0.0
            entry = peak = open_price
        if shares:
            exposed += 1
            peak = max(peak, close_price)
        equity = cash + shares * close_price
        equities.append(equity)

    benchmark = [100000 * value / closes[0] for value in closes]
    returns = [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities)) if equities[i - 1]]
    vol = statistics.stdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    annualized = (equities[-1] / equities[0]) ** (252 / max(1, len(equities) - 1)) - 1
    step = max(1, math.ceil(len(bars) / 180))
    curve = [{"date": bars[i]["trade_date"], "strategy": round(equities[i], 2),
              "benchmark": round(benchmark[i], 2)} for i in range(0, len(bars), step)]
    if curve[-1]["date"] != bars[-1]["trade_date"]:
        curve.append({"date": bars[-1]["trade_date"], "strategy": round(equities[-1], 2),
                      "benchmark": round(benchmark[-1], 2)})
    return {
        "engine": "本地无未来函数回放（AKQuant 方法口径）",
        "strategy": definition["strategy"],
        "signal_lag": "仅使用上一交易日及更早数据，次日开盘执行",
        "commission_rate": fee,
        "total_return_pct": round((equities[-1] / equities[0] - 1) * 100, 2),
        "benchmark_return_pct": round((benchmark[-1] / benchmark[0] - 1) * 100, 2),
        "annualized_return_pct": round(annualized * 100, 2),
        "max_drawdown_pct": round(_max_drawdown(equities) * 100, 2),
        "sharpe_ratio": round((statistics.mean(returns) * 252) / vol, 2) if vol else 0.0,
        "exposure_pct": round(exposed / len(bars) * 100, 1),
        "curve": curve,
    }


def _scale(value, low: float, high: float, inverse: bool = False) -> tuple[float, bool]:
    if value is None:
        return 50.0, False
    score = max(0.0, min(100.0, (float(value) - low) / (high - low) * 100))
    return (100 - score if inverse else score), True


def _average(*items: tuple[float, bool]) -> tuple[float, float]:
    return statistics.mean(item[0] for item in items), sum(item[1] for item in items) / len(items)


def _dimension_scores(metrics: dict, fundamentals: dict, profile: str) -> tuple[dict, float]:
    unavailable = (50.0, False)
    momentum = _average(_scale(metrics["momentum_20d"], -0.20, 0.35),
                        _scale(metrics["momentum_60d"], -0.30, 0.55))
    price_growth = _average(_scale(metrics["momentum_120d"], -0.35, 0.70),
                            _scale(metrics["annualized_return"], -0.20, 0.45))
    # Revenue/profit growth is not inferred from price action.
    growth = _average(price_growth, unavailable)
    valuation = _average(_scale(fundamentals.get("pe_ttm") or fundamentals.get("pe_dynamic"), 8, 60, True),
                         _scale(fundamentals.get("pb"), 0.8, 8, True))
    downside = _average(_scale(metrics["annualized_volatility"], 0.12, 0.65, True),
                        _scale(abs(metrics["max_drawdown"]), 0.08, 0.60, True),
                        _scale(abs(metrics["current_drawdown"]), 0.02, 0.35, True))
    quality = _average(_scale(fundamentals.get("roe"), 0.02, 0.25),
                       _scale(metrics["sharpe_ratio"], -0.4, 1.8),
                       _scale(metrics["positive_day_ratio"], 0.42, 0.58))
    liquidity = _scale(math.log10(max(metrics["avg_amount_20d"], 1)), 6, 10)
    roe = _scale(fundamentals.get("roe"), 0.02, 0.25)
    financial = _average(roe, unavailable, downside)

    if profile == "aggressive":
        values = {
            "catalyst": _average(momentum, unavailable),
            "growth": growth,
            "momentum": momentum,
            "optionality": _average(price_growth, _scale(metrics["annualized_volatility"], 0.12, 0.65)),
            "valuation": valuation,
            "runway": _average(liquidity, downside),
        }
    elif profile == "balanced":
        values = {"quality": quality, "growth": growth, "valuation": valuation,
                  "financial": financial, "risk": downside, "shareholder": unavailable}
    else:
        values = {"balance_sheet": unavailable, "cashflow": _average(roe, unavailable),
                  "valuation": valuation, "shareholder": unavailable,
                  "resilience": _average(downside, _scale(metrics["positive_day_ratio"], 0.42, 0.58)),
                  "downside": downside}
    dimensions = {key: round(value[0], 1) for key, value in values.items()}
    coverage = statistics.mean(value[1] for value in values.values())
    return dimensions, coverage


def _red_flags(metrics: dict, fundamentals: dict) -> list[str]:
    flags = []
    if metrics["max_drawdown"] <= -0.40:
        flags.append("历史最大回撤超过 40%")
    if metrics["annualized_volatility"] >= 0.45:
        flags.append("年化波动率较高")
    if metrics["momentum_60d"] is not None and metrics["momentum_60d"] < -0.15:
        flags.append("60 日动量明显转弱")
    pe = fundamentals.get("pe_ttm") or fundamentals.get("pe_dynamic")
    if pe is not None and pe > 60:
        flags.append("市盈率较高，需验证增长持续性")
    if fundamentals.get("pb") is not None and fundamentals["pb"] > 8:
        flags.append("市净率较高，安全边际有限")
    return flags or ["未发现由现有价格与估值数据直接触发的高风险项"]


def _invalidation(metrics: dict, profile: str) -> str:
    if profile == "aggressive":
        return "若 60 日动量转负、突破策略失效，或增长催化无法由公告/财报确认，激进逻辑失效。"
    if profile == "balanced":
        return "若趋势转弱同时估值仍无安全边际，或现金流与利润质量不能验证，应下调排序。"
    return "若最大回撤继续扩大、估值缺乏折价，或资产负债表与自由现金流无法核验，不满足保守要求。"


def _profile_weights(profile: str):
    default = PROFILE_DEFINITIONS[profile]["weights"]
    from .harness import active_config
    override = active_config().get("profile_weights", {}).get(profile)
    if not isinstance(override, dict):
        return default
    keys = {key for key, _label, _weight in default}
    if set(override) != keys or abs(sum(float(value) for value in override.values()) - 100) > 0.001:
        return default
    return tuple((key, label, float(override[key])) for key, label, _weight in default)


def _score_item(item: dict, profile: str) -> dict:
    definition = PROFILE_DEFINITIONS[profile]
    weights = _profile_weights(profile)
    dimensions, coverage = _dimension_scores(item["metrics"], item["fundamentals"], profile)
    total = sum(dimensions[key] * weight / 100 for key, _label, weight in weights)
    item["dimensions"] = [{"key": key, "label": label, "weight": weight, "score": dimensions[key]}
                          for key, label, weight in weights]
    item["score"] = round(total, 1)
    # Price/valuation data cannot fully stand in for company filings. The cap
    # prevents the UI from overstating certainty when cash-flow, leverage,
    # guidance, dividend and catalyst fields are not available.
    confidence_cap = {"aggressive": 72, "balanced": 68, "conservative": 56}[profile]
    item["confidence"] = min(round(coverage * 100), confidence_cap)
    item["red_flags"] = _red_flags(item["metrics"], item["fundamentals"])
    item["invalidation"] = _invalidation(item["metrics"], profile)
    item["stance"] = "优先研究" if total >= 72 else ("重点观察" if total >= 60 else ("中性观察" if total >= 48 else "暂缓"))
    return item


def _stale_symbols(symbols: list[str]) -> list[str]:
    source_filter = (" AND d.code IN ('tdx_local','tdx_public')"
                     if market_provider_mode() == "tdx" else "")
    with closing(connect()) as conn:
        latest = {row[0]: row[1] for row in conn.execute(
            f"""SELECT m.asset_symbol,MAX(m.trade_date) FROM market_daily_bars m
                JOIN data_sources d ON d.id=m.source_id
                WHERE m.asset_symbol IN ({','.join('?' for _ in symbols)}){source_filter}
                GROUP BY m.asset_symbol""",
            symbols,
        )}
    return [symbol for symbol in symbols
            if symbol not in latest or (date.today() - date.fromisoformat(latest[symbol])).days > 7]


def _missing_tdx_history_symbols(symbols: list[str]) -> list[str]:
    if market_provider_mode() != "tdx":
        return []
    with closing(connect()) as conn:
        coverage = {row[0]: int(row[1]) for row in conn.execute(
            f"""SELECT m.asset_symbol,COUNT(DISTINCT m.interval_minutes)
                FROM minute_bars m JOIN data_sources d ON d.id=m.source_id
                WHERE m.asset_symbol IN ({','.join('?' for _ in symbols)})
                  AND m.interval_minutes IN (1,5,15,30,60)
                  AND d.code IN ('tdx_local','tdx_public')
                GROUP BY m.asset_symbol""", symbols,
        )}
    return [symbol for symbol in symbols if coverage.get(symbol, 0) < 5]


def _schedule_history_refresh(symbols: list[str]) -> list[str]:
    with _history_refresh_lock:
        due = [symbol for symbol in symbols if symbol not in _history_refreshing]
        _history_refreshing.update(due)
    if not due:
        return []

    def worker():
        try:
            refresh_market_data(due, include_minutes=False, include_daily=False, include_history=True)
        finally:
            with _history_refresh_lock:
                _history_refreshing.difference_update(due)

    threading.Thread(target=worker, name="argus-tdx-history-refresh", daemon=True).start()
    return due


def _refresh_if_needed(resolved: list[dict], force: bool) -> dict:
    symbols = list(dict.fromkeys(item["symbol"] for item in resolved))
    missing_history = set(_missing_tdx_history_symbols(symbols))
    stale = set(_stale_symbols(symbols))
    pending = symbols if force else [symbol for symbol in symbols if symbol in stale or symbol in missing_history]
    if not pending:
        return {"status": "CACHE_FRESH", "errors": []}

    # One refresh may serve many browser windows. Waiting callers re-check the
    # recent-attempt map and avoid repeating the same external requests.
    with _refresh_lock:
        now = time.monotonic()
        expired = [symbol for symbol, attempted_at in _refresh_last_attempt.items()
                   if now - attempted_at >= _REFRESH_COALESCE_SECONDS]
        for symbol in expired:
            _refresh_last_attempt.pop(symbol, None)
        missing_history = set(_missing_tdx_history_symbols(symbols))
        stale = set(_stale_symbols(symbols))
        pending = symbols if force else [symbol for symbol in symbols if symbol in stale or symbol in missing_history]
        due = [symbol for symbol in pending if symbol not in _refresh_last_attempt]
        coalesced = [symbol for symbol in pending if symbol not in due]
        if not due:
            return {"status": "REFRESH_COALESCED", "coalesced_symbols": coalesced, "errors": []}
        try:
            result = refresh_market_data(due, include_minutes=False, include_daily=True, include_history=False)
        except Exception as exc:
            result = {"status": "DEGRADED_CACHE", "errors": [{"error": str(exc)}]}
        finally:
            attempted_at = time.monotonic()
            for symbol in due:
                _refresh_last_attempt[symbol] = attempted_at
        if coalesced:
            result["coalesced_symbols"] = coalesced
        history_started = _schedule_history_refresh(
            [symbol for symbol in due if force or symbol in missing_history]
        )
        if history_started:
            result["history_refresh"] = {"status": "STARTED", "symbols": history_started}
        return result


def compare_stocks(stock_inputs: str | Iterable[str], profile_value: str, refresh: bool = False) -> dict:
    profile = normalize_profile(profile_value)
    requested = split_stock_inputs(stock_inputs)
    resolved = [resolve_stock(token) for token in requested]
    symbols = [item["symbol"] for item in resolved]
    if len(set(symbols)) != len(symbols):
        raise ValueError("输入中包含重复股票，请保留不同标的")
    register_compared_stocks(resolved)
    register_report_equities(resolved)
    refresh_result = _refresh_if_needed(resolved, refresh)
    items = []
    errors = []
    for stock in resolved:
        bars = _load_bars(stock["symbol"])
        if len(bars) < 60:
            errors.append(f"{stock['name']}（{stock['symbol']}）历史日线不足 60 个交易日")
            continue
        fundamentals = _fundamentals(stock["symbol"])
        quote = _latest_quote(stock["symbol"])
        stock["name"] = fundamentals.get("name") if fundamentals.get("name") != stock["symbol"] else stock["name"]
        item = {
            **stock,
            "quote": quote,
            "fundamentals": fundamentals,
            "metrics": calculate_price_metrics(bars),
            "backtest": run_profile_backtest(bars, profile),
        }
        items.append(_score_item(item, profile))
    if len(items) < 2:
        detail = "；".join(errors) or "没有足够数据完成对比"
        raise RuntimeError(detail)
    items.sort(key=lambda item: item["score"], reverse=True)
    for rank, item in enumerate(items, 1):
        item["rank"] = rank
    winner = items[0]
    strongest = sorted(winner["dimensions"], key=lambda item: item["score"], reverse=True)[:2]
    label = PROFILE_DEFINITIONS[profile]["label"]
    threshold = 60 if profile == "conservative" else 48
    qualified = winner["score"] >= threshold
    headline = (f"{label}当前优先研究：{winner['name']}（{winner['symbol']}）" if qualified else
                f"当前没有一只达到{label}门槛；暂时领先的是{winner['name']}（{winner['symbol']}）")
    return {
        "profile": {"key": profile, **PROFILE_DEFINITIONS[profile]},
        "as_of": max(item["metrics"]["data_end"] for item in items),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ranking": items,
        "verdict": {
            "winner": winner["symbol"],
            "headline": headline,
            "reason": f"它在{strongest[0]['label']}和{strongest[1]['label']}两项上更占优；结论置信度 {winner['confidence']}%。",
            "qualification": "这是基于公开行情、可得估值与历史回放的研究排序，不是个性化资产配置或买卖指令。",
            "qualified": qualified,
        },
        "methodology": {
            "horizon": PROFILE_DEFINITIONS[profile]["horizon"],
            "weights": [{"key": key, "label": text, "weight": weight}
                        for key, text, weight in _profile_weights(profile)],
            "backtest": "参考 AKQuant 的事件驱动思想：信号滞后一日、次日开盘执行、计入 0.03% 单边费用。",
            "sources": [
                {"name": "通达信兼容行情链路", "url": "https://www.tdx.com.cn/"},
                {"name": "AKShare", "url": "https://github.com/akfamily/akshare"},
                {"name": "AKQuant", "url": "https://github.com/akfamily/akquant"},
            ],
        },
        "refresh": refresh_result,
        "warnings": errors + ["未覆盖的财务字段按中性分处理，已反映在每只股票的置信度中。"],
        "order_execution": False,
        "api_key_json": False,
    }
