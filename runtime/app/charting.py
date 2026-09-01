"""Multi-timeframe market chart construction."""

from __future__ import annotations

from datetime import date, datetime
import os
import re

SUPPORTED_PERIODS = {
    "time": "分时", "5d": "五日", "1m": "1分钟", "5m": "5分钟",
    "15m": "15分钟", "30m": "30分钟", "60m": "60分钟", "120m": "120分钟",
    "1d": "日K", "1w": "周K", "1mo": "月K", "1q": "季K", "1y": "年K",
}


def _merge(group: list[dict], time_value: str) -> dict:
    return {
        "time": time_value,
        "open": float(group[0]["open"]),
        "high": max(float(row["high"]) for row in group),
        "low": min(float(row["low"]) for row in group),
        "close": float(group[-1]["close"]),
        "volume": sum(float(row.get("volume") or 0) for row in group),
        "amount": sum(float(row.get("amount") or 0) for row in group),
    }


def aggregate_intraday(rows: list[dict], minutes: int, source_minutes: int = 1) -> list[dict]:
    buckets = {}
    for row in rows:
        when = datetime.fromisoformat(row["bar_time"])
        clock = when.hour * 60 + when.minute
        # Native multi-minute providers timestamp bars at the interval end.
        # Shift to the interval start before grouping 60m bars into 120m bars.
        if source_minutes > 1:
            clock -= source_minutes
        if 570 <= clock <= 690:
            session, offset = "am", clock - 570
        elif 780 <= clock <= 900:
            session, offset = "pm", clock - 780
        else:
            continue
        key = (when.date().isoformat(), session, offset // minutes)
        buckets.setdefault(key, []).append(row)
    result = []
    for group in buckets.values():
        group.sort(key=lambda item: item["bar_time"])
        result.append(_merge(group, group[0]["bar_time"]))
    return sorted(result, key=lambda item: item["time"])


def aggregate_calendar(rows: list[dict], period: str) -> list[dict]:
    buckets = {}
    for row in rows:
        when = datetime.fromisoformat(row["trade_date"])
        if period == "1w":
            iso = when.isocalendar()
            key = (iso.year, iso.week)
        elif period == "1mo":
            key = (when.year, when.month)
        elif period == "1q":
            key = (when.year, (when.month - 1) // 3 + 1)
        elif period == "1y":
            key = (when.year,)
        else:
            key = (when.date().isoformat(),)
        buckets.setdefault(key, []).append(row)
    result = []
    for group in buckets.values():
        group.sort(key=lambda item: item["trade_date"])
        result.append(_merge(group, group[-1]["trade_date"]))
    return sorted(result, key=lambda item: item["time"])


def add_ma5(rows: list[dict]) -> list[dict]:
    closes = []
    for row in rows:
        closes.append(float(row["close"]))
        row["ma5"] = round(sum(closes[-5:]) / 5, 6) if len(closes) >= 5 else None
    return rows


def add_technical_indicators(rows: list[dict], symbol: str = "") -> list[dict]:
    """Add hover metrics, KDJ and MACD without inventing unavailable turnover data."""
    if not rows:
        return rows
    ema12 = ema26 = dea = None
    k = d = 50.0
    first_close = float(rows[0]["close"])
    cn_lot_volume = bool(re.fullmatch(r"\d{6}(?:\.(?:SH|SZ))?", symbol.upper()))
    for index, row in enumerate(rows):
        close = float(row["close"])
        previous_close = float(rows[index - 1]["close"]) if index else float(row["open"])
        row["change_pct"] = round((close / previous_close - 1) * 100, 4) if previous_close else None
        row["cumulative_pct"] = round((close / first_close - 1) * 100, 4) if first_close else None
        row["turnover_rate"] = row.get("turnover_rate")
        if row.get("amount") in (None, 0) and row.get("volume") and cn_lot_volume:
            typical = (float(row["high"]) + float(row["low"]) + close) / 3
            row["amount"] = round(typical * float(row["volume"]) * 100, 2)
            row["amount_estimated"] = True
        else:
            row["amount_estimated"] = False

        start = max(0, index - 8)
        highest = max(float(item["high"]) for item in rows[start:index + 1])
        lowest = min(float(item["low"]) for item in rows[start:index + 1])
        rsv = (close - lowest) / (highest - lowest) * 100 if highest > lowest else 50.0
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
        row["kdj_k"], row["kdj_d"], row["kdj_j"] = round(k, 4), round(d, 4), round(3 * k - 2 * d, 4)

        ema12 = close if ema12 is None else ema12 * 11 / 13 + close * 2 / 13
        ema26 = close if ema26 is None else ema26 * 25 / 27 + close * 2 / 27
        dif = ema12 - ema26
        dea = dif if dea is None else dea * 8 / 10 + dif * 2 / 10
        row["macd_dif"], row["macd_dea"] = round(dif, 6), round(dea, 6)
        row["macd_hist"] = round(2 * (dif - dea), 6)
    return rows


def _minute_source_rows(conn, symbol: str, interval_minutes: int = 1) -> list[dict]:
    tdx_mode = os.environ.get("ARGUS_MARKET_PROVIDER", "tdx").lower() == "tdx"
    source_order = ("CASE ds2.code WHEN 'tdx_local' THEN 0 WHEN 'tdx_public' THEN 1 ELSE 2 END,"
                    if tdx_mode else "")
    source_filter = " AND ds2.code IN ('tdx_local','tdx_public')" if tdx_mode else ""
    rows = conn.execute(
        f"""SELECT m.bar_time,m.open,m.high,m.low,m.close,m.volume,m.amount,m.bar_kind,ds.code AS source
           FROM minute_bars m JOIN data_sources ds ON ds.id=m.source_id
           WHERE m.asset_symbol=? AND m.interval_minutes=? AND m.source_id=(
             SELECT m2.source_id FROM minute_bars m2 JOIN data_sources ds2 ON ds2.id=m2.source_id
             WHERE m2.asset_symbol=? AND m2.interval_minutes=?{source_filter} GROUP BY m2.source_id
             ORDER BY {source_order}(julianday(MAX(m2.bar_time))-julianday(MIN(m2.bar_time))) DESC,
                      COUNT(*) DESC,MAX(m2.captured_at) DESC LIMIT 1)
           ORDER BY m.bar_time""", (symbol, interval_minutes, symbol, interval_minutes),
    ).fetchall()
    return [dict(row) for row in rows]


def _daily_source_rows(conn, symbol: str) -> list[dict]:
    tdx_mode = os.environ.get("ARGUS_MARKET_PROVIDER", "tdx").lower() == "tdx"
    source_order = ("CASE ds2.code WHEN 'tdx_local' THEN 0 WHEN 'tdx_public' THEN 1 ELSE 2 END,"
                    if tdx_mode else "")
    source_filter = " AND ds2.code IN ('tdx_local','tdx_public')" if tdx_mode else ""
    rows = conn.execute(
        f"""SELECT m.trade_date,m.open,m.high,m.low,m.close,m.volume,m.amount,ds.code AS source
           FROM market_daily_bars m JOIN data_sources ds ON ds.id=m.source_id
           WHERE m.asset_symbol=? AND m.adjust_mode='qfq' AND m.source_id=(
             SELECT m2.source_id FROM market_daily_bars m2 JOIN data_sources ds2 ON ds2.id=m2.source_id
             WHERE m2.asset_symbol=? AND m2.adjust_mode='qfq'{source_filter} GROUP BY m2.source_id
             ORDER BY {source_order}(julianday(MAX(m2.trade_date))-julianday(MIN(m2.trade_date))) DESC,
             COUNT(*) DESC,MAX(m2.captured_at) DESC LIMIT 1) ORDER BY m.trade_date""", (symbol, symbol),
    ).fetchall()
    if not rows and not tdx_mode:
        rows = conn.execute(
            """SELECT p.trade_date,p.open,p.high,p.low,p.close,p.volume,p.amount
               FROM prices p JOIN assets a ON a.id=p.asset_id WHERE a.symbol=?
               ORDER BY p.trade_date""", (symbol,),
        ).fetchall()
    return [dict(row) for row in rows]


def _coverage(rows: list[dict], source_rows: list[dict], source_interval_minutes: int | None,
              period: str) -> dict:
    """Describe actual stored coverage; never imply that missing history exists."""
    view_values = [str(row.get("time") or row.get("bar_time") or row.get("trade_date")) for row in rows]
    source_values = [str(row.get("bar_time") or row.get("trade_date") or row.get("time")) for row in source_rows]
    coverage_values = source_values or view_values
    dates = sorted({value[:10] for value in coverage_values if value})
    today = date.today()
    try:
        required_start = today.replace(year=today.year - 3)
    except ValueError:
        required_start = today.replace(year=today.year - 3, day=28)
    first_date = date.fromisoformat(dates[0]) if dates else None
    sources = sorted({str(row.get("source")) for row in source_rows if row.get("source")})
    exempt = period == "5d"
    return {
        "required_years": None if exempt else 3,
        "required_start": required_start.isoformat(),
        "first_bar": coverage_values[0] if coverage_values else None,
        "last_bar": coverage_values[-1] if coverage_values else None,
        "view_first_bar": view_values[0] if view_values else None,
        "view_last_bar": view_values[-1] if view_values else None,
        "row_count": len(rows),
        "source_row_count": len(source_rows),
        "trading_days": len(dates),
        "requirement_exempt": exempt,
        "meets_required_history": None if exempt else bool(first_date and first_date <= required_start),
        "sources": sources,
        "source_interval_minutes": source_interval_minutes,
        "storage": "local_sqlite",
    }


def build_chart_series(conn, symbol: str, period: str) -> dict:
    if period not in SUPPORTED_PERIODS:
        raise ValueError(f"unsupported period: {period}")
    source_interval_minutes = None
    coverage_source = []
    if period in {"time", "5d"} or period.endswith("m") and period != "1mo":
        requested_minutes = None if period in {"time", "5d"} else int(period[:-1])
        native_minutes = 60 if requested_minutes == 120 else requested_minutes
        source = _minute_source_rows(conn, symbol, native_minutes or 1)
        if not source and native_minutes not in (None, 1):
            native_minutes = 1
            source = _minute_source_rows(conn, symbol, 1)
        source_interval_minutes = native_minutes or 1
        coverage_source = list(source)
        if period == "time" and source:
            latest = max(row["bar_time"][:10] for row in source)
            source = [row for row in source if row["bar_time"].startswith(latest)]
        elif period == "5d" and source:
            latest_dates = sorted({row["bar_time"][:10] for row in source})[-5:]
            source = [row for row in source if row["bar_time"][:10] in latest_dates]
            coverage_source = list(source)
        if period in {"time", "5d"}:
            rows = [{"time": row["bar_time"], "open": row["close"], "high": row["close"],
                     "low": row["close"], "close": row["close"], "volume": row["volume"],
                     "amount": row["amount"], "ma5": None} for row in source
                    if "09:30" <= row["bar_time"][11:16] <= "11:30" or "13:00" <= row["bar_time"][11:16] <= "15:00"]
            chart_type = "line"
        else:
            if native_minutes == requested_minutes:
                rows = add_ma5([{**row, "time": row["bar_time"]} for row in source])
            else:
                rows = add_ma5(aggregate_intraday(source, requested_minutes, native_minutes or 1))
            chart_type = "candlestick"
    else:
        source = _daily_source_rows(conn, symbol)
        coverage_source = source
        rows = add_ma5(aggregate_calendar(source, period))
        chart_type = "candlestick"
    add_technical_indicators(rows, symbol)
    return {"period": period, "period_label": SUPPORTED_PERIODS[period],
            "chart_type": chart_type, "series": rows, "supports_ma5": chart_type == "candlestick",
            "coverage": _coverage(rows, coverage_source, source_interval_minutes, period)}
