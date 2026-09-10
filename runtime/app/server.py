"""Dependency-free local HTTP server for the analysis MVP."""

import hashlib
import hmac
import json
import ipaddress
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

from .analytics import calculate_risk_lines, evaluate_discipline, market_risk, policy_catalog
from .alerts import send_pending, smtp_status, confirm_received, retry_message
from .mail_settings import public_settings, save_settings, verify_connection
from .email_digest import build_digest, queue_digest, queue_due_digests
from .process_lock import ProcessLock
from .db import DATA_LAKE, ROOT, connect, initialize
from .data_sources.desktop import probe_desktop_sources
from .data_sources.market import DEFAULT_SYMBOLS, market_provider_mode, normalize_symbol, refresh_market_data
from .charting import SUPPORTED_PERIODS, add_ma5, add_technical_indicators, build_chart_series
from .search import exact_search, semantic_search, semantic_status, sync_semantic_index
from .research import run_backtest, run_factor, strategy_lab_payload
from .harness import (active_config, approve_candidate, evaluate_candidate, generate_candidate,
                      harness_payload, normalize_input, record_bad_case, rollback_version)
from .harness_autonomy import run_autonomous_cycle
from . import research_agent
from .service_monitor import ServiceMonitor
from .quote_sync import status_payload as quote_sync_status, write_status as write_quote_sync

_service_monitor = None
from .agent_harness import (cancel_run as cancel_harness_run,
                            create_run as create_harness_run,
                            get_run as get_harness_run,
                            recover_interrupted_runs as recover_interrupted_harness_runs,
                            resolve_approval as resolve_harness_approval,
                            resume_run as resume_harness_run,
                            runtime_payload as harness_runtime_payload)
from .stock_compare import compare_stocks, resolve_stock
from .strategy_evolution import strategy_evolution_payload
from .quant_portfolio import (quant_portfolio_payload, recover_interrupted_quant_runs,
                              recommendation_methodology_payload,
                              register_quant_mandate, resolve_quant_decision)
from .model_registry import (activate_model_definition, create_model_definition,
                             model_registry_payload)
from .portfolio import (clear_portfolio, confirm_portfolio_import, portfolio_metadata,
                        preview_portfolio_import)
from .signal_service import (create_subscription, notification_payload,
                             queue_test_email, set_subscription_enabled,
                             signal_feed, subscription_payload,
                             update_signal_status)
from .sector_cache import (recover_sector_cache_jobs, sector_cache_status,
                           trigger_active_sector_cache, trigger_sector_cache)
from .continuous_learning import (continuous_learning_payload, learning_universe,
                                  scheduled_cycle_request)
from .intraday_strategy import intraday_payload
from .deep_learning import deep_learning_payload
from .social_sentiment import sentiment_coverage_payload
from .code_evolution import (code_evolution_payload, rollback_active_version as rollback_code_version,
                             run_automatic_code_evolution)
from .data_sources.intelligence import (collect_bilibili, collect_sec_submissions,
                                        collect_x_recent, source_access_status)
from .data_sources.reports import (recover_interrupted_report_library_sync,
                                   refresh_report_library_if_due, refresh_stock_report,
                                   register_report_equities, report_library_payload,
                                   research_material_sync_status, trigger_report_refresh)
from .data_sources.report_bulk import (bulk_report_status, pause_bulk_report_sync,
                                       recover_interrupted_report_sync,
                                       trigger_bulk_report_sync,
                                       trigger_daily_increment_if_due)


def _runtime_revision() -> str:
    digest = hashlib.sha256()
    files = sorted(
        path for path in (ROOT / "app").rglob("*")
        if path.is_file() and path.suffix.lower() in {".py", ".js", ".css", ".html"}
    )
    for path in files:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _plugin_version() -> str:
    manifest = ROOT.parent / ".codex-plugin" / "plugin.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return "unknown"


STATIC_DIR = Path(__file__).resolve().parent / "static"
RUNTIME_REVISION = _runtime_revision()
PLUGIN_VERSION = _plugin_version()
MAX_CONCURRENT_COMPARISONS = max(1, min(int(os.environ.get("ARGUS_MAX_CONCURRENT_COMPARISONS", "8")), 32))
REMOTE_TOKEN_HEADER = "X-Argus-Token"
API_RATE_LIMIT_PER_MINUTE = max(
    30, min(int(os.environ.get("ARGUS_API_RATE_LIMIT_PER_MINUTE", "180")), 10_000)
)
_comparison_slots = threading.BoundedSemaphore(MAX_CONCURRENT_COMPARISONS)
_asset_refresh_lock = threading.Lock()
_asset_refresh_processes: dict[str, subprocess.Popen] = {}
_api_rate_lock = threading.Lock()
_api_rate_windows: dict[str, deque] = defaultdict(deque)


class ConcurrentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class DualStackHTTPServer(ConcurrentHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        if hasattr(socket, "IPV6_V6ONLY"):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def _server_class_for_host(host: str):
    try:
        socket.inet_pton(socket.AF_INET6, host)
    except OSError:
        return ConcurrentHTTPServer
    return DualStackHTTPServer


def _configured_remote_token() -> str | None:
    token_file = os.environ.get("ARGUS_REMOTE_TOKEN_FILE", "").strip()
    if not token_file:
        return None
    try:
        return Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _remote_auth_status(path: str, supplied_token: str | None) -> HTTPStatus | None:
    if not path.startswith("/api/"):
        return None
    expected_token = _configured_remote_token()
    if expected_token is None:
        return None
    if not expected_token:
        return HTTPStatus.SERVICE_UNAVAILABLE
    if not supplied_token:
        return HTTPStatus.UNAUTHORIZED
    if not hmac.compare_digest(supplied_token, expected_token):
        return HTTPStatus.FORBIDDEN
    return None


def _client_ip(client_address) -> str:
    value = str(client_address[0])
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return value
    return str(getattr(parsed, "ipv4_mapped", None) or parsed)


def _direct_local_browser(client_address, headers, port: int) -> bool:
    """Trust only direct same-origin browser requests made on this computer."""
    if headers.get("X-Argus-Local") != "1":
        return False
    try:
        if not ipaddress.ip_address(_client_ip(client_address)).is_loopback:
            return False
    except ValueError:
        return False
    # Forwarded requests must continue through remote token authentication.
    if any(name.lower() in {"forwarded", "x-real-ip", "via"}
           or name.lower().startswith("x-forwarded-") for name in headers):
        return False
    hosts = headers.get_all("Host", [])
    if len(hosts) != 1:
        return False
    host = hosts[0].lower()
    if host not in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}:
        return False
    origin = headers.get("Origin")
    if origin is not None and origin.lower() != f"http://{host}":
        return False
    return headers.get("Sec-Fetch-Site", "same-origin") == "same-origin"


def _allowed_csv(name: str) -> set[str]:
    return {item.strip().lower() for item in os.environ.get(name, "").split(",")
            if item.strip()}


def _request_host_allowed(host: str) -> bool:
    allowed = _allowed_csv("ARGUS_ALLOWED_HOSTS")
    if not allowed:
        return True
    raw = str(host or "").strip().lower()
    normalized = (raw[1:raw.index("]")] if raw.startswith("[") and "]" in raw
                  else raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw)
    return normalized in allowed


def _origin_allowed(origin: str, host: str) -> bool:
    if not origin:
        return True
    allowed = _allowed_csv("ARGUS_ALLOWED_ORIGINS")
    normalized = origin.rstrip("/").lower()
    if allowed:
        return normalized in allowed
    try:
        return urlparse(normalized).netloc == str(host or "").lower()
    except ValueError:
        return False


def _within_rate_limit(client_ip: str) -> bool:
    now = time.monotonic()
    with _api_rate_lock:
        window = _api_rate_windows[client_ip]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= API_RATE_LIMIT_PER_MINUTE:
            return False
        window.append(now)
        return True


def _audit_api(method: str, path: str, client_ip: str,
               auth_status: str, outcome: str) -> None:
    try:
        with closing(connect()) as conn:
            conn.execute(
                """INSERT INTO api_audit_log
                   (request_id,method,path,client_ip,auth_status,outcome,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, method, path, client_ip, auth_status, outcome,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except Exception:
        pass


def _rows(rows):
    return [dict(row) for row in rows]


def _source_state(source: dict, now: datetime | None = None) -> dict:
    item = dict(source)
    item["enabled"] = bool(item.get("enabled"))
    raw_status = str(item.get("health_status") or "UNKNOWN").upper()
    last_success = item.get("last_success_at")
    status_code = "UNVERIFIED"
    status_label = "尚未验证"
    status_tone = "muted"

    if not item["enabled"]:
        status_code, status_label = "NOT_CONNECTED", "未接入"
    elif raw_status in {"FAILED", "ERROR", "UNHEALTHY"}:
        status_code, status_label, status_tone = "UNAVAILABLE", "暂不可用", "bad"
    elif raw_status in {"DEGRADED", "LIMITED", "PARTIAL"}:
        status_code, status_label, status_tone = "LIMITED", "部分可用", "warn"
    elif raw_status == "HEALTHY":
        status_code, status_label, status_tone = "NORMAL", "正常", "good"
        if last_success:
            try:
                parsed = datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                checked_at = now or datetime.now(timezone.utc)
                if checked_at.tzinfo is None:
                    checked_at = checked_at.replace(tzinfo=timezone.utc)
                if (checked_at.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() > 72 * 3600:
                    status_code, status_label, status_tone = "STALE", "数据可能陈旧", "warn"
            except (TypeError, ValueError):
                status_code, status_label, status_tone = "UNVERIFIED", "时间待核验", "muted"

    item.update({
        "status_code": status_code,
        "status_label": status_label,
        "status_tone": status_tone,
        "connection_label": "已允许使用" if item["enabled"] else "未接入",
    })
    return item


def _source_summary(sources: list[dict], now: datetime | None = None) -> dict:
    items = [_source_state(source, now=now) for source in sources]
    by_code = {item["code"]: item for item in items}
    provider_mode = market_provider_mode()
    primary_codes = ["tdx_public"] if provider_mode == "tdx" else ["tencent", "eastmoney"]
    primary = next((by_code[code] for code in primary_codes if code in by_code), None)
    if primary is None:
        primary = next((item for item in items if item["enabled"] and item["code"] != "local_cache"), None)
    backup = by_code.get("local_cache")
    if backup:
        backup = dict(backup)
        backup.update({
            "status_code": "READY" if backup["enabled"] else "DISABLED",
            "status_label": "已启用" if backup["enabled"] else "未启用",
            "status_tone": "good" if backup["enabled"] else "muted",
            "connection_label": "自动兜底" if backup["enabled"] else "未启用",
        })
    excluded = {item["code"] for item in (primary, backup) if item}
    return {
        "provider_mode": provider_mode,
        "timezone": "Asia/Shanghai",
        "timezone_label": "北京时间",
        "primary": primary,
        "backup": backup,
        "alternatives": [item for item in items if item["code"] not in excluded],
    }


def comparison_payload(stock_inputs, profile, refresh=False):
    with _comparison_slots:
        return compare_stocks(stock_inputs, profile, refresh)


def comparison_analysis_assets(conn):
    assets = []
    watch_rows = conn.execute(
        """SELECT symbol,name,first_compared_at,last_compared_at,compare_count
           FROM comparison_watchlist ORDER BY last_compared_at DESC,symbol"""
    ).fetchall()
    for watch in watch_rows:
        symbol = watch["symbol"]
        quote = conn.execute(
            """SELECT q.*,ds.code AS source FROM quote_snapshots q
               JOIN data_sources ds ON ds.id=q.source_id
               WHERE q.asset_symbol=?
               ORDER BY julianday(q.captured_at) DESC,q.observed_at DESC,ds.priority LIMIT 1""",
            (symbol,),
        ).fetchone()
        daily = conn.execute(
            """SELECT b.close,b.trade_date,ds.code AS source
               FROM market_daily_bars b JOIN data_sources ds ON ds.id=b.source_id
               WHERE b.asset_symbol=? AND b.source_id=(
                 SELECT b2.source_id FROM market_daily_bars b2
                 JOIN data_sources ds2 ON ds2.id=b2.source_id
                 WHERE b2.asset_symbol=b.asset_symbol AND b2.trade_date=b.trade_date
                 ORDER BY ds2.priority LIMIT 1)
               ORDER BY b.trade_date DESC LIMIT 2""",
            (symbol,),
        ).fetchall()
        price = quote["price"] if quote else (daily[0]["close"] if daily else None)
        change_pct = quote["change_pct"] if quote else (
            (daily[0]["close"] / daily[1]["close"] - 1) * 100 if len(daily) > 1 and daily[1]["close"] else None
        )
        assets.append({
            "symbol": symbol,
            "name": watch["name"],
            "market": "CN",
            "asset_type": "ETF" if symbol.startswith(("1", "5")) else "EQUITY",
            "currency": "CNY",
            "price": price,
            "current_price": price,
            "change_pct": change_pct,
            "pnl_pct": change_pct or 0,
            "trade_date": quote["observed_at"] if quote else (daily[0]["trade_date"] if daily else None),
            "source": quote["source"] if quote else (daily[0]["source"] if daily else None),
            "first_compared_at": watch["first_compared_at"],
            "last_compared_at": watch["last_compared_at"],
            "compare_count": watch["compare_count"],
        })
    return assets


def dashboard_payload():
    with closing(connect()) as conn:
        latest_real = conn.execute("SELECT MAX(trade_date) FROM prices WHERE is_demo=0").fetchone()[0]
        live_market_rows = conn.execute(
            """
            SELECT q.asset_symbol AS symbol,q.asset_name AS name,'CN' AS market,
                   CASE WHEN q.asset_symbol LIKE '%.SH' OR q.asset_symbol LIKE '%.SZ' THEN 'INDEX'
                        WHEN q.asset_symbol LIKE '5%' OR q.asset_symbol LIKE '1%' THEN 'ETF' ELSE 'EQUITY' END AS asset_type,
                   'CNY' AS currency,q.price,q.observed_at AS trade_date,q.change_pct,
                   ds.code AS source,q.captured_at
            FROM quote_snapshots q JOIN data_sources ds ON ds.id=q.source_id
            WHERE q.rowid=(SELECT q2.rowid FROM quote_snapshots q2
                           JOIN data_sources ds2 ON ds2.id=q2.source_id
                           WHERE q2.asset_symbol=q.asset_symbol
                           ORDER BY julianday(q2.captured_at) DESC,q2.observed_at DESC,
                                    ds2.priority LIMIT 1)
            ORDER BY CASE q.asset_symbol WHEN '000001.SH' THEN 0 WHEN '512400' THEN 1 WHEN '562500' THEN 2 ELSE 3 END
            LIMIT 8
            """
        ).fetchall()
        fallback_market_rows = conn.execute(
            """
            SELECT a.symbol,a.name,a.market,a.asset_type,a.currency,a.data_status,
                   p.close AS price,p.trade_date,
                   (p.close / (SELECT p2.close FROM prices p2 WHERE p2.asset_id=a.id ORDER BY p2.trade_date DESC LIMIT 1 OFFSET 1)-1)*100 AS change_pct
            FROM assets a JOIN prices p ON p.asset_id=a.id
            WHERE p.trade_date=(SELECT MAX(p3.trade_date) FROM prices p3 WHERE p3.asset_id=a.id)
              AND a.symbol NOT IN ('512400','562500')
            ORDER BY CASE a.market WHEN 'CN' THEN 1 WHEN 'HK' THEN 2 WHEN 'US' THEN 3 WHEN 'JP' THEN 4 ELSE 5 END
            """
        ).fetchall()
        market_rows = live_market_rows or fallback_market_rows
        latest_quote_at = conn.execute(
            """SELECT observed_at FROM quote_snapshots
               ORDER BY julianday(captured_at) DESC,observed_at DESC LIMIT 1"""
        ).fetchone()
        latest_quote_at = latest_quote_at[0] if latest_quote_at else None
        latest_quote_capture_at = conn.execute(
            "SELECT MAX(captured_at) FROM quote_snapshots"
        ).fetchone()[0]
        tracked_symbols = conn.execute(
            "SELECT COUNT(*) FROM comparison_watchlist"
        ).fetchone()[0]
        portfolio_meta = portfolio_metadata(conn)
        positions = []
        for row in conn.execute(
            """
            SELECT p.id,a.symbol,a.name,a.market,a.asset_type,a.currency,p.quantity,
                   p.cost_price,p.current_price,p.highest_since_entry,
                   p.as_of,p.source_type,p.source_name,p.verification_status,
                   p.valuation_status,p.price_observed_at,p.price_source,p.tracking_started_at
            FROM positions p JOIN assets a ON a.id=p.asset_id WHERE p.verification_status='USER_CONFIRMED' ORDER BY p.id
            """
        ):
            item = dict(row)
            reliable = (
                item["verification_status"] == "USER_CONFIRMED"
                and item["valuation_status"] == "AVAILABLE"
                and float(item["current_price"]) > 0
            )
            item["has_reliable_price"] = reliable
            if reliable:
                item["market_value"] = round(item["quantity"] * item["current_price"], 2)
                item["pnl_pct"] = (item["current_price"] / item["cost_price"] - 1) * 100
                lines = calculate_risk_lines(
                    item["cost_price"], item["current_price"], item["highest_since_entry"],
                    item["market"], item["asset_type"],
                )
                item["lines"] = lines
                item["discipline"] = evaluate_discipline(item["current_price"], lines)
            else:
                item["market_value"] = None
                item["pnl_pct"] = None
                item["lines"] = None
                item["discipline"] = None
            positions.append(item)
        hypotheses = _rows(conn.execute("SELECT * FROM hypotheses WHERE status!='ARCHIVED' ORDER BY id").fetchall())
        evidence = _rows(
            conn.execute(
                """SELECT e.*,s.name AS source_name,s.url,s.reliability,s.verification_status
                   FROM evidence e LEFT JOIN sources s ON s.id=e.source_id ORDER BY e.id DESC LIMIT 8"""
            ).fetchall()
        )
        source_health = _rows(conn.execute(
            """SELECT code,name,source_kind,access_mode,priority,enabled,health_status,
                      last_success_at,last_error_at,last_error,homepage,license_note
               FROM data_sources ORDER BY priority"""
        ).fetchall())
        evidence_coverage = conn.execute(
            "SELECT AVG(evidence_coverage) FROM reports"
        ).fetchone()[0]
        analysis_assets = comparison_analysis_assets(conn)
        all_priced = bool(positions) and all(item["has_reliable_price"] for item in positions)
        priced_positions = sum(1 for item in positions if item["has_reliable_price"])
        price_times = [item["price_observed_at"] for item in positions
                       if item["has_reliable_price"] and item["price_observed_at"]]
        return {
            "meta": {
                "api_version": 2,
                "as_of": latest_quote_at or latest_real or "2026-08-11",
                "last_sync_at": latest_quote_capture_at,
                "quote_sync": quote_sync_status(_a_share_session_open(), os.environ.get("ARGUS_LIVE_REFRESH_ENABLED", "1") == "1"),
                "live_refresh_enabled": os.environ.get("ARGUS_LIVE_REFRESH_ENABLED", "1") == "1",
                "market_session_open": _a_share_session_open(),
                "tracked_symbols": int(tracked_symbols or 0),
                "refresh_policy": "运行期间每60秒核对通达信报价；休市价格可能不变，查看的分钟图自动补齐",
                "mode": "FREE_DELAYED_REALTIME" if latest_quote_at else ("LOCAL_REAL_WITH_DEMO_BENCHMARKS" if latest_real else "DEMO_OFFLINE"),
                "warning": "A股大盘、ETF和个股使用免费公开行情并保存到本机；请根据页面显示的数据时间判断是否已经更新。海外市场数据仍可能是演示数据。系统不能下单。",
                "fact_opinion_rule": "所有结论必须标记为事实、观点或待验证假设。",
                "evidence_coverage": (
                    None if evidence_coverage is None else round(float(evidence_coverage), 4)
                ),
            },
            "portfolio": {
                **portfolio_meta,
                "has_positions": bool(positions),
                "market_value": (
                    round(sum(p["market_value"] for p in positions), 2)
                    if all_priced else None
                ),
                "unrealized_pnl": (
                    round(sum(
                        (p["current_price"] - p["cost_price"]) * p["quantity"]
                        for p in positions
                    ), 2)
                    if all_priced else None
                ),
                "valuation_status": (
                    "EMPTY" if not positions else "COMPLETE" if all_priced
                    else "PARTIAL" if priced_positions else "UNAVAILABLE"
                ),
                "priced_positions": priced_positions,
                "total_positions": len(positions),
                "price_as_of": max(price_times) if price_times else None,
                "positions": positions,
            },
            "markets": _rows(market_rows),
            "analysis_assets": analysis_assets,
            "hypotheses": hypotheses,
            "evidence": evidence,
            "risk_policies": policy_catalog(),
            "strategy_lab": strategy_lab_payload(),
            "intelligence_sources": source_access_status(),
            "source_health": source_health,
            "source_summary": _source_summary(source_health),
            "email": smtp_status(),
        }


def chart_payload(symbol, period="1d"):
    with closing(connect()) as conn:
        asset = conn.execute("SELECT * FROM assets WHERE symbol=?", (symbol,)).fetchone()
        compared = conn.execute(
            "SELECT symbol,name FROM comparison_watchlist WHERE symbol=?", (symbol,)
        ).fetchone()
        quote = conn.execute(
            """SELECT q.*,ds.code AS source FROM quote_snapshots q JOIN data_sources ds ON ds.id=q.source_id
               WHERE q.asset_symbol=?
               ORDER BY julianday(q.captured_at) DESC,q.observed_at DESC,ds.priority LIMIT 1""",
            (symbol,),
        ).fetchone()
        if not asset and not quote and not compared:
            return None
        asset_dict = dict(asset) if asset else {
            "symbol": symbol, "name": quote["asset_name"] if quote else compared["name"],
            "market": "CN", "asset_type": "ETF" if symbol.startswith(("1", "5")) else "EQUITY",
            "currency": "CNY", "data_status": "REALTIME_ONLY",
        }
        position = conn.execute(
            """SELECT * FROM positions WHERE asset_id=?
               AND verification_status='USER_CONFIRMED'
               AND valuation_status='AVAILABLE' AND current_price>0""",
            (asset["id"],),
        ).fetchone() if asset else None
        lines = None
        if position:
            lines = calculate_risk_lines(position["cost_price"], position["current_price"], position["highest_since_entry"], asset_dict["market"], asset_dict["asset_type"])
        chart = build_chart_series(conn, symbol, period)
        # Keep the current daily candle live between slower historical refreshes.
        # Quote volume/amount are current-day cumulative values, so they replace
        # rather than add to the current daily candle.
        if quote and period == "1d" and chart["series"]:
            observed_date = str(quote["observed_at"])[:10]
            current = {"time": observed_date, "open": quote["open"] or quote["price"],
                       "high": quote["high"] or quote["price"], "low": quote["low"] or quote["price"],
                       "close": quote["price"], "volume": quote["volume"] or 0,
                       "amount": quote["amount"] or 0}
            if str(chart["series"][-1]["time"])[:10] == observed_date:
                chart["series"][-1].update(current)
            elif observed_date > str(chart["series"][-1]["time"])[:10]:
                chart["series"].append(current)
            add_ma5(chart["series"])
            add_technical_indicators(chart["series"], symbol)
            chart["coverage"]["view_last_bar"] = observed_date
        if quote and chart["series"] and quote["turnover_rate"] is not None:
            chart["series"][-1]["turnover_rate"] = quote["turnover_rate"]
        daily = build_chart_series(conn, symbol, "1d")["series"]
        risk = market_risk([p["close"] for p in daily]) if len(daily) >= 3 else {
            "annualized_volatility": 0.0,
            "max_drawdown": 0.0,
            "momentum_20d": 0.0,
            "mean_daily_return": 0.0,
        }
        return {"asset": asset_dict, "quote": dict(quote) if quote else None, "lines": lines,
                "risk": risk,
                "periods": [{"key": key, "label": label} for key, label in SUPPORTED_PERIODS.items()],
                **chart}


def _refresh_market_asset_in_background(symbol: str) -> bool:
    with _asset_refresh_lock:
        current = _asset_refresh_processes.get(symbol)
        if current and current.poll() is None:
            return False
        log_path = DATA_LAKE / "logs" / "asset_refresh.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("ab")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "app.data_sources.market", "--no-history",
                 "--no-daily", "--symbol", symbol],
                cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        finally:
            log.close()
        _asset_refresh_processes[symbol] = process
    return True


def _asset_live_refresh_due(symbol: str, max_age_seconds: int = 55) -> bool:
    """Return whether the selected asset needs a non-blocking intraday refresh."""
    symbol = normalize_symbol(symbol)
    try:
        with closing(connect()) as conn:
            captured_at = conn.execute(
                """SELECT MAX(captured_at) FROM minute_bars
                   WHERE asset_symbol=? AND interval_minutes=1""",
                (symbol,),
            ).fetchone()[0]
        if not captured_at:
            return True
        captured = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds()
        return age >= max_age_seconds
    except (TypeError, ValueError):
        return True


def resolve_market_asset(query: str) -> dict:
    """Resolve one A-share name/code and populate the local chart cache."""
    stock = resolve_stock(query)
    symbol = stock["symbol"]
    exchange = "SH" if symbol.startswith(("5", "6", "9")) else (
        "BJ" if symbol.startswith(("4", "8")) else "SZ"
    )
    asset_type = "ETF" if symbol.startswith(("1", "5")) else "EQUITY"
    with closing(connect()) as conn:
        conn.execute(
            """INSERT INTO assets(symbol,exchange_symbol,name,market,asset_type,currency,data_status)
               VALUES(?,?,?,?,?,'CNY','LOCAL_CACHE')
               ON CONFLICT(symbol) DO UPDATE SET name=excluded.name,
                 exchange_symbol=excluded.exchange_symbol""",
            (symbol, f"{symbol}.{exchange}", stock["name"], "CN", asset_type),
        )
        daily_rows = conn.execute(
            "SELECT COUNT(*) FROM market_daily_bars WHERE asset_symbol=?", (symbol,)
        ).fetchone()[0]
        conn.commit()
    if daily_rows >= 250:
        chart = chart_payload(symbol, "1d")
        started = _refresh_market_asset_in_background(symbol)
        refresh = {
            "provider": market_provider_mode(),
            "status": "BACKGROUND_REFRESH" if started else "BACKGROUND_REFRESH_RUNNING",
            "errors": [],
        }
    else:
        refresh = refresh_market_data(
            [symbol], include_minutes=True, include_daily=True, include_history=True
        )
        chart = chart_payload(symbol, "1d")
    return {
        "stock": stock,
        "cache": {
            "daily_rows_before": int(daily_rows),
            "history_requested": daily_rows < 250,
            "background_refresh": daily_rows >= 250,
            "provider": refresh.get("provider"),
            "status": refresh.get("status"),
            "errors": refresh.get("errors", []),
        },
        "chart": chart,
    }


def market_quotes_payload(symbols):
    normalized = [normalize_symbol(symbol) for symbol in symbols]
    if not normalized:
        return {"quotes": []}
    placeholders = ",".join("?" for _ in normalized)
    with closing(connect()) as conn:
        rows = conn.execute(
            f"""SELECT q.*,ds.code AS source FROM quote_snapshots q JOIN data_sources ds ON ds.id=q.source_id
                WHERE q.asset_symbol IN ({placeholders}) AND q.rowid=(
                  SELECT q2.rowid FROM quote_snapshots q2 JOIN data_sources ds2 ON ds2.id=q2.source_id
                  WHERE q2.asset_symbol=q.asset_symbol
                  ORDER BY julianday(q2.captured_at) DESC,q2.observed_at DESC,ds2.priority LIMIT 1)
                ORDER BY q.asset_symbol""", normalized,
        ).fetchall()
    return {"quotes": _rows(rows)}


def market_minutes_payload(symbol, limit=300):
    symbol = normalize_symbol(symbol)
    limit = max(1, min(int(limit), 2000))
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT * FROM (SELECT m.bar_time,m.interval_minutes,m.open,m.high,m.low,m.close,
                                      m.volume,m.amount,m.bar_kind,ds.code AS source,m.captured_at
                               FROM minute_bars m JOIN data_sources ds ON ds.id=m.source_id
                               WHERE m.asset_symbol=? AND m.interval_minutes=1 AND m.source_id=(
                                 SELECT source_id FROM minute_bars WHERE asset_symbol=? AND interval_minutes=1
                                 ORDER BY captured_at DESC LIMIT 1)
                               ORDER BY m.bar_time DESC LIMIT ?)
               ORDER BY bar_time""", (symbol, symbol, limit),
        ).fetchall()
    return {"symbol": symbol, "bars": _rows(rows)}


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _is_cn_trading_day(value) -> bool:
    try:
        with closing(connect()) as conn:
            row = conn.execute(
                "SELECT is_open FROM trading_calendar WHERE trade_date=? AND market='CN'",
                (value.isoformat(),),
            ).fetchone()
            coverage = conn.execute(
                "SELECT MIN(trade_date),MAX(trade_date) FROM trading_calendar WHERE market='CN'"
            ).fetchone()
        if row:
            return bool(row[0])
        if coverage and coverage[0] and str(coverage[0]) <= value.isoformat() <= str(coverage[1]):
            return False
    except Exception:
        pass
    return value.weekday() < 5


def _a_share_session_open(current: datetime | None = None) -> bool:
    current = current or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    else:
        current = current.astimezone(SHANGHAI)
    if not _is_cn_trading_day(current.date()):
        return False
    clock = current.hour * 60 + current.minute
    return 9 * 60 + 30 <= clock <= 11 * 60 + 30 or 13 * 60 <= clock <= 15 * 60


class MarketDataRefresher(threading.Thread):
    """Daemon refresher; HTTP handlers only read SQLite and remain responsive."""

    def __init__(self):
        super().__init__(name="argus-market-refresh", daemon=True)
        configured = os.environ.get("ARGUS_MARKET_SYMBOLS", ",".join(DEFAULT_SYMBOLS))
        self.symbols = [item.strip() for item in configured.split(",") if item.strip()]
        self.symbol_limit = max(4, min(200, int(
            os.environ.get("ARGUS_LIVE_SYMBOL_LIMIT", "100")
        )))
        self.poll_seconds = max(5, int(os.environ.get("ARGUS_MARKET_POLL_SECONDS", "10")))
        self.minute_seconds = max(60, int(os.environ.get("ARGUS_MINUTE_REFRESH_SECONDS", "60")))
        self.daily_seconds = max(3600, int(os.environ.get("ARGUS_DAILY_REFRESH_SECONDS", "21600")))
        self.history_seconds = max(3600, int(os.environ.get("ARGUS_HISTORY_REFRESH_SECONDS", "86400")))
        self.stop_event = threading.Event()
        self.refresh_process = None
        self.refresh_started = None
        self.daily_process = None
        self.history_process = None

    def _tracked_symbols(self) -> list[str]:
        candidates = list(self.symbols)
        try:
            with closing(connect()) as conn:
                rows = conn.execute(
                    """SELECT symbol FROM comparison_watchlist
                       ORDER BY last_compared_at DESC,symbol LIMIT ?""",
                    (self.symbol_limit,),
                ).fetchall()
                candidates.extend(str(row[0]) for row in rows)
                rows = conn.execute(
                    """SELECT a.symbol FROM positions p JOIN assets a ON a.id=p.asset_id
                       ORDER BY p.id DESC LIMIT ?""",
                    (self.symbol_limit,),
                ).fetchall()
                candidates.extend(str(row[0]) for row in rows)
                rows = conn.execute(
                    """SELECT DISTINCT c.symbol FROM quant_portfolio_versions v
                       JOIN quant_portfolio_candidates c ON c.run_id=v.run_id
                       WHERE v.status='ACTIVE'
                       ORDER BY c.liquidity_rank,c.symbol LIMIT ?""",
                    (self.symbol_limit,),
                ).fetchall()
                candidates.extend(str(row[0]) for row in rows)
        except Exception as exc:
            print(f"[market-refresh-symbols] {exc!r}")
        output = []
        for candidate in candidates:
            try:
                symbol = normalize_symbol(candidate)
            except ValueError:
                continue
            if symbol not in output:
                output.append(symbol)
            if len(output) >= self.symbol_limit:
                break
        return output

    def _launch_market_refresh(self, include_minutes: bool, include_daily: bool,
                               symbols: list[str] | None = None):
        """Keep native TDX work outside the Web interpreter and its GIL."""
        if self.refresh_process and self.refresh_process.poll() is None:
            if self.refresh_started is not None and time.monotonic() - self.refresh_started >= 50:
                self.refresh_process.kill()
                self.refresh_process.wait(timeout=3)
                write_quote_sync(status="FAILED", last_result_status="FAILED", finished_at=datetime.now(timezone.utc).isoformat(),
                                 errors=[{"error": "通达信行情请求超过50秒，下一分钟重试"}])
            else:
                return False
        log_path = DATA_LAKE / "logs" / "market_refresh.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        arguments = [sys.executable, "-m", "app.data_sources.market", "--no-history"]
        if not include_minutes:
            arguments.append("--no-minute")
        if not include_daily:
            arguments.append("--no-daily")
        if not include_minutes and not include_daily:
            arguments.append("--quote-cycle")
        for symbol in (symbols or self._tracked_symbols()):
            arguments.extend(("--symbol", symbol))
        log = log_path.open("ab")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            write_quote_sync(status="RUNNING", started_at=datetime.now(timezone.utc).isoformat(),
                             requested_count=len(symbols or self._tracked_symbols()))
            self.refresh_process = subprocess.Popen(
                arguments, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            self.refresh_started = time.monotonic()
        finally:
            log.close()
        return True

    def _launch_daily_refresh(self, symbols: list[str] | None = None):
        """Refresh daily bars independently so slow per-symbol work cannot block quotes."""
        if self.daily_process and self.daily_process.poll() is None:
            return False
        log_path = DATA_LAKE / "logs" / "daily_refresh.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        arguments = [sys.executable, "-m", "app.data_sources.market",
                     "--no-history", "--no-minute"]
        for symbol in (symbols or self._tracked_symbols()):
            arguments.extend(("--symbol", symbol))
        log = log_path.open("ab")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.daily_process = subprocess.Popen(
                arguments, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        finally:
            log.close()
        return True

    def _launch_history_refresh(self):
        """Run slow/native history clients outside the Web process and its GIL."""
        if self.history_process and self.history_process.poll() is None:
            return False
        log_path = DATA_LAKE / "logs" / "history_refresh.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("ab")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.history_process = subprocess.Popen(
                [sys.executable, "-m", "app.data_sources.market", "--no-minute", "--no-daily"],
                cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, creationflags=flags,
            )
        finally:
            log.close()
        return True

    def run(self):
        last_quote = None
        last_daily = 0.0
        last_history = 0.0
        while not self.stop_event.is_set():
            try:
                now = time.monotonic()
                # Keep checking the source every minute, including the final
                # close and corrections published after the trading session.
                include_quotes = last_quote is None or now - last_quote >= self.minute_seconds
                include_daily = now - last_daily >= self.daily_seconds
                include_history = now - last_history >= self.history_seconds
                symbols = self._tracked_symbols()
                if include_quotes and self._launch_market_refresh(False, False, symbols):
                    last_quote = now
                if include_daily and self._launch_daily_refresh(symbols):
                    last_daily = now
                if include_history and self._launch_history_refresh():
                    last_history = now
            except Exception as exc:
                print(f"[market-refresh] {exc!r}")
            self.stop_event.wait(self.poll_seconds)

    def stop(self):
        self.stop_event.set()
        if self.refresh_process and self.refresh_process.poll() is None:
            self.refresh_process.terminate()
        if self.daily_process and self.daily_process.poll() is None:
            self.daily_process.terminate()
        if self.history_process and self.history_process.poll() is None:
            self.history_process.terminate()


class ReportLibraryRefresher(threading.Thread):
    """Run the daily unified material sync, with hourly catch-up after failures."""

    def __init__(self):
        super().__init__(name="argus-report-refresh", daemon=True)
        self.poll_seconds = max(3600, int(os.environ.get("ARGUS_REPORT_POLL_SECONDS", "3600")))
        self.stop_event = threading.Event()

    def run(self):
        # Do not delay Web startup; the first poll begins in its own thread.
        while not self.stop_event.is_set():
            try:
                result = refresh_report_library_if_due()
                print(f"[report-refresh] {result['status']} documents={result['documents']} errors={len(result['errors'])}")
            except Exception as exc:
                print(f"[report-refresh] {exc!r}")
            self.stop_event.wait(self.poll_seconds)

    def stop(self):
        self.stop_event.set()


class DailyReportIncrementRefresher(threading.Thread):
    """Check hourly whether the completed full archive needs today's increment."""

    def __init__(self):
        super().__init__(name="argus-report-daily-increment", daemon=True)
        self.poll_seconds = max(3600, int(os.environ.get("ARGUS_REPORT_DAILY_POLL_SECONDS", "3600")))
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                trigger_daily_increment_if_due()
            except Exception as exc:
                print(f"[report-daily-increment] {exc!r}")
            self.stop_event.wait(self.poll_seconds)

    def stop(self):
        self.stop_event.set()


class AutonomousHarnessRefresher(threading.Thread):
    """Run deterministic bad-case discovery after startup and then once per day."""

    def __init__(self):
        super().__init__(name="argus-harness-autonomy", daemon=True)
        self.poll_seconds = max(3600, int(os.environ.get("ARGUS_HARNESS_POLL_SECONDS", "86400")))
        self.startup_delay = max(5, int(os.environ.get("ARGUS_HARNESS_STARTUP_DELAY_SECONDS", "20")))
        self.auto_apply = False
        self.stock_limit = max(1, min(int(os.environ.get("ARGUS_HARNESS_STOCK_LIMIT", "20")), 100))
        self.stop_event = threading.Event()

    def run(self):
        if self.stop_event.wait(self.startup_delay):
            return
        while not self.stop_event.is_set():
            try:
                result = run_autonomous_cycle(self.auto_apply, self.stock_limit)
                print(f"[harness-autonomy] {result['summary']}")
            except Exception as exc:
                print(f"[harness-autonomy] {exc!r}")
            self.stop_event.wait(self.poll_seconds)

    def stop(self):
        self.stop_event.set()


class ContinuousLearningRefresher(threading.Thread):
    """Schedule idempotent A-share learning cycles through the durable Harness."""

    def __init__(self):
        super().__init__(name="argus-continuous-learning", daemon=True)
        self.poll_seconds = max(30, int(os.environ.get("ARGUS_CONTINUOUS_LEARNING_POLL_SECONDS", "60")))
        self.startup_delay = max(5, int(os.environ.get("ARGUS_CONTINUOUS_LEARNING_STARTUP_DELAY_SECONDS", "30")))
        self.strategy_retry_poll_seconds = max(
            300, int(os.environ.get("ARGUS_STRATEGY_RETRY_POLL_SECONDS", "1800"))
        )
        self.last_strategy_retry_check = 0.0
        self.stop_event = threading.Event()

    def run(self):
        if self.stop_event.wait(self.startup_delay):
            return
        while not self.stop_event.is_set():
            try:
                with closing(connect()) as conn:
                    active = conn.execute(
                        """SELECT 1 FROM harness_runs WHERE workflow='continuous_learning'
                           AND status IN ('QUEUED','RUNNING','WAITING_APPROVAL') LIMIT 1"""
                    ).fetchone()
                request = None if active else scheduled_cycle_request()
                if request:
                    run = create_harness_run(
                        "continuous_learning", request,
                        intent="每日 A 股行情、舆情、预测评分和研究模型滚动更新",
                        requested_by="continuous_learning_scheduler",
                    )
                    print(f"[continuous-learning] scheduled {run['run']['run_key']} {request['phase']}")
                elif (not active and
                      time.monotonic() - self.last_strategy_retry_check >=
                      self.strategy_retry_poll_seconds):
                    self.last_strategy_retry_check = time.monotonic()
                    from .strategy_evolution import retry_pending_strategy_evolutions
                    retries = retry_pending_strategy_evolutions(max_jobs=1)
                    if retries["adopted"] or retries["results"] or retries["errors"]:
                        print(
                            "[strategy-evolution] "
                            f"adopted={retries['adopted']} checked={retries['checked']} "
                            f"errors={len(retries['errors'])}"
                        )
            except Exception as exc:
                print(f"[continuous-learning] {exc!r}")
            self.stop_event.wait(self.poll_seconds)

    def stop(self):
        self.stop_event.set()


class AlertOutboxRefresher(threading.Thread):
    def __init__(self):
        super().__init__(name="argus-alert-outbox", daemon=True)
        self.stop_event = threading.Event()

    def run_once(self):
        return send_pending(limit=100)

    def run(self):
        while not self.stop_event.is_set():
            try:
                queue_due_digests()
                self.run_once()
            except Exception as exc:
                print(f"[alert-outbox] {exc!r}")
            self.stop_event.wait(30)

    def stop(self):
        self.stop_event.set()


class SectorCacheRefresher(threading.Thread):
    """Keep complete daily/fundamental caches for active research sectors."""

    def __init__(self):
        super().__init__(name="argus-sector-cache-scheduler", daemon=True)
        self.poll_seconds = max(
            1800, int(os.environ.get("ARGUS_SECTOR_CACHE_POLL_SECONDS", "3600"))
        )
        self.startup_delay = max(
            2, int(os.environ.get("ARGUS_SECTOR_CACHE_STARTUP_DELAY_SECONDS", "5"))
        )
        self.stop_event = threading.Event()

    def run(self):
        if self.stop_event.wait(self.startup_delay):
            return
        while not self.stop_event.is_set():
            try:
                result = trigger_active_sector_cache()
                print(
                    f"[sector-cache] {result['status']} "
                    f"market={result.get('market_cached', 0)}/{result.get('total_symbols', 0)} "
                    f"model={result.get('model_ready', 0)}"
                )
            except Exception as exc:
                print(f"[sector-cache] {exc!r}")
            self.stop_event.wait(self.poll_seconds)

    def stop(self):
        self.stop_event.set()


class Handler(BaseHTTPRequestHandler):
    server_version = "MarketIntelMVP/0.1"

    def log_message(self, fmt, *args):
        print("[%s client=%s] %s" % (
            self.log_date_time_string(), _client_ip(self.client_address), fmt % args,
        ), flush=True)

    def _json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'",
        )

    def _file(self, path):
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(STATIC_DIR.resolve())
        except (FileNotFoundError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = resolved.read_bytes()
        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _authorize_api(self, path: str, mutation: bool = False) -> bool:
        client_ip = _client_ip(self.client_address)
        if path.startswith("/api/") and not _within_rate_limit(client_ip):
            _audit_api(self.command, path, client_ip, "RATE_LIMITED", "REJECTED")
            self._json({"error": "rate limit exceeded"}, HTTPStatus.TOO_MANY_REQUESTS)
            return False
        if not _request_host_allowed(self.headers.get("Host", "")):
            _audit_api(self.command, path, client_ip, "HOST_REJECTED", "REJECTED")
            self._json({"error": "host not allowed"}, HTTPStatus.FORBIDDEN)
            return False
        if mutation and not _origin_allowed(
            self.headers.get("Origin", ""), self.headers.get("Host", "")
        ):
            _audit_api(self.command, path, client_ip, "ORIGIN_REJECTED", "REJECTED")
            self._json({"error": "origin not allowed"}, HTTPStatus.FORBIDDEN)
            return False
        local_browser = _direct_local_browser(
            self.client_address, self.headers, self.server.server_port
        )
        status = (None if local_browser else
                  _remote_auth_status(path, self.headers.get(REMOTE_TOKEN_HEADER)))
        if status is None:
            if mutation:
                _audit_api(self.command, path, client_ip, "AUTHORIZED", "ACCEPTED")
            return True
        messages = {
            HTTPStatus.UNAUTHORIZED: "authentication required",
            HTTPStatus.FORBIDDEN: "authentication failed",
            HTTPStatus.SERVICE_UNAVAILABLE: "authentication unavailable",
        }
        _audit_api(self.command, path, client_ip, status.name, "REJECTED")
        self._json({"error": messages[status]}, status)
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        if not self._authorize_api(parsed.path):
            return
        if parsed.path == "/api/services":
            learning = continuous_learning_payload()
            code = code_evolution_payload()
            deep = deep_learning_payload()
            cycle = learning.get("last_cycle") or {}
            evaluation = code.get("last_evaluation") or {}
            self._json({"runtime": _service_monitor.payload() if _service_monitor else {"all_enabled_alive": False, "services": []},
                        "agent": research_agent.runtime_status(),
                        "learning": {"status": cycle.get("status"), "phase": cycle.get("phase"),
                                     "data_asof": cycle.get("data_asof"), "errors": cycle.get("errors"),
                                     "progress": cycle.get("progress"), "predictions": learning.get("predictions"),
                                     "model_version": (learning.get("active_model") or {}).get("version_key")},
                        "evolution": {"automatic": code.get("automatic_activation"),
                                      "active_version": (code.get("active_version") or {}).get("version_key"),
                                      "last_status": evaluation.get("status"), "gate": evaluation.get("gate"),
                                      "rollback_available": code.get("rollback_available")},
                        "deep_learning": {"base_model_ready": deep.get("base_model_ready"),
                                          "active_version": (deep.get("active_model") or {}).get("version_key")}})
            return
        if parsed.path == "/api/research-agent":
            self._json({"runtime": research_agent.runtime_status(), "runs": research_agent.list_runs()})
            return
        research_match = re.fullmatch(r"/api/research-agent/runs/(research_[a-f0-9]{32})", parsed.path)
        if research_match:
            try:
                self._json(research_agent.get_run(research_match.group(1)))
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/health":
            virtual_environment = os.environ.get("CONDA_PREFIX") or os.environ.get("VIRTUAL_ENV")
            if not virtual_environment:
                executable_root = Path(sys.executable).resolve().parent
                if (executable_root / "conda-meta").is_dir():
                    virtual_environment = str(executable_root)
                elif sys.prefix != sys.base_prefix:
                    virtual_environment = sys.prefix
            with closing(connect()) as conn:
                sources = _rows(conn.execute("SELECT code,enabled,health_status,last_success_at,last_error FROM data_sources ORDER BY priority").fetchall())
                persistence = dict(conn.execute(
                    """SELECT
                         (SELECT COUNT(*) FROM ingestion_runs) AS ingestion_runs,
                         (SELECT COUNT(*) FROM quote_snapshots) AS quote_snapshots,
                         (SELECT COUNT(*) FROM minute_bars) AS minute_bars,
                         (SELECT COUNT(*) FROM market_daily_bars) AS daily_bars,
                         (SELECT COUNT(*) FROM source_documents) AS source_documents,
                         (SELECT COUNT(*) FROM source_document_versions) AS document_versions"""
                ).fetchone())
            self._json({"status": "ok", "api_version": 2, "database": "sqlite",
                        "background_services": _service_monitor.payload() if _service_monitor else None,
                        "runtime": {"python_executable": sys.executable,
                                    "python_version": sys.version.split()[0],
                                    "is_virtual_environment": virtual_environment is not None,
                                    "virtual_environment": virtual_environment,
                                    "runtime_root": str(ROOT.resolve()),
                                    "runtime_identity": f"{ROOT.stat().st_dev}:{ROOT.stat().st_ino}",
                                    "runtime_revision": RUNTIME_REVISION,
                                    "plugin_version": PLUGIN_VERSION,
                                    "process_id": os.getpid(),
                                    "data_lake_singleton": True,
                                    "data_lake": str(DATA_LAKE.resolve())},
                        "network": {"bind_address": os.environ.get("ARGUS_BIND_ADDRESS", "127.0.0.1"),
                                    "public_host": os.environ.get("ARGUS_PUBLIC_HOST", "127.0.0.1")},
                        "market_data": {"provider": market_provider_mode(),
                                        "tdx_home": os.environ.get("ARGUS_TDX_HOME"),
                                        "storage": "local_sqlite"},
                        "concurrency": {"shared_service": True,
                                        "server": "thread-per-request",
                                        "request_queue_size": ConcurrentHTTPServer.request_queue_size,
                                        "max_concurrent_comparisons": MAX_CONCURRENT_COMPARISONS,
                                        "refresh_coalescing": True},
                        "order_execution": False, "sources": sources,
                        "persistence": persistence,
                        "desktop_backups": probe_desktop_sources(),
                        "email": smtp_status()})
            return
        if parsed.path == "/api/search/status":
            payload = semantic_status()
            payload["daily_sync"] = research_material_sync_status()
            self._json(payload)
            return
        if parsed.path == "/api/models":
            self._json(model_registry_payload())
            return
        if parsed.path == "/api/signals":
            query = parse_qs(parsed.query)
            self._json(signal_feed(query.get("mandate_key", [None])[0],
                                   int(query.get("limit", ["100"])[0])))
            return
        if parsed.path == "/api/notifications":
            self._json(notification_payload())
            return
        if parsed.path == '/api/email-settings':
            self._json(public_settings())
            return
        if parsed.path == "/api/notification-subscriptions":
            self._json(subscription_payload())
            return
        if parsed.path == "/api/harness":
            payload = harness_payload()
            payload["agent_runtime"] = harness_runtime_payload()
            payload["strategy_evolution"] = strategy_evolution_payload()
            payload["quant_portfolio"] = quant_portfolio_payload()
            payload["continuous_learning"] = continuous_learning_payload()
            payload["intraday_evolution"] = intraday_payload()
            payload["deep_learning"] = deep_learning_payload()
            payload["sentiment_coverage"] = sentiment_coverage_payload()
            payload["code_evolution"] = code_evolution_payload()
            payload["sector_cache"] = sector_cache_status()
            self._json(payload)
            return
        harness_run_match = re.fullmatch(r"/api/harness/runs/([^/]+)", parsed.path)
        if harness_run_match:
            after = parse_qs(parsed.query).get("after_event_id", ["0"])[0]
            self._json(get_harness_run(unquote(harness_run_match.group(1)), int(after)))
            return
        if parsed.path == "/api/strategy-lab":
            self._json(strategy_lab_payload())
            return
        if parsed.path == "/api/quant/methodology":
            self._json(recommendation_methodology_payload())
            return
        if parsed.path == "/api/intelligence-sources":
            self._json({"sources": source_access_status(), "credentials_in_database": False})
            return
        if parsed.path == "/api/reports/library":
            self._json(report_library_payload())
            return
        if parsed.path == "/api/reports/sync/status":
            self._json(bulk_report_status())
            return
        if parsed.path == "/api/search":
            query = parse_qs(parsed.query)
            text_query = query.get("q", [""])[0]
            mode = query.get("mode", ["exact"])[0]
            page = query.get("page", [None])[0]
            harness_config = active_config()
            normalized_input = normalize_input(text_query, harness_config)
            normalized_query = str(harness_config.get("search_aliases", {}).get(normalized_input, normalized_input)).strip()
            sync_status = research_material_sync_status()
            if re.fullmatch(r"\d{6}(?:\.(?:SH|SZ))?", normalized_query, re.IGNORECASE):
                results = exact_search(normalized_query, None)
                self._json({"query": text_query, "mode": "exact", "requested_mode": mode,
                            "page": page, "results": results, "degraded": False,
                            "warning": "股票代码已自动跨页面精确查询股票档案和研报库。",
                            "semantic": semantic_status(), "daily_sync": sync_status})
                return
            try:
                results = semantic_search(normalized_query, page) if mode == "semantic" else exact_search(normalized_query, page)
                if not results and normalized_query:
                    record_bad_case("search_no_result", {"query": text_query, "normalized_query": normalized_query,
                                                         "page": page, "mode": mode},
                                    observed={"result_count": 0}, source="automatic", page=page)
                self._json({"query": text_query, "mode": mode, "page": page, "results": results,
                            "semantic": semantic_status(), "daily_sync": sync_status})
            except (RuntimeError, ImportError, ModuleNotFoundError) as exc:
                if mode == "semantic":
                    results = exact_search(normalized_query, page)
                    record_bad_case("semantic_degraded", {"query": text_query, "page": page},
                                    observed={"error": str(exc)}, source="automatic", page=page)
                    self._json({"query": text_query, "mode": "exact", "requested_mode": mode,
                                "page": page, "results": results, "degraded": True,
                                "warning": "语义模型当前不可用，已自动改用严格匹配。",
                                "semantic_error": str(exc), "semantic": semantic_status(),
                                "daily_sync": sync_status})
                else:
                    self._json({"error": str(exc), "semantic": semantic_status()}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if parsed.path == "/api/market-sync":
            session_open = _a_share_session_open()
            enabled = os.environ.get("ARGUS_LIVE_REFRESH_ENABLED", "1") == "1"
            self._json({"market_session_open": session_open,
                        "live_refresh_enabled": enabled,
                        "quote_sync": quote_sync_status(session_open, enabled)})
            return
        if parsed.path == "/api/dashboard":
            self._json(dashboard_payload())
            return
        if parsed.path == "/api/stock-comparison":
            query = parse_qs(parsed.query)
            try:
                self._json(comparison_payload(
                    query.get("stocks", [""])[0],
                    query.get("profile", ["balanced"])[0],
                    query.get("refresh", ["0"])[0] == "1",
                ))
            except ValueError as exc:
                record_bad_case("comparison_error", {"stocks": query.get("stocks", [""])[0],
                                                       "profile": query.get("profile", ["balanced"])[0]},
                                observed={"error": str(exc)}, source="automatic", severity="HIGH")
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except RuntimeError as exc:
                record_bad_case("comparison_error", {"stocks": query.get("stocks", [""])[0],
                                                       "profile": query.get("profile", ["balanced"])[0]},
                                observed={"error": str(exc)}, source="automatic", severity="HIGH")
                self._json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if parsed.path == "/api/market/quotes":
            symbols = parse_qs(parsed.query).get("symbols", [","])[0].split(",")
            try:
                self._json(market_quotes_payload([item for item in symbols if item]))
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        minute_match = re.fullmatch(r"/api/market/minutes/([^/]+)", parsed.path)
        if minute_match:
            try:
                limit = parse_qs(parsed.query).get("limit", ["300"])[0]
                self._json(market_minutes_payload(unquote(minute_match.group(1)), limit))
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        match = re.fullmatch(r"/api/assets/([^/]+)/chart", parsed.path)
        if match:
            try:
                period = parse_qs(parsed.query).get("period", ["1d"])[0]
                symbol = unquote(match.group(1))
                if (os.environ.get("ARGUS_LIVE_REFRESH_ENABLED", "1") == "1"
                        and _asset_live_refresh_due(symbol)):
                    _refresh_market_asset_in_background(normalize_symbol(symbol))
                payload = chart_payload(symbol, period)
                self._json(payload, HTTPStatus.OK if payload else HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/" or parsed.path == "/index.html":
            self._file(STATIC_DIR / "index.html")
            return
        self._file(STATIC_DIR / parsed.path.lstrip("/"))

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorize_api(path, mutation=True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 5_000_000:
                raise ValueError("invalid body size")
            payload = json.loads(self.rfile.read(length)) if length else {}
            if path == "/api/research-agent/runs":
                self._json(research_agent.submit(payload.get("question"), payload.get("parent_key")), HTTPStatus.ACCEPTED)
                return
            research_cancel = re.fullmatch(r"/api/research-agent/runs/(research_[a-f0-9]{32})/cancel", path)
            if research_cancel:
                self._json(research_agent.cancel_run(research_cancel.group(1)))
                return
            if path == "/api/research-agent/evolve":
                self._json(create_harness_run("continuous_learning", {
                    "phase": "POST_CLOSE", "stock_limit": 20, "auto_promote": True,
                    "auto_promote_code": True, "refresh_data": True, "collect_sentiment": True,
                    "evolve_intraday": True, "evolve_source_code": True, "train_deep_model": True,
                    "retry_if_stale": True,
                }, intent="投资人请求运行数据更新、预测评分和策略自进化闭环", requested_by="research_agent"), HTTPStatus.ACCEPTED)
                return
            if path == "/api/models":
                actor = str(payload.get("created_by") or "").strip()
                self._json(create_model_definition(payload, actor), HTTPStatus.CREATED)
                return
            model_activation = re.fullmatch(r"/api/models/(\d+)/activate", path)
            if model_activation:
                self._json(activate_model_definition(
                    int(model_activation.group(1)), str(payload.get("scope_type", "DEFAULT")),
                    str(payload.get("scope_value", "*")), payload.get("profile"),
                    str(payload.get("approved_by", "")), bool(payload.get("confirmed", False)),
                ))
                return
            signal_action = re.fullmatch(r"/api/signals/(\d+)/(acknowledge|dismiss)", path)
            if signal_action:
                status = ("ACKNOWLEDGED" if signal_action.group(2) == "acknowledge"
                          else "DISMISSED")
                self._json(update_signal_status(int(signal_action.group(1)), status))
                return
            if path == '/api/email-settings':
                self._json(save_settings(payload))
                return
            if path == '/api/email-settings/verify':
                self._json(verify_connection())
                return
            if path == '/api/digests/preview':
                self._json(build_digest(payload))
                return
            if path == '/api/digests/send':
                if not smtp_status().get('configured') or not smtp_status().get('send_enabled'):
                    raise ValueError('请先配置发件邮箱并打开邮件发送；可以先预览日报')
                result = queue_digest(int(payload['subscription_id']), payload.get('report_date'))
                result['delivery'] = send_pending()
                self._json(result)
                return
            received_match = re.fullmatch(r'/api/notifications/(\d+)/received', path)
            if received_match:
                self._json(confirm_received(int(received_match.group(1))))
                return
            retry_match = re.fullmatch(r'/api/notifications/(\d+)/retry', path)
            if retry_match:
                self._json(retry_message(int(retry_match.group(1))))
                return
            if path == "/api/notification-subscriptions":
                self._json(create_subscription(payload), HTTPStatus.CREATED)
                return
            subscription_action = re.fullmatch(
                r"/api/notification-subscriptions/(\d+)/(enable|disable)", path
            )
            if subscription_action:
                self._json(set_subscription_enabled(
                    int(subscription_action.group(1)), subscription_action.group(2) == "enable"
                ))
                return
            if path == "/api/notifications/test":
                subscription_id = payload.get("subscription_id")
                self._json(queue_test_email(
                    int(subscription_id) if subscription_id is not None else None
                ))
                return
            if path == "/api/notifications/send":
                self._json(send_pending(limit=max(1, min(int(payload.get("limit", 50)), 200))))
                return
            if path == "/api/service/shutdown":
                if (not ipaddress.ip_address(_client_ip(self.client_address)).is_loopback
                        or payload.get("confirmed") is not True):
                    self._json({"error": "local confirmation required"}, HTTPStatus.FORBIDDEN)
                    return
                self._json({"status": "STOPPING"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/search/reindex":
                count = sync_semantic_index()
                self._json({"indexed": count, "semantic": semantic_status()})
                return
            if path == "/api/portfolio/imports/preview":
                self._json(preview_portfolio_import(payload), HTTPStatus.CREATED)
                return
            if path == "/api/portfolio/imports/confirm":
                self._json(confirm_portfolio_import(payload))
                return
            if path == "/api/portfolio/clear":
                self._json(clear_portfolio(payload))
                return
            if path == "/api/assets/resolve":
                self._json(resolve_market_asset(str(payload.get("query", ""))))
                return
            if path == "/api/quant/decision":
                self._json(resolve_quant_decision(payload.get("input", payload)))
                return
            if path == "/api/quant/mandates":
                result = register_quant_mandate(payload.get("input", payload))
                try:
                    sectors = result.get("decision", {}).get("mandate", {}).get(
                        "input", {}
                    ).get("sectors", [])
                    result["sector_cache"] = trigger_sector_cache(sectors)
                except Exception as exc:
                    result["sector_cache"] = {"status": "FAILED", "error": str(exc)}
                self._json(result, HTTPStatus.CREATED)
                return
            if path == "/api/quant/cache":
                self._json(trigger_sector_cache(
                    payload.get("sectors", []), bool(payload.get("force", False))
                ), HTTPStatus.ACCEPTED)
                return
            if path == "/api/harness/bad-cases":
                self._json(record_bad_case(
                    str(payload.get("case_type", "")), payload.get("input", {}),
                    payload.get("expected", {}), payload.get("observed", {}),
                    str(payload.get("source", "manual")), payload.get("page"),
                    str(payload.get("severity", "MEDIUM")), str(payload.get("notes", "")),
                ), HTTPStatus.CREATED)
                return
            if path == "/api/harness/runs":
                self._json(create_harness_run(
                    str(payload.get("workflow", "")), payload.get("input", {}),
                    str(payload.get("intent", "")), payload.get("thread_key"),
                    str(payload.get("requested_by") or _client_ip(self.client_address)), True,
                ), HTTPStatus.CREATED)
                return
            harness_run_action = re.fullmatch(r"/api/harness/runs/([^/]+)/(resume|cancel)", path)
            if harness_run_action:
                run_key = unquote(harness_run_action.group(1))
                actor = str(payload.get("requested_by") or _client_ip(self.client_address))
                result = (resume_harness_run(run_key, actor, True)
                          if harness_run_action.group(2) == "resume"
                          else cancel_harness_run(run_key, actor))
                self._json(result)
                return
            harness_approval_match = re.fullmatch(r"/api/harness/approvals/([^/]+)/resolve", path)
            if harness_approval_match:
                self._json(resolve_harness_approval(
                    unquote(harness_approval_match.group(1)), bool(payload.get("approved", False)),
                    str(payload.get("resolved_by", "")), bool(payload.get("confirmed", False)), True,
                ))
                return
            if path == "/api/harness/candidates/generate":
                self._json(generate_candidate(int(payload.get("bad_case_id", 0))), HTTPStatus.CREATED)
                return
            if path == "/api/harness/evaluate":
                candidate_id = payload.get("candidate_id")
                self._json(evaluate_candidate(int(candidate_id) if candidate_id is not None else None))
                return
            if path == "/api/harness/autonomous/run":
                self._json(run_autonomous_cycle(
                    auto_apply=False,
                    stock_limit=max(1, min(int(payload.get("stock_limit", 20)), 100)),
                ))
                return
            if path == "/api/harness/candidates/approve":
                self._json(approve_candidate(int(payload.get("candidate_id", 0)),
                                             str(payload.get("approved_by", "")),
                                             bool(payload.get("confirmed", False))))
                return
            if path == "/api/harness/versions/rollback":
                self._json(rollback_version(str(payload.get("version_key", "")),
                                            str(payload.get("approved_by", "")),
                                            bool(payload.get("confirmed", False))))
                return
            if path == "/api/harness/code-evolution/run":
                symbols = payload.get("symbols") or learning_universe(
                    max(3, min(int(payload.get("stock_limit", 20)), 100)))
                self._json(run_automatic_code_evolution(
                    symbols, max(0.01, min(float(payload.get("max_drawdown", 0.15)), 0.80)),
                    bool(payload.get("auto_promote", True)),
                ))
                return
            if path == "/api/harness/code-evolution/rollback":
                if not payload.get("confirmed") or not str(payload.get("approved_by", "")).strip():
                    raise ValueError("源码回滚必须明确确认并填写操作人")
                self._json(rollback_code_version(
                    f"{payload.get('reason', 'manual rollback')} by {payload['approved_by']}"))
                return
            if path == "/api/research/backtest":
                self._json(run_backtest(str(payload.get("strategy_key", "")), str(payload.get("symbol", "512400"))))
                return
            if path == "/api/research/factors/run":
                self._json(run_factor(str(payload.get("factor_key", ""))))
                return
            if path == "/api/stock-comparison":
                try:
                    self._json(comparison_payload(
                        payload.get("stocks", []), str(payload.get("profile", "balanced")),
                        bool(payload.get("refresh", False)),
                    ))
                except ValueError as exc:
                    record_bad_case("comparison_error", {
                        "stocks": payload.get("stocks", []),
                        "profile": str(payload.get("profile", "balanced")),
                    }, observed={"error": str(exc)}, source="automatic", severity="HIGH")
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                except RuntimeError as exc:
                    record_bad_case("comparison_error", {
                        "stocks": payload.get("stocks", []),
                        "profile": str(payload.get("profile", "balanced")),
                    }, observed={"error": str(exc)}, source="automatic", severity="HIGH")
                    self._json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if path == "/api/collect/bilibili":
                self._json(collect_bilibili(str(payload.get("target", "")), bool(payload.get("use_browser", False))))
                return
            if path == "/api/collect/x":
                self._json(collect_x_recent(str(payload.get("query", "")), int(payload.get("max_results", 10))))
                return
            if path == "/api/collect/sec":
                self._json(collect_sec_submissions(str(payload.get("cik", ""))))
                return
            if path == "/api/reports/refresh":
                self._json(trigger_report_refresh())
                return
            if path == "/api/reports/stock":
                stock = resolve_stock(str(payload.get("stock", "")))
                register_report_equities([stock])
                result = refresh_stock_report(stock["symbol"])
                self._json({"stock": stock, "result": result})
                return
            if path == "/api/reports/sync/start":
                self._json(trigger_bulk_report_sync(
                    str(payload.get("mode", "full")), bool(payload.get("restart", False)),
                ))
                return
            if path == "/api/reports/sync/pause":
                self._json(pause_bulk_report_sync())
                return
            if path != "/api/risk/evaluate":
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            lines = calculate_risk_lines(
                float(payload["cost_price"]), float(payload["current_price"]),
                float(payload.get("highest_since_entry", payload["current_price"])),
                str(payload["market"]), str(payload["asset_type"]),
            )
            self._json({"lines": lines, "discipline": evaluate_discipline(float(payload["current_price"]), lines)})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except (RuntimeError, ImportError, ModuleNotFoundError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)


def run(host="127.0.0.1", port=8765):
    if not ipaddress.ip_address(host).is_loopback and not _configured_remote_token():
        raise RuntimeError("non-loopback service requires ARGUS_REMOTE_TOKEN_FILE")
    lock = ProcessLock(DATA_LAKE / "cache" / "service.lock")
    if not lock.acquire():
        raise RuntimeError("this data lake already has a running service; reuse it or explicitly replace it")
    try:
        # Bind before starting jobs so a port conflict cannot leave orphan workers.
        with closing(_server_class_for_host(host)((host, port), Handler)) as server:
            descriptor = {
                "process_id": os.getpid(), "python_executable": sys.executable,
                "runtime_root": str(ROOT.resolve()), "data_lake": str(DATA_LAKE.resolve()),
                "plugin_version": PLUGIN_VERSION, "runtime_revision": RUNTIME_REVISION,
                "bind_address": host, "port": port,
                "public_host": os.environ.get("ARGUS_PUBLIC_HOST", "127.0.0.1"),
                "token_file": os.environ.get("ARGUS_REMOTE_TOKEN_FILE", ""),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            descriptor_path = DATA_LAKE / "cache" / "service.json"
            temporary = descriptor_path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(descriptor), encoding="utf-8")
            os.replace(temporary, descriptor_path)
            _run_service(server, host, port)
    finally:
        lock.release()


def _run_service(server, host, port):
    global _service_monitor
    initialize()
    interrupted_harness_runs = recover_interrupted_harness_runs()
    interrupted_quant_runs = recover_interrupted_quant_runs()
    interrupted_report_jobs = recover_interrupted_report_sync()
    recover_interrupted_report_library_sync()
    interrupted_sector_cache_jobs = recover_sector_cache_jobs()
    refresher = None
    report_refresher = None
    daily_report_refresher = None
    harness_refresher = None
    continuous_learning_refresher = None
    sector_cache_refresher = None
    outbox_refresher = AlertOutboxRefresher()
    outbox_refresher.start()
    if os.environ.get("ARGUS_LIVE_REFRESH_ENABLED", "1") == "1":
        refresher = MarketDataRefresher()
        refresher.start()
    if os.environ.get("ARGUS_REPORT_REFRESH_ENABLED", "1") == "1":
        report_refresher = ReportLibraryRefresher()
        report_refresher.start()
        daily_report_refresher = DailyReportIncrementRefresher()
        daily_report_refresher.start()
        for mode in interrupted_report_jobs:
            trigger_bulk_report_sync(mode)
    if os.environ.get("ARGUS_HARNESS_AUTONOMY_ENABLED", "1") == "1":
        harness_refresher = AutonomousHarnessRefresher()
        harness_refresher.start()
    if os.environ.get("ARGUS_CONTINUOUS_LEARNING_ENABLED", "1") == "1":
        continuous_learning_refresher = ContinuousLearningRefresher()
        continuous_learning_refresher.start()
    if os.environ.get("ARGUS_SECTOR_CACHE_ENABLED", "1") == "1":
        sector_cache_refresher = SectorCacheRefresher()
        sector_cache_refresher.start()
    agent_worker = research_agent.start_worker()
    _service_monitor = ServiceMonitor()
    for key, label, worker, factory in (
        ("market", "行情与历史数据更新", refresher, MarketDataRefresher),
        ("reports", "研报与资料同步", report_refresher, ReportLibraryRefresher),
        ("report_increment", "研报每日增量", daily_report_refresher, DailyReportIncrementRefresher),
        ("audit", "研究质量巡检", harness_refresher, AutonomousHarnessRefresher),
        ("learning", "预测评分与策略自进化", continuous_learning_refresher, ContinuousLearningRefresher),
        ("sector", "板块行情与财务缓存", sector_cache_refresher, SectorCacheRefresher),
        ("outbox", "通知队列处理", outbox_refresher, AlertOutboxRefresher),
        ("codex", "投资研究助手", agent_worker, research_agent.start_worker),
    ):
        _service_monitor.register(key, label, worker, factory, enabled=worker is not None)
    _service_monitor.start()
    display_host = f"[{host}]" if ":" in host else host
    print(f"Market Intelligence Agent running at http://{display_host}:{port}")
    if interrupted_harness_runs:
        print(f"Harness recovered {interrupted_harness_runs} interrupted run(s); resume is available.")
    if interrupted_quant_runs:
        print(f"Quant research recovered {interrupted_quant_runs} interrupted run(s).")
    if interrupted_sector_cache_jobs:
        print(f"Sector cache recovered {interrupted_sector_cache_jobs} interrupted job(s).")
    print("Analysis only. Order execution is intentionally disabled.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if _service_monitor:
            _service_monitor.stop()
        if refresher:
            refresher.stop()
        if report_refresher:
            report_refresher.stop()
        if daily_report_refresher:
            daily_report_refresher.stop()
        if harness_refresher:
            harness_refresher.stop()
        if continuous_learning_refresher:
            continuous_learning_refresher.stop()
        if sector_cache_refresher:
            sector_cache_refresher.stop()
        outbox_refresher.stop()
        server.server_close()


if __name__ == "__main__":
    run()
