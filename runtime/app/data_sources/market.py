"""Zero-account A-share real-time and intraday market-data pipeline.

Tencent's public quote pages are the first online source.  A narrowly scoped
Eastmoney request is the second online source.  Both are unofficially exposed
web interfaces rather than contractual APIs, so every response is persisted,
validated, and served from SQLite.  The web application never waits on an
upstream website and never imports a trading function.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from ..db import DATA_LAKE, connect, ensure_data_lake, initialize

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_SYMBOLS = ("000001.SH", "512400", "562500", "600519")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ArgusMarketData/0.2"
HISTORY_INTERVALS = (1, 5, 15, 30, 60)
HISTORY_DAYS = 366 * 3
HISTORY_MAX_BARS = {1: 190_000, 5: 38_000, 15: 13_000, 30: 7_000, 60: 4_000}
TDX_PUBLIC_SERVERS = (
    ("180.153.18.170", 7709),
    ("119.29.19.242", 7709),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if re.fullmatch(r"(?:SH|SZ|BJ)\d{6}", value):
        return value[2:] if value[2:] != "000001" else f"{value[2:]}.{value[:2]}"
    if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", value):
        return value
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError(f"unsupported A-share symbol: {symbol!r}")
    return value


def provider_code(symbol: str) -> str:
    symbol = normalize_symbol(symbol)
    if "." in symbol:
        code, exchange = symbol.split(".", 1)
        return exchange.lower() + code
    if symbol.startswith(("5", "6", "9")):
        return "sh" + symbol
    if symbol.startswith(("4", "8")):
        return "bj" + symbol
    return "sz" + symbol


def eastmoney_secid(symbol: str) -> str:
    code = provider_code(symbol)
    market = "1" if code.startswith("sh") else "0"
    return f"{market}.{code[2:]}"


def _request(url: str, timeout: float = 10.0) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Referer": "https://quote.eastmoney.com/"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _raw_path(source: str, dataset: str, symbol: str = "batch", suffix: str = "json") -> Path:
    stamp = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%f")
    target = DATA_LAKE / "raw" / "market" / source / normalize_symbol(symbol) if symbol != "batch" else DATA_LAKE / "raw" / "market" / source / "batch"
    target.mkdir(parents=True, exist_ok=True)
    return target / f"{dataset}_{stamp}.{suffix}"


def _float(value, default=None):
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


def _iso_market_time(value: str) -> str:
    parsed = datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
    return parsed.isoformat()


def _validate_quote(item: dict) -> None:
    if not item["price"] or item["price"] <= 0:
        raise ValueError(f"invalid price for {item['symbol']}: {item['price']}")
    high, low = item.get("high"), item.get("low")
    if high and low and high < low:
        raise ValueError(f"invalid high/low for {item['symbol']}: {high}/{low}")
    if high and item["price"] > high * 1.001:
        raise ValueError(f"price above high for {item['symbol']}")
    if low and item["price"] < low * .999:
        raise ValueError(f"price below low for {item['symbol']}")


def market_provider_mode() -> str:
    mode = os.environ.get("ARGUS_MARKET_PROVIDER", "tdx").strip().lower()
    if mode not in {"tdx", "mixed"}:
        raise ValueError("ARGUS_MARKET_PROVIDER must be tdx or mixed")
    return mode


def _tdx_instrument(symbol: str) -> tuple[str, int, bool, bool]:
    from tdxrs.constants import MARKET_BJ, MARKET_SH, MARKET_SZ

    normalized = normalize_symbol(symbol)
    provider = provider_code(normalized)
    market = MARKET_SH if provider.startswith("sh") else (MARKET_BJ if provider.startswith("bj") else MARKET_SZ)
    code = provider[2:]
    is_index = "." in normalized
    is_fund = not is_index and (code.startswith("5") or code.startswith(("15", "16")))
    return code, market, is_index, is_fund


def _tdx_observed_at(server_time: str | None, current: datetime | None = None) -> str:
    now = current or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    else:
        now = now.astimezone(SHANGHAI)
    match = re.search(r"(\d{2}):(\d{2}):(\d{2})", str(server_time or ""))
    if not match:
        return now.isoformat()
    hour, minute, second = (int(value) for value in match.groups())
    if hour > 23 or minute > 59 or second > 59:
        return now.isoformat()
    observed = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    # Before the next session opens, TDX nodes may still return the previous
    # close's clock time without a date. Do not stamp that value into the future.
    if observed > now + timedelta(minutes=30):
        observed -= timedelta(days=1)
        while observed.weekday() >= 5:
            observed -= timedelta(days=1)
    return observed.isoformat()


def _tdx_quote_item(symbol: str, row: dict, name: str | None = None) -> dict:
    price = float(row.get("price") or 0)
    previous_close = float(row.get("last_close") or 0)
    item = {
        "symbol": normalize_symbol(symbol),
        "name": name or normalize_symbol(symbol),
        "observed_at": _tdx_observed_at(row.get("servertime")),
        "price": price,
        "previous_close": previous_close,
        "open": _float(row.get("open")),
        "high": _float(row.get("high")),
        "low": _float(row.get("low")),
        # TDX quote/time-line volume uses lots; normalized storage uses shares.
        "volume": _float(row.get("vol"), 0) * 100,
        "amount": _float(row.get("amount")),
        "turnover_rate": None,
        "change_value": price - previous_close if previous_close else None,
        "change_pct": (price / previous_close - 1) * 100 if previous_close else None,
        "source": "tdx_public",
    }
    _validate_quote(item)
    return item


def _tdx_fetch_quote_group(instruments: list[tuple[str, int, str]], fund_mode: bool) -> tuple[list[dict], str]:
    from tdxrs import TdxHqClient, TdxHqFundClient

    last_error = None
    for host, port in TDX_PUBLIC_SERVERS:
        client = TdxHqFundClient() if fund_mode else TdxHqClient()
        try:
            client.connect(host, port, timeout=6)
            pairs = [(market, code) for _symbol, market, code in instruments]
            rows = client.get_fund_quotes(pairs) if fund_mode else client.get_security_quotes(pairs)
            if rows:
                return list(rows), f"{host}:{port}"
            raise ValueError("TDX returned no quotes")
        except Exception as exc:
            last_error = exc
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
    raise RuntimeError(f"all TDX public nodes failed for quotes: {last_error!r}")


def fetch_tdx_quotes(symbols: Iterable[str], names: dict[str, str] | None = None) -> tuple[list[dict], bytes]:
    requested = list(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols))
    names = names or {}
    groups: dict[bool, list[tuple[str, int, str]]] = {False: [], True: []}
    for symbol in requested:
        code, market, _is_index, is_fund = _tdx_instrument(symbol)
        groups[is_fund].append((symbol, market, code))
    results = []
    servers = []
    for fund_mode, instruments in groups.items():
        if not instruments:
            continue
        try:
            rows, server = _tdx_fetch_quote_group(instruments, fund_mode)
        except RuntimeError:
            if not fund_mode:
                raise
            rows, server = _tdx_fetch_quote_group(instruments, False)
        servers.append(server)
        by_code = {str(row.get("code")): row for row in rows}
        for symbol, _market, code in instruments:
            row = by_code.get(code)
            if row:
                results.append(_tdx_quote_item(symbol, row, names.get(symbol)))
    if not results:
        raise ValueError("TDX returned no valid requested quotes")
    raw = json.dumps({"provider": "tdx_public", "servers": servers, "quotes": results},
                     ensure_ascii=False).encode("utf-8")
    return results, raw


def _tdx_time_rows(symbol: str, trade_date: str, points: Iterable[dict]) -> list[dict]:
    rows_by_time = {}
    for point in points:
        clock = str(point.get("time") or "")[:5]
        if not re.fullmatch(r"\d{2}:\d{2}", clock):
            continue
        price = float(point.get("price") or 0)
        if price <= 0:
            continue
        volume = max(0.0, float(point.get("vol") or 0) * 100)
        when = datetime.strptime(f"{trade_date} {clock}", "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI).isoformat()
        rows_by_time[when] = {
            "symbol": normalize_symbol(symbol), "bar_time": when, "interval_minutes": 1,
            "open": price, "high": price, "low": price, "close": price,
            "volume": volume, "amount": price * volume,
            "bar_kind": "LAST_PRICE_POINT", "source": "tdx_public",
        }
    return [rows_by_time[key] for key in sorted(rows_by_time)]


def _tdx_observable_bar_time(stamp: str, observed_at: datetime | None = None) -> str | None:
    when = datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
    return when.isoformat() if when <= (observed_at or datetime.now(SHANGHAI)) else None


def fetch_tdx_current_minutes(symbol: str) -> tuple[list[dict], bytes]:
    from tdxrs import TdxHqClient, TdxHqFundClient
    from tdxrs.constants import KLINE_DAILY

    symbol = normalize_symbol(symbol)
    code, market, _is_index, is_fund = _tdx_instrument(symbol)
    modes = ("fund", "security") if is_fund else ("security",)
    last_error = None
    for host, port in TDX_PUBLIC_SERVERS:
        for mode in modes:
            client = TdxHqFundClient() if mode == "fund" else TdxHqClient()
            try:
                client.connect(host, port, timeout=6)
                if mode == "fund":
                    daily = client.get_fund_bars(KLINE_DAILY, market, code, 0, 1)
                    points = client.get_fund_minute_time_data(market, code)
                else:
                    daily = client.get_security_bars(KLINE_DAILY, market, code, 0, 1, 0)
                    points = client.get_minute_time_data(market, code)
                if not daily or not points:
                    raise ValueError("TDX returned no current minute data")
                trade_date = str(daily[-1]["datetime"])[:10]
                rows = _tdx_time_rows(symbol, trade_date, points)
                if not rows:
                    raise ValueError("TDX returned no valid current minute points")
                raw = json.dumps({"provider": "tdx_public", "server": f"{host}:{port}",
                                  "mode": mode, "trade_date": trade_date, "points": list(points)},
                                 ensure_ascii=False).encode("utf-8")
                return rows, raw
            except Exception as exc:
                last_error = exc
            finally:
                try:
                    client.disconnect()
                except Exception:
                    pass
    raise RuntimeError(f"all TDX public nodes failed for current minutes {symbol}: {last_error!r}")


def fetch_tdx_recent_minutes(symbol: str, trading_days: int = 5) -> tuple[list[dict], bytes]:
    from tdxrs import TdxHqClient, TdxHqFundClient
    from tdxrs.constants import KLINE_DAILY

    symbol = normalize_symbol(symbol)
    code, market, _is_index, is_fund = _tdx_instrument(symbol)
    modes = ("fund", "security") if is_fund else ("security",)
    trading_days = max(1, min(int(trading_days), 10))
    last_error = None
    for host, port in TDX_PUBLIC_SERVERS:
        for mode in modes:
            client = TdxHqFundClient() if mode == "fund" else TdxHqClient()
            try:
                client.connect(host, port, timeout=6)
                daily = (client.get_fund_bars(KLINE_DAILY, market, code, 0, trading_days)
                         if mode == "fund" else
                         client.get_security_bars(KLINE_DAILY, market, code, 0, trading_days, 0))
                dates = sorted({str(bar["datetime"])[:10] for bar in daily})[-trading_days:]
                if not dates:
                    raise ValueError("TDX returned no trading dates for minute history")
                rows = []
                raw_days = []
                for trade_date in dates:
                    date_value = int(trade_date.replace("-", ""))
                    if trade_date == dates[-1]:
                        points = (client.get_fund_minute_time_data(market, code) if mode == "fund"
                                  else client.get_minute_time_data(market, code))
                    else:
                        points = (client.get_fund_history_minute_time_data(market, code, date_value)
                                  if mode == "fund" else
                                  client.get_history_minute_time_data(market, code, date_value))
                    day_rows = _tdx_time_rows(symbol, trade_date, points)
                    if day_rows:
                        rows.extend(day_rows)
                        raw_days.append({"trade_date": trade_date, "points": list(points)})
                if not rows:
                    raise ValueError("TDX returned no valid recent minute points")
                raw = json.dumps({"provider": "tdx_public", "server": f"{host}:{port}",
                                  "mode": mode, "days": raw_days}, ensure_ascii=False).encode("utf-8")
                return rows, raw
            except Exception as exc:
                last_error = exc
            finally:
                try:
                    client.disconnect()
                except Exception:
                    pass
    raise RuntimeError(f"all TDX public nodes failed for recent minutes {symbol}: {last_error!r}")


def fetch_tdx_daily(symbol: str, count: int = 1000) -> tuple[list[dict], bytes]:
    from tdxrs import TdxHqClient, TdxHqFundClient
    from tdxrs.constants import KLINE_DAILY

    symbol = normalize_symbol(symbol)
    code, market, _is_index, is_fund = _tdx_instrument(symbol)
    modes = ("fund", "security") if is_fund else ("security",)
    count = max(50, min(int(count), 2000))
    last_error = None
    for host, port in TDX_PUBLIC_SERVERS:
        for mode in modes:
            client = TdxHqFundClient() if mode == "fund" else TdxHqClient()
            try:
                client.connect(host, port, timeout=6)
                collected = {}
                for offset in range(0, count + 800, 800):
                    page = (client.get_fund_bars(KLINE_DAILY, market, code, offset, 800)
                            if mode == "fund" else
                            client.get_security_bars(KLINE_DAILY, market, code, offset, 800, 1))
                    if not page:
                        break
                    collected.update({str(bar["datetime"])[:10]: bar for bar in page})
                    if len(page) < 800 or len(collected) >= count:
                        break
                source_rows = [collected[key] for key in sorted(collected)][-count:]
                rows = [{
                    "symbol": symbol, "trade_date": str(bar["datetime"])[:10], "adjust_mode": "qfq",
                    "open": float(bar["open"]), "high": float(bar["high"]),
                    "low": float(bar["low"]), "close": float(bar["close"]),
                    "volume": float(bar.get("vol") or 0), "amount": float(bar.get("amount") or 0),
                    "source": "tdx_public",
                } for bar in source_rows if float(bar.get("close") or 0) > 0]
                if not rows:
                    raise ValueError("TDX returned no valid daily bars")
                raw = json.dumps({"provider": "tdx_public", "server": f"{host}:{port}",
                                  "mode": mode, "adjust_mode": "qfq", "rows": source_rows},
                                 ensure_ascii=False).encode("utf-8")
                return rows, raw
            except Exception as exc:
                last_error = exc
            finally:
                try:
                    client.disconnect()
                except Exception:
                    pass
    raise RuntimeError(f"all TDX public nodes failed for daily bars {symbol}: {last_error!r}")


def fetch_tencent_quotes(symbols: Iterable[str]) -> tuple[list[dict], bytes]:
    requested = [normalize_symbol(symbol) for symbol in symbols]
    codes = [provider_code(symbol) for symbol in requested]
    raw = _request("https://qt.gtimg.cn/q=" + ",".join(codes), timeout=8)
    text = raw.decode("gbk", errors="replace")
    found = {match.group(1): match.group(2).split("~") for match in re.finditer(r'v_([a-z0-9]+)="([^"]*)";', text)}
    results = []
    for symbol, code in zip(requested, codes):
        fields = found.get(code)
        if not fields or len(fields) < 35:
            continue
        item = {
            "symbol": symbol,
            "name": fields[1],
            "observed_at": _iso_market_time(fields[30]),
            "price": _float(fields[3]),
            "previous_close": _float(fields[4]),
            "open": _float(fields[5]),
            "volume": _float(fields[6]),
            "change_value": _float(fields[31]),
            "change_pct": _float(fields[32]),
            "high": _float(fields[33]),
            "low": _float(fields[34]),
            "amount": _float(fields[37]) if len(fields) > 37 else None,
            "turnover_rate": _float(fields[38]) if len(fields) > 38 else None,
            "source": "tencent",
        }
        _validate_quote(item)
        results.append(item)
    if not results:
        raise ValueError("Tencent returned no valid requested quotes")
    return results, raw


def fetch_eastmoney_quote(symbol: str) -> tuple[dict, bytes]:
    symbol = normalize_symbol(symbol)
    params = urllib.parse.urlencode({
        "secid": eastmoney_secid(symbol),
        "fields": "f57,f58,f43,f44,f45,f46,f47,f48,f60,f59,f86,f169,f170",
    })
    raw = _request("https://push2.eastmoney.com/api/qt/stock/get?" + params, timeout=8)
    payload = json.loads(raw)
    data = payload.get("data") or {}
    decimals = int(data.get("f59") or 2)
    scale = 10 ** decimals
    observed = datetime.fromtimestamp(int(data["f86"]), SHANGHAI).isoformat()
    item = {
        "symbol": symbol,
        "name": data.get("f58") or symbol,
        "observed_at": observed,
        "price": _float(data.get("f43"), 0) / scale,
        "previous_close": _float(data.get("f60"), 0) / scale,
        "open": _float(data.get("f46"), 0) / scale,
        "high": _float(data.get("f44"), 0) / scale,
        "low": _float(data.get("f45"), 0) / scale,
        "volume": _float(data.get("f47")),
        "amount": _float(data.get("f48")),
        "turnover_rate": None,
        "change_value": _float(data.get("f169"), 0) / scale,
        "change_pct": _float(data.get("f170"), 0) / 100,
        "source": "eastmoney",
    }
    _validate_quote(item)
    return item, raw


def fetch_tencent_minutes(symbol: str) -> tuple[list[dict], bytes]:
    """Return five trading days of one-minute last-price points.

    Tencent exposes cumulative volume/amount on this endpoint.  We convert them
    to per-minute deltas; OHLC fields are intentionally equal to the last price
    and ``bar_kind`` makes that limitation explicit.
    """
    symbol = normalize_symbol(symbol)
    code = provider_code(symbol)
    raw = _request(f"https://web.ifzq.gtimg.cn/appstock/app/day/query?code={code}", timeout=10)
    payload = json.loads(raw)
    days = ((payload.get("data") or {}).get(code) or {}).get("data") or []
    rows = []
    for day in days:
        date_text = day["date"]
        previous_volume = previous_amount = 0.0
        for line in day.get("data") or []:
            fields = line.split()
            if len(fields) < 4:
                continue
            price = float(fields[1])
            cumulative_volume, cumulative_amount = float(fields[2]), float(fields[3])
            volume = max(0.0, cumulative_volume - previous_volume)
            amount = max(0.0, cumulative_amount - previous_amount)
            previous_volume, previous_amount = cumulative_volume, cumulative_amount
            bar_time = datetime.strptime(date_text + fields[0], "%Y%m%d%H%M").replace(tzinfo=SHANGHAI).isoformat()
            rows.append({"symbol": symbol, "bar_time": bar_time, "interval_minutes": 1,
                         "open": price, "high": price, "low": price, "close": price,
                         "volume": volume, "amount": amount, "bar_kind": "LAST_PRICE_POINT",
                         "source": "tencent"})
    if not rows:
        raise ValueError(f"Tencent returned no minute points for {symbol}")
    rows.sort(key=lambda item: item["bar_time"])
    return rows, raw


def fetch_tencent_daily(symbol: str, count: int = 1000) -> tuple[list[dict], bytes]:
    """Fetch roughly four years of forward-adjusted daily candles."""
    symbol = normalize_symbol(symbol)
    code = provider_code(symbol)
    count = max(50, min(int(count), 2000))
    # Tencent silently caps a single adjusted-price response at 640 rows.
    # Page backwards by end date so a fresh install still obtains >=3 years.
    source_rows_by_date: dict[str, list] = {}
    raw_pages = []
    end_date = ""
    while len(source_rows_by_date) < count and len(raw_pages) < 4:
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
            f"{code},day,,{end_date},640,qfq"
        )
        page_raw = _request(url, timeout=12)
        payload = json.loads(page_raw)
        raw_pages.append(payload)
        data = ((payload.get("data") or {}).get(code) or {})
        page_rows = data.get("qfqday") or data.get("day") or []
        if not page_rows:
            break
        previous_size = len(source_rows_by_date)
        source_rows_by_date.update({fields[0]: fields for fields in page_rows if fields})
        if len(source_rows_by_date) == previous_size:
            break
        oldest = min(source_rows_by_date)
        end_date = (datetime.strptime(oldest, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    source_rows = [source_rows_by_date[key] for key in sorted(source_rows_by_date)][-count:]
    rows = []
    for fields in source_rows:
        if len(fields) < 6:
            continue
        item = {"symbol": symbol, "trade_date": fields[0], "adjust_mode": "qfq",
                "open": float(fields[1]), "close": float(fields[2]),
                "high": float(fields[3]), "low": float(fields[4]),
                "volume": float(fields[5]), "amount": None, "source": "tencent"}
        if item["close"] <= 0 or item["high"] < item["low"]:
            raise ValueError(f"invalid Tencent daily row for {symbol}: {fields}")
        rows.append(item)
    if not rows:
        raise ValueError(f"Tencent returned no daily candles for {symbol}")
    raw = json.dumps({"provider": "tencent", "pages": raw_pages}, ensure_ascii=False).encode("utf-8")
    return rows, raw


def fetch_eastmoney_minutes(symbol: str) -> tuple[list[dict], bytes]:
    symbol = normalize_symbol(symbol)
    params = urllib.parse.urlencode({
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ut": "7eea3edcaed734bea9cbfc24409ed989", "ndays": "5", "iscr": "0",
        "secid": eastmoney_secid(symbol),
    })
    raw = _request("https://push2his.eastmoney.com/api/qt/stock/trends2/get?" + params, timeout=10)
    payload = json.loads(raw)
    trends = (payload.get("data") or {}).get("trends") or []
    rows = []
    for line in trends:
        fields = line.split(",")
        if len(fields) < 7:
            continue
        when = datetime.strptime(fields[0], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI).isoformat()
        item = {"symbol": symbol, "bar_time": when, "interval_minutes": 1,
                "open": float(fields[1]), "close": float(fields[2]),
                "high": float(fields[3]), "low": float(fields[4]),
                "volume": float(fields[5]), "amount": float(fields[6]),
                "bar_kind": "OHLC", "source": "eastmoney"}
        if item["high"] < item["low"] or item["close"] <= 0:
            raise ValueError(f"invalid Eastmoney minute row for {symbol}: {line}")
        rows.append(item)
    if not rows:
        raise ValueError(f"Eastmoney returned no minute bars for {symbol}")
    return rows, raw


def fetch_eastmoney_history_minutes(symbol: str, interval_minutes: int,
                                    start_date: str | None = None,
                                    end_date: str | None = None) -> tuple[list[dict], bytes]:
    """Fetch native historical intraday OHLC bars, not the five-day trends feed."""
    symbol = normalize_symbol(symbol)
    if interval_minutes not in HISTORY_INTERVALS:
        raise ValueError(f"unsupported historical minute interval: {interval_minutes}")
    now = datetime.now(SHANGHAI)
    start_date = start_date or (now - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")
    end_date = end_date or now.strftime("%Y%m%d")
    params = urllib.parse.urlencode({
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": str(interval_minutes), "fqt": "0", "secid": eastmoney_secid(symbol),
        "beg": start_date.replace("-", "")[:8], "end": end_date.replace("-", "")[:8],
    })
    raw = _request("https://push2his.eastmoney.com/api/qt/stock/kline/get?" + params, timeout=20)
    payload = json.loads(raw)
    lines = ((payload.get("data") or {}).get("klines") or [])
    rows = []
    for line in lines:
        fields = line.split(",")
        if len(fields) < 7:
            continue
        when = datetime.strptime(fields[0], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI).isoformat()
        item = {"symbol": symbol, "bar_time": when, "interval_minutes": interval_minutes,
                "open": float(fields[1]), "close": float(fields[2]),
                "high": float(fields[3]), "low": float(fields[4]),
                "volume": float(fields[5]), "amount": float(fields[6]),
                "bar_kind": "OHLC", "source": "eastmoney"}
        if item["close"] <= 0 or item["high"] < item["low"]:
            raise ValueError(f"invalid Eastmoney historical minute row for {symbol}: {line}")
        rows.append(item)
    if not rows:
        raise ValueError(f"Eastmoney returned no {interval_minutes}-minute history for {symbol}")
    rows.sort(key=lambda item: item["bar_time"])
    return rows, raw


def fetch_tdx_history_minutes(symbol: str, interval_minutes: int,
                              start_date: str | None = None) -> tuple[list[dict], bytes]:
    """Fetch historical bars from free TDX TCP nodes for stocks, ETFs and indices."""
    try:
        from tdxrs import TdxHqClient, TdxHqFundClient
        from tdxrs.constants import (
            KLINE_1MIN, KLINE_5MIN, KLINE_15MIN, KLINE_30MIN, KLINE_1HOUR,
            MARKET_SH, MARKET_SZ,
        )
    except ImportError as exc:
        raise RuntimeError("tdxrs is not installed; run pip install -r requirements-data.txt") from exc
    symbol = normalize_symbol(symbol)
    if interval_minutes not in HISTORY_INTERVALS:
        raise ValueError(f"unsupported TDX historical interval: {interval_minutes}")
    if interval_minutes == 1:
        return fetch_tdx_recent_minutes(symbol, 5)
    provider = provider_code(symbol)
    code, market = provider[2:], MARKET_SH if provider.startswith("sh") else MARKET_SZ
    category = {
        1: KLINE_1MIN, 5: KLINE_5MIN, 15: KLINE_15MIN,
        30: KLINE_30MIN, 60: KLINE_1HOUR,
    }[interval_minutes]
    is_index = "." in symbol
    is_fund = not is_index and (code.startswith("5") or code.startswith(("15", "16")))
    cutoff = start_date or (datetime.now(SHANGHAI) - timedelta(days=HISTORY_DAYS)).date().isoformat()
    last_error = None
    for host, port in TDX_PUBLIC_SERVERS:
        # Some newer ETFs are exposed by a TDX node's normal security API even
        # when the fund-specific code validator has not learned the code yet.
        client_modes = ("fund", "security") if is_fund else ("security",)
        for client_mode in client_modes:
            client = TdxHqFundClient() if client_mode == "fund" else TdxHqClient()
            try:
                client.connect(host, port, timeout=6)
                collected = {}
                for offset in range(0, HISTORY_MAX_BARS[interval_minutes] + 800, 800):
                    if client_mode == "fund":
                        page = client.get_fund_bars(category, market, code, offset, 800)
                    elif is_index:
                        page = client.get_index_bars(category, market, code, offset, 800, 0)
                    else:
                        page = client.get_security_bars(category, market, code, offset, 800, 0)
                    if not page:
                        break
                    for bar in page:
                        if bar["datetime"][:10] >= cutoff:
                            collected[bar["datetime"]] = bar
                    if len(page) < 800 or page[0]["datetime"][:10] < cutoff:
                        break
                if not collected:
                    raise ValueError(f"TDX node {host} returned no {interval_minutes}-minute history for {symbol}")
                rows = []
                observed_at = datetime.now(SHANGHAI)
                for stamp in sorted(collected):
                    bar = collected[stamp]
                    when = _tdx_observable_bar_time(stamp, observed_at)
                    if not when:
                        continue
                    rows.append({"symbol": symbol, "bar_time": when, "interval_minutes": interval_minutes,
                                 "open": float(bar["open"]), "high": float(bar["high"]),
                                 "low": float(bar["low"]), "close": float(bar["close"]),
                                 "volume": float(bar.get("vol") or 0), "amount": float(bar.get("amount") or 0),
                                 "bar_kind": "OHLC", "source": "tdx_public"})
                raw = json.dumps({"provider": "tdx_public", "server": f"{host}:{port}",
                                  "client_mode": client_mode, "interval_minutes": interval_minutes, "rows": rows},
                                 ensure_ascii=False).encode("utf-8")
                return rows, raw
            except Exception as exc:
                last_error = exc
            finally:
                try:
                    client.disconnect()
                except Exception:
                    pass
    raise RuntimeError(f"all TDX public nodes failed for {symbol}/{interval_minutes}m: {last_error!r}")


def fetch_baostock_history_minutes(symbol: str, interval_minutes: int,
                                    start_date: str | None = None,
                                    end_date: str | None = None) -> tuple[list[dict], bytes]:
    """Fetch BaoStock intraday history where the asset class is supported."""
    if interval_minutes not in (5, 15, 30, 60):
        raise ValueError(f"BaoStock does not provide {interval_minutes}-minute history")
    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("baostock is not installed; run pip install -r requirements-data.txt") from exc
    symbol = normalize_symbol(symbol)
    provider = provider_code(symbol)
    code = f"{provider[:2]}.{provider[2:]}"
    now = datetime.now(SHANGHAI)
    start_date = start_date or (now - timedelta(days=HISTORY_DAYS)).date().isoformat()
    end_date = end_date or now.date().isoformat()
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_code} {login.error_msg}")
    records = []
    try:
        result = bs.query_history_k_data_plus(
            code, "date,time,code,open,high,low,close,volume,amount",
            start_date=start_date, end_date=end_date, frequency=str(interval_minutes), adjustflag="3",
        )
        if result.error_code != "0":
            raise RuntimeError(f"BaoStock query failed: {result.error_code} {result.error_msg}")
        while result.next():
            records.append(dict(zip(result.fields, result.get_row_data())))
    finally:
        bs.logout()
    rows = []
    for record in records:
        stamp = str(record.get("time") or "")[:12]
        if len(stamp) != 12:
            continue
        when = datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=SHANGHAI).isoformat()
        item = {
            "symbol": symbol, "bar_time": when, "interval_minutes": interval_minutes,
            "open": float(record["open"]), "high": float(record["high"]),
            "low": float(record["low"]), "close": float(record["close"]),
            "volume": float(record.get("volume") or 0), "amount": float(record.get("amount") or 0),
            "bar_kind": "OHLC", "source": "baostock",
        }
        if item["close"] > 0 and item["high"] >= item["low"]:
            rows.append(item)
    if not rows:
        raise ValueError(f"BaoStock returned no {interval_minutes}-minute history for {symbol}")
    rows.sort(key=lambda item: item["bar_time"])
    raw = json.dumps({"provider": "baostock", "interval_minutes": interval_minutes,
                      "query": {"code": code, "start": start_date, "end": end_date},
                      "records": records}, ensure_ascii=False).encode("utf-8")
    return rows, raw


def _record_run(conn, source_id: int, dataset: str, symbol: str, status: str,
                started_at: str, rows: int, raw_path: str | None, error: str | None = None) -> None:
    conn.execute(
        """INSERT INTO ingestion_runs(source_id,dataset,asset_symbol,started_at,finished_at,status,row_count,raw_path,error)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (source_id, dataset, symbol, started_at, utc_now(), status, rows, raw_path, error),
    )


def _write_quotes(items: list[dict], raw: bytes, source: str) -> dict:
    path = _raw_path(source, "quotes", suffix="txt" if source == "tencent" else "json")
    path.write_bytes(raw)
    captured = utc_now()
    with closing(connect()) as conn:
        source_row = conn.execute("SELECT id FROM data_sources WHERE code=?", (source,)).fetchone()
        source_id = source_row[0]
        for item in items:
            conn.execute(
                """INSERT INTO quote_snapshots(asset_symbol,asset_name,observed_at,price,previous_close,open,high,low,
                   change_value,change_pct,volume,amount,turnover_rate,source_id,captured_at,raw_path)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET
                   asset_name=excluded.asset_name,price=excluded.price,previous_close=excluded.previous_close,
                   open=excluded.open,high=excluded.high,low=excluded.low,change_value=excluded.change_value,
                   change_pct=excluded.change_pct,volume=excluded.volume,amount=excluded.amount,
                   turnover_rate=excluded.turnover_rate,
                   captured_at=excluded.captured_at,raw_path=excluded.raw_path""",
                (item["symbol"], item["name"], item["observed_at"], item["price"], item["previous_close"],
                 item["open"], item["high"], item["low"], item["change_value"], item["change_pct"],
                 item["volume"], item["amount"], item.get("turnover_rate"), source_id, captured, str(path)),
            )
            conn.execute(
                """UPDATE positions SET current_price=?,highest_since_entry=MAX(highest_since_entry,?),
                   valuation_status='AVAILABLE',price_observed_at=?,price_source=?
                   WHERE asset_id=(SELECT id FROM assets WHERE symbol=?)
                     AND verification_status='USER_CONFIRMED'""",
                (item["price"], item["price"], item["observed_at"], source, item["symbol"]),
            )
        _record_run(conn, source_id, "realtime_quotes", ",".join(i["symbol"] for i in items),
                    "SUCCESS", captured, len(items), str(path))
        conn.execute("UPDATE data_sources SET health_status='HEALTHY',last_success_at=?,last_error=NULL WHERE id=?",
                     (captured, source_id))
        conn.commit()
    return {"source": source, "rows": len(items), "raw_path": str(path)}


def _write_minutes(symbol: str, rows: list[dict], raw: bytes, source: str) -> dict:
    intervals = {int(row["interval_minutes"]) for row in rows}
    if len(intervals) != 1:
        raise ValueError(f"minute write must contain one interval, got {sorted(intervals)}")
    interval = intervals.pop()
    dataset = f"minute_{interval}"
    path = _raw_path(source, dataset, symbol)
    path.write_bytes(raw)
    captured = utc_now()
    with closing(connect()) as conn:
        source_id = conn.execute("SELECT id FROM data_sources WHERE code=?", (source,)).fetchone()[0]
        conn.executemany(
            """INSERT INTO minute_bars(asset_symbol,bar_time,interval_minutes,open,high,low,close,volume,amount,
               bar_kind,source_id,captured_at,raw_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
               volume=excluded.volume,amount=excluded.amount,bar_kind=excluded.bar_kind,
               captured_at=excluded.captured_at,raw_path=excluded.raw_path""",
            [(r["symbol"], r["bar_time"], r["interval_minutes"], r["open"], r["high"], r["low"], r["close"],
              r["volume"], r["amount"], r["bar_kind"], source_id, captured, str(path)) for r in rows],
        )
        _record_run(conn, source_id, dataset, symbol, "SUCCESS", captured, len(rows), str(path))
        conn.execute("UPDATE data_sources SET health_status='HEALTHY',last_success_at=?,last_error=NULL WHERE id=?",
                     (captured, source_id))
        conn.commit()
    dates = sorted({row["bar_time"][:10] for row in rows})
    return {"symbol": symbol, "source": source, "interval_minutes": interval, "rows": len(rows),
            "first_bar": rows[0]["bar_time"], "last_bar": rows[-1]["bar_time"],
            "trading_days": len(dates), "raw_path": str(path)}


def _write_daily(symbol: str, rows: list[dict], raw: bytes, source: str) -> dict:
    path = _raw_path(source, "daily_qfq", symbol)
    path.write_bytes(raw)
    captured = utc_now()
    with closing(connect()) as conn:
        source_id = conn.execute("SELECT id FROM data_sources WHERE code=?", (source,)).fetchone()[0]
        conn.executemany(
            """INSERT INTO market_daily_bars(asset_symbol,trade_date,adjust_mode,open,high,low,close,volume,amount,
               source_id,captured_at,raw_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
               volume=excluded.volume,amount=excluded.amount,captured_at=excluded.captured_at,raw_path=excluded.raw_path""",
            [(r["symbol"], r["trade_date"], r["adjust_mode"], r["open"], r["high"], r["low"], r["close"],
              r["volume"], r["amount"], source_id, captured, str(path)) for r in rows],
        )
        _record_run(conn, source_id, "daily_qfq", symbol, "SUCCESS", captured, len(rows), str(path))
        conn.execute("UPDATE data_sources SET health_status='HEALTHY',last_success_at=?,last_error=NULL WHERE id=?",
                     (captured, source_id))
        conn.commit()
    return {"symbol": symbol, "source": source, "rows": len(rows), "raw_path": str(path)}


def _failure_artifact_name(source: str, dataset: str, symbol: str) -> str:
    """Return a bounded filename even when a request contains many symbols."""
    safe_source = re.sub(r"[^A-Za-z0-9_.-]+", "_", source)[:32] or "source"
    safe_dataset = re.sub(r"[^A-Za-z0-9_.-]+", "_", dataset)[:32] or "dataset"
    symbol_digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:12]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    return f"{safe_source}_{safe_dataset}_{symbol_digest}_{stamp}.json"


def _mark_failure(source: str, dataset: str, symbol: str, started: str, exc: Exception) -> None:
    ensure_data_lake()
    dead = DATA_LAKE / "dead_letter" / _failure_artifact_name(source, dataset, symbol)
    dead.write_text(json.dumps({"source": source, "dataset": dataset, "symbol": symbol,
                                "captured_at": utc_now(), "error": repr(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
    with closing(connect()) as conn:
        source_id = conn.execute("SELECT id FROM data_sources WHERE code=?", (source,)).fetchone()[0]
        _record_run(conn, source_id, dataset, symbol, "FAILED", started, 0, str(dead), repr(exc))
        conn.execute("UPDATE data_sources SET health_status='DEGRADED',last_error_at=?,last_error=? WHERE id=?",
                     (utc_now(), repr(exc), source_id))
        conn.commit()


def _cached_names(symbols: list[str]) -> dict[str, str]:
    names = {}
    with closing(connect()) as conn:
        for symbol in symbols:
            row = conn.execute(
                "SELECT name FROM assets WHERE symbol=? UNION ALL "
                "SELECT asset_name FROM quote_snapshots WHERE asset_symbol=? ORDER BY 1 LIMIT 1",
                (symbol, symbol),
            ).fetchone()
            if row and row[0] and row[0] != symbol:
                names[symbol] = str(row[0])
    return names


def _refresh_tdx_market_data(symbols: list[str], include_minutes: bool,
                             include_daily: bool, include_history: bool) -> dict:
    result = {"provider": "tdx", "quotes": [], "minutes": [], "history": [],
              "history_gaps": [], "daily": [], "cached_symbols": [], "errors": []}
    received = set()
    started = utc_now()
    try:
        quotes, raw = fetch_tdx_quotes(symbols, _cached_names(symbols))
        result["quotes"].append(_write_quotes(quotes, raw, "tdx_public"))
        received.update(item["symbol"] for item in quotes)
    except Exception as exc:
        _mark_failure("tdx_public", "realtime_quotes", ",".join(symbols), started, exc)
        result["errors"].append({"source": "tdx_public", "dataset": "quotes", "error": repr(exc)})

    if include_minutes:
        for symbol in symbols:
            started = utc_now()
            try:
                rows, raw = fetch_tdx_recent_minutes(symbol, 5)
                result["minutes"].append(_write_minutes(symbol, rows, raw, "tdx_public"))
            except Exception as exc:
                _mark_failure("tdx_public", "minute_1", symbol, started, exc)
                result["errors"].append({"source": "tdx_public", "dataset": "minute_1",
                                         "symbol": symbol, "error": repr(exc)})

    if include_daily:
        for symbol in symbols:
            started = utc_now()
            try:
                rows, raw = fetch_tdx_daily(symbol)
                result["daily"].append(_write_daily(symbol, rows, raw, "tdx_public"))
            except Exception as exc:
                _mark_failure("tdx_public", "daily_qfq", symbol, started, exc)
                result["errors"].append({"source": "tdx_public", "dataset": "daily_qfq",
                                         "symbol": symbol, "error": repr(exc)})

    if include_history:
        required_start = (datetime.now(SHANGHAI) - timedelta(days=HISTORY_DAYS)).date().isoformat()
        for symbol in symbols:
            for interval in HISTORY_INTERVALS:
                started = utc_now()
                try:
                    rows, raw = fetch_tdx_history_minutes(symbol, interval)
                    written = _write_minutes(symbol, rows, raw, "tdx_public")
                    result["history"].append(written)
                    if written["first_bar"][:10] > required_start:
                        result["history_gaps"].append({
                            "symbol": symbol, "interval_minutes": interval,
                            "required_start": required_start, "actual_start": written["first_bar"][:10],
                            "status": "PARTIAL",
                        })
                except Exception as exc:
                    _mark_failure("tdx_public", f"minute_{interval}", symbol, started, exc)
                    result["errors"].append({"source": "tdx_public", "dataset": f"minute_{interval}",
                                             "symbol": symbol, "error": repr(exc)})
                    result["history_gaps"].append({
                        "symbol": symbol, "interval_minutes": interval,
                        "required_start": required_start, "actual_start": None, "status": "MISSING",
                    })

    with closing(connect()) as conn:
        available = {row[0] for row in conn.execute(
            f"SELECT DISTINCT asset_symbol FROM quote_snapshots WHERE asset_symbol IN ({','.join('?' for _ in symbols)})",
            symbols,
        )}
    result["cached_symbols"] = sorted(set(symbols).difference(received).intersection(available))
    completed = bool(result["quotes"] or result["minutes"] or result["daily"] or result["history"])
    result["status"] = "PARTIAL" if result["errors"] and completed else ("DEGRADED_CACHE" if result["errors"] else "REFRESHED")
    return result


def refresh_market_data(symbols: Iterable[str] = DEFAULT_SYMBOLS, include_minutes: bool = True,
                        include_daily: bool = False, include_history: bool = False) -> dict:
    initialize()
    ensure_data_lake()
    symbols = list(dict.fromkeys(normalize_symbol(s) for s in symbols))
    if market_provider_mode() == "tdx":
        return _refresh_tdx_market_data(symbols, include_minutes, include_daily, include_history)
    result = {"quotes": [], "minutes": [], "history": [], "history_gaps": [],
              "daily": [], "cached_symbols": [], "errors": []}

    started = utc_now()
    received = set()
    try:
        quotes, raw = fetch_tencent_quotes(symbols)
        result["quotes"].append(_write_quotes(quotes, raw, "tencent"))
        received.update(item["symbol"] for item in quotes)
    except Exception as exc:
        _mark_failure("tencent", "realtime_quotes", ",".join(symbols), started, exc)
        result["errors"].append({"source": "tencent", "dataset": "quotes", "error": repr(exc)})
    for symbol in set(symbols).difference(received):
        started = utc_now()
        try:
            quote, raw = fetch_eastmoney_quote(symbol)
            result["quotes"].append(_write_quotes([quote], raw, "eastmoney"))
            received.add(symbol)
        except Exception as exc:
            _mark_failure("eastmoney", "realtime_quotes", symbol, started, exc)
            result["errors"].append({"source": "eastmoney", "dataset": "quotes", "symbol": symbol, "error": repr(exc)})

    if include_minutes:
        for symbol in symbols:
            started = utc_now()
            try:
                rows, raw = fetch_tencent_minutes(symbol)
                result["minutes"].append(_write_minutes(symbol, rows, raw, "tencent"))
                continue
            except Exception as exc:
                _mark_failure("tencent", "minute_1", symbol, started, exc)
                result["errors"].append({"source": "tencent", "dataset": "minute_1", "symbol": symbol, "error": repr(exc)})
            started = utc_now()
            try:
                rows, raw = fetch_eastmoney_minutes(symbol)
                result["minutes"].append(_write_minutes(symbol, rows, raw, "eastmoney"))
            except Exception as exc:
                _mark_failure("eastmoney", "minute_1", symbol, started, exc)
                result["errors"].append({"source": "eastmoney", "dataset": "minute_1", "symbol": symbol, "error": repr(exc)})

    if include_daily:
        for symbol in symbols:
            started = utc_now()
            try:
                rows, raw = fetch_tencent_daily(symbol)
                result["daily"].append(_write_daily(symbol, rows, raw, "tencent"))
            except Exception as exc:
                _mark_failure("tencent", "daily_qfq", symbol, started, exc)
                result["errors"].append({"source": "tencent", "dataset": "daily_qfq", "symbol": symbol, "error": repr(exc)})

    if include_history:
        for symbol in symbols:
            for interval in HISTORY_INTERVALS:
                dataset = f"minute_{interval}"
                required_start = (datetime.now(SHANGHAI) - timedelta(days=HISTORY_DAYS)).date().isoformat()
                written_batches = []
                started = utc_now()
                eastmoney_complete = False
                try:
                    rows, raw = fetch_eastmoney_history_minutes(symbol, interval)
                    written = _write_minutes(symbol, rows, raw, "eastmoney")
                    result["history"].append(written)
                    written_batches.append(written)
                    eastmoney_complete = written["first_bar"][:10] <= required_start
                except Exception as exc:
                    _mark_failure("eastmoney", dataset, symbol, started, exc)
                    result["errors"].append({"source": "eastmoney", "dataset": dataset,
                                             "symbol": symbol, "error": repr(exc)})
                if eastmoney_complete:
                    continue
                started = utc_now()
                try:
                    rows, raw = fetch_tdx_history_minutes(symbol, interval)
                    written = _write_minutes(symbol, rows, raw, "tdx_public")
                    result["history"].append(written)
                    written_batches.append(written)
                except Exception as exc:
                    _mark_failure("tdx_public", dataset, symbol, started, exc)
                    result["errors"].append({"source": "tdx_public", "dataset": dataset,
                                             "symbol": symbol, "error": repr(exc)})
                complete = any(batch["first_bar"][:10] <= required_start for batch in written_batches)
                if not complete and interval in (5, 15, 30, 60):
                    started = utc_now()
                    try:
                        rows, raw = fetch_baostock_history_minutes(symbol, interval)
                        written = _write_minutes(symbol, rows, raw, "baostock")
                        result["history"].append(written)
                        written_batches.append(written)
                    except Exception as exc:
                        _mark_failure("baostock", dataset, symbol, started, exc)
                        result["errors"].append({"source": "baostock", "dataset": dataset,
                                                 "symbol": symbol, "error": repr(exc)})
                complete = any(batch["first_bar"][:10] <= required_start for batch in written_batches)
                if not complete:
                    starts = [batch["first_bar"][:10] for batch in written_batches]
                    result["history_gaps"].append({
                        "symbol": symbol, "interval_minutes": interval,
                        "required_start": required_start, "actual_start": min(starts) if starts else None,
                        "status": "PARTIAL" if starts else "MISSING",
                    })

    with closing(connect()) as conn:
        available = {row[0] for row in conn.execute(
            f"SELECT DISTINCT asset_symbol FROM quote_snapshots WHERE asset_symbol IN ({','.join('?' for _ in symbols)})",
            symbols,
        )}
    result["cached_symbols"] = sorted(set(symbols).difference(received).intersection(available))
    result["status"] = "SUCCESS" if len(received) == len(symbols) else ("DEGRADED_CACHE" if available else "FAILED")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh free A-share real-time and one-minute market data")
    parser.add_argument("--symbol", action="append", help="A-share/ETF code or index such as 000001.SH")
    parser.add_argument("--no-minute", action="store_true")
    parser.add_argument("--no-daily", action="store_true")
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--watch", action="store_true", help="keep refreshing; intended for a separate local process")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()
    symbols = args.symbol or list(DEFAULT_SYMBOLS)
    while True:
        result = refresh_market_data(symbols, include_minutes=not args.no_minute,
                                     include_daily=not args.no_daily,
                                     include_history=not args.no_history)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not args.watch:
            return 0 if result["status"] != "FAILED" else 2
        time.sleep(max(30, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
