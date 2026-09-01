"""Durable full-sector market and fundamental cache backfill.

The worker stores research data only. It never creates orders or modifies the
formal portfolio publication gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from .db import connect, initialize


DEFAULT_RESEARCH_SECTORS = ("白酒", "新能源", "半导体", "航空")
MODEL_READY_ROWS = 420
RETENTION_DAYS = 365
_WORKER_LOCK = threading.Lock()
_ACTIVE_WORKER: threading.Thread | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


def _target_market_date() -> str:
    today = date.today().isoformat()
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM trading_calendar WHERE is_open=1 AND trade_date<=?",
            (today,),
        ).fetchone()
        if row and row[0]:
            return str(row[0])
        row = conn.execute(
            "SELECT MAX(training_end) FROM prediction_model_versions WHERE status='ACTIVE'"
        ).fetchone()
        if row and row[0]:
            return str(row[0])
        row = conn.execute("SELECT MAX(trade_date) FROM market_daily_bars").fetchone()
    return str(row[0]) if row and row[0] else today


def _market_coverage(symbols: list[str], target_asof: str) -> dict[str, dict]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with closing(connect()) as conn:
        rows = conn.execute(
            f"""SELECT asset_symbol,COUNT(DISTINCT trade_date) row_count,
                       MIN(trade_date) data_start,MAX(trade_date) data_end
                FROM market_daily_bars
                WHERE adjust_mode='qfq' AND trade_date<=?
                  AND asset_symbol IN ({placeholders})
                GROUP BY asset_symbol""",
            (target_asof, *symbols),
        ).fetchall()
    return {str(row["asset_symbol"]): dict(row) for row in rows}


def _fundamental_coverage(symbols: list[str], target_asof: str) -> dict[str, dict]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with closing(connect()) as conn:
        reports = {
            str(row["symbol"]): int(row["report_count"])
            for row in conn.execute(
                f"""SELECT symbol,COUNT(*) report_count FROM fundamental_reports
                     WHERE symbol IN ({placeholders}) AND notice_date<=?
                     GROUP BY symbol""",
                (*symbols, target_asof),
            )
        }
        valuations = {
            str(row["symbol"]): str(row["valuation_date"])
            for row in conn.execute(
                f"""SELECT symbol,MAX(asof_date) valuation_date
                     FROM fundamental_valuations
                     WHERE symbol IN ({placeholders}) AND asof_date<=?
                     GROUP BY symbol""",
                (*symbols, target_asof),
            )
            if row["valuation_date"]
        }
    return {
        symbol: {
            "report_count": reports.get(symbol, 0),
            "valuation_date": valuations.get(symbol),
        }
        for symbol in symbols
    }


def _resolved_sector_members(sectors: Iterable[str]) -> tuple[dict, list[dict]]:
    from .quant_portfolio import _resolve_sectors, _sector_candidates, sync_a_share_universe

    sync_a_share_universe()
    normalized = list(dict.fromkeys(str(item).strip() for item in sectors if str(item).strip()))
    resolution = _resolve_sectors(normalized)
    members = _sector_candidates(resolution["codes"])
    members = [item for item in members if "ST" not in item["name"].upper() and "退" not in item["name"]]
    return resolution, members


def _job_payload(row, include_errors: bool = True) -> dict:
    if not row:
        return {
            "status": "IDLE", "sectors": list(DEFAULT_RESEARCH_SECTORS),
            "retention_days": RETENTION_DAYS, "model_ready_rows": MODEL_READY_ROWS,
            "order_execution": False,
        }
    item = dict(row)
    item["sectors"] = _load(item.pop("sectors_json"), [])
    item["sector_codes"] = _load(item.pop("sector_codes_json"), [])
    total = max(1, int(item["total_symbols"] or 0))
    item["market_percent"] = round(int(item["market_processed"] or 0) / total * 100, 1)
    item["fundamental_percent"] = round(
        int(item["fundamentals_processed"] or 0) / total * 100, 1
    )
    item["retention_days"] = RETENTION_DAYS
    item["model_ready_rows"] = MODEL_READY_ROWS
    item["order_execution"] = False
    if include_errors:
        with closing(connect()) as conn:
            errors = conn.execute(
                """SELECT symbol,name,market_status,fundamental_status,last_error
                   FROM sector_cache_items WHERE job_id=? AND last_error IS NOT NULL
                   ORDER BY updated_at DESC LIMIT 12""",
                (item["id"],),
            ).fetchall()
        item["recent_errors"] = [dict(error) for error in errors]
    return item


def sector_cache_status() -> dict:
    initialize()
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM sector_cache_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return _job_payload(row)


def _sync_job_counts(conn, job_id: int) -> None:
    row = conn.execute(
        "SELECT target_asof FROM sector_cache_jobs WHERE id=?", (job_id,)
    ).fetchone()
    if not row:
        return
    target_asof = str(row["target_asof"])
    counts = conn.execute(
        """SELECT
             SUM(CASE WHEN market_status!='PENDING' THEN 1 ELSE 0 END) market_processed,
             SUM(CASE WHEN row_count>0 THEN 1 ELSE 0 END) market_cached,
             SUM(CASE WHEN row_count>=? AND data_end=? THEN 1 ELSE 0 END) model_ready,
             SUM(CASE WHEN fundamental_status!='PENDING' THEN 1 ELSE 0 END) fundamentals_processed,
             SUM(CASE WHEN report_count>0 AND valuation_date IS NOT NULL THEN 1 ELSE 0 END) fundamentals_cached,
             SUM(CASE WHEN market_status='FAILED' OR fundamental_status='FAILED' THEN 1 ELSE 0 END) error_count
           FROM sector_cache_items WHERE job_id=?""",
        (MODEL_READY_ROWS, target_asof, job_id),
    ).fetchone()
    conn.execute(
        """UPDATE sector_cache_jobs SET market_processed=?,market_cached=?,model_ready=?,
             fundamentals_processed=?,fundamentals_cached=?,error_count=?,updated_at=?
           WHERE id=?""",
        tuple(int(counts[key] or 0) for key in (
            "market_processed", "market_cached", "model_ready",
            "fundamentals_processed", "fundamentals_cached", "error_count",
        )) + (_now(), job_id),
    )


def _refresh_market_batch(job_id: int, symbols: list[str], target_asof: str) -> None:
    from .continuous_learning import _refresh_market_data_isolated

    errors: dict[str, str] = {}
    try:
        result = _refresh_market_data_isolated(symbols, include_minutes=False)
        for error in result.get("errors", []):
            symbol = str(error.get("symbol") or "")
            if symbol in symbols and error.get("dataset") == "daily_qfq":
                errors[symbol] = str(error.get("error") or "daily refresh failed")
    except Exception as exc:
        errors = {symbol: repr(exc) for symbol in symbols}
    coverage = _market_coverage(symbols, target_asof)
    stamp = _now()
    with closing(connect()) as conn:
        for symbol in symbols:
            value = coverage.get(symbol, {})
            rows = int(value.get("row_count") or 0)
            data_end = value.get("data_end")
            if rows and str(data_end) == target_asof:
                status = "CACHED"
            elif rows:
                status = "PARTIAL"
            else:
                status = "FAILED"
            error = errors.get(symbol)
            if status == "FAILED" and not error:
                error = "未取得有效日线"
            conn.execute(
                """UPDATE sector_cache_items SET market_status=?,row_count=?,data_start=?,
                     data_end=?,attempts=attempts+1,last_error=?,updated_at=?
                   WHERE job_id=? AND symbol=?""",
                (status, rows, value.get("data_start"), data_end, error, stamp,
                 job_id, symbol),
            )
        _sync_job_counts(conn, job_id)
        conn.commit()


def _refresh_fundamental_batch(job_id: int, symbols: list[str], target_asof: str) -> None:
    from .fundamentals import refresh_fundamental_snapshots

    result = refresh_fundamental_snapshots(symbols, target_asof)
    errors = {str(item.get("symbol")): str(item.get("error"))
              for item in result.get("errors", [])}
    coverage = _fundamental_coverage(symbols, target_asof)
    stamp = _now()
    with closing(connect()) as conn:
        for symbol in symbols:
            value = coverage.get(symbol, {})
            reports = int(value.get("report_count") or 0)
            valuation_date = value.get("valuation_date")
            if reports and valuation_date:
                status = "CACHED"
            elif reports or valuation_date:
                status = "PARTIAL"
            else:
                status = "FAILED"
            conn.execute(
                """UPDATE sector_cache_items SET fundamental_status=?,report_count=?,
                     valuation_date=?,last_error=COALESCE(?,last_error),updated_at=?
                   WHERE job_id=? AND symbol=?""",
                (status, reports, valuation_date, errors.get(symbol), stamp,
                 job_id, symbol),
            )
        _sync_job_counts(conn, job_id)
        conn.commit()


def _run_job(job_id: int) -> None:
    market_batch_size = max(1, min(int(os.environ.get("ARGUS_SECTOR_CACHE_MARKET_BATCH", "12")), 30))
    fundamental_batch_size = max(
        1, min(int(os.environ.get("ARGUS_SECTOR_CACHE_FUNDAMENTAL_BATCH", "3")), 10)
    )
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT target_asof FROM sector_cache_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row:
            return
        target_asof = str(row["target_asof"])
        conn.execute(
            """UPDATE sector_cache_jobs SET status='RUNNING_MARKET',
               started_at=COALESCE(started_at,?),updated_at=? WHERE id=?""",
            (_now(), _now(), job_id),
        )
        conn.commit()
    while True:
        with closing(connect()) as conn:
            rows = conn.execute(
                """SELECT i.symbol FROM sector_cache_items i
                   WHERE i.job_id=? AND i.market_status='PENDING'
                   ORDER BY CASE
                     WHEN EXISTS (
                       SELECT 1 FROM a_share_sector_memberships m
                       WHERE m.symbol=i.symbol AND m.sector_name='航空'
                     ) THEN 0
                     WHEN EXISTS (
                       SELECT 1 FROM a_share_sector_memberships m
                       WHERE m.symbol=i.symbol AND m.sector_name='半导体'
                     ) THEN 1
                     WHEN EXISTS (
                       SELECT 1 FROM a_share_sector_memberships m
                       WHERE m.symbol=i.symbol AND m.sector_name='白酒'
                     ) THEN 2 ELSE 3 END,
                     i.symbol LIMIT ?""",
                (job_id, market_batch_size),
            ).fetchall()
            symbols = [str(row["symbol"]) for row in rows]
            if symbols:
                conn.execute(
                    "UPDATE sector_cache_jobs SET current_symbol=?,updated_at=? WHERE id=?",
                    (symbols[0], _now(), job_id),
                )
                conn.commit()
        if not symbols:
            break
        _refresh_market_batch(job_id, symbols, target_asof)
    with closing(connect()) as conn:
        conn.execute(
            """UPDATE sector_cache_jobs SET status='RUNNING_FUNDAMENTALS',
               current_symbol=NULL,updated_at=? WHERE id=?""",
            (_now(), job_id),
        )
        conn.commit()
    while True:
        with closing(connect()) as conn:
            rows = conn.execute(
                """SELECT symbol FROM sector_cache_items
                   WHERE job_id=? AND fundamental_status='PENDING'
                   ORDER BY symbol LIMIT ?""",
                (job_id, fundamental_batch_size),
            ).fetchall()
            symbols = [str(row["symbol"]) for row in rows]
            if symbols:
                conn.execute(
                    "UPDATE sector_cache_jobs SET current_symbol=?,updated_at=? WHERE id=?",
                    (symbols[0], _now(), job_id),
                )
                conn.commit()
        if not symbols:
            break
        _refresh_fundamental_batch(job_id, symbols, target_asof)
    with closing(connect()) as conn:
        _sync_job_counts(conn, job_id)
        row = conn.execute(
            "SELECT error_count FROM sector_cache_jobs WHERE id=?", (job_id,)
        ).fetchone()
        status = "COMPLETED_WITH_WARNINGS" if int(row["error_count"] or 0) else "COMPLETED"
        conn.execute(
            """UPDATE sector_cache_jobs SET status=?,current_symbol=NULL,
               completed_at=?,updated_at=? WHERE id=?""",
            (status, _now(), _now(), job_id),
        )
        conn.commit()


def _worker_loop() -> None:
    global _ACTIVE_WORKER
    try:
        while True:
            with closing(connect()) as conn:
                row = conn.execute(
                    """SELECT id FROM sector_cache_jobs WHERE status='QUEUED'
                       ORDER BY id LIMIT 1"""
                ).fetchone()
            if not row:
                return
            job_id = int(row["id"])
            try:
                _run_job(job_id)
            except Exception as exc:
                with closing(connect()) as conn:
                    conn.execute(
                        """UPDATE sector_cache_jobs SET status='FAILED',last_error=?,
                           completed_at=?,updated_at=? WHERE id=?""",
                        (repr(exc), _now(), _now(), job_id),
                    )
                    conn.commit()
    finally:
        with _WORKER_LOCK:
            _ACTIVE_WORKER = None


def _ensure_worker() -> None:
    global _ACTIVE_WORKER
    with _WORKER_LOCK:
        if _ACTIVE_WORKER and _ACTIVE_WORKER.is_alive():
            return
        _ACTIVE_WORKER = threading.Thread(
            target=_worker_loop, name="argus-sector-cache", daemon=True
        )
        _ACTIVE_WORKER.start()


def trigger_sector_cache(sectors: Iterable[str] = DEFAULT_RESEARCH_SECTORS,
                         force: bool = False) -> dict:
    initialize()
    resolution, members = _resolved_sector_members(sectors)
    if not members:
        raise RuntimeError("所选板块没有可缓存的 A 股成员")
    target_asof = _target_market_date()
    retention_start = (date.fromisoformat(target_asof) - timedelta(days=RETENTION_DAYS)).isoformat()
    identity = _dump({"codes": resolution["codes"], "target_asof": target_asof})
    job_key = "sector-cache-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    symbols = [str(item["symbol"]) for item in members]
    market = _market_coverage(symbols, target_asof)
    fundamentals = _fundamental_coverage(symbols, target_asof)
    stamp = _now()
    with closing(connect()) as conn:
        existing = conn.execute(
            "SELECT * FROM sector_cache_jobs WHERE job_key=?", (job_key,)
        ).fetchone()
        active_statuses = {"QUEUED", "RUNNING_MARKET", "RUNNING_FUNDAMENTALS"}
        if existing and str(existing["status"]) in active_statuses:
            _ensure_worker()
            return _job_payload(existing)
        if existing and not force:
            if str(existing["status"]) in active_statuses:
                _ensure_worker()
            return _job_payload(existing)
        if existing:
            conn.execute("DELETE FROM sector_cache_items WHERE job_id=?", (existing["id"],))
            conn.execute("DELETE FROM sector_cache_jobs WHERE id=?", (existing["id"],))
        cursor = conn.execute(
            """INSERT INTO sector_cache_jobs
               (job_key,status,sectors_json,sector_codes_json,target_asof,retention_start,
                total_symbols,requested_at,updated_at)
               VALUES(?,'QUEUED',?,?,?,?,?,?,?)""",
            (job_key, _dump(list(sectors)), _dump(resolution["codes"]), target_asof,
             retention_start, len(members), stamp, stamp),
        )
        job_id = int(cursor.lastrowid)
        rows = []
        for member in members:
            symbol = str(member["symbol"])
            history = market.get(symbol, {})
            row_count = int(history.get("row_count") or 0)
            data_end = history.get("data_end")
            market_status = (
                "CACHED" if row_count >= MODEL_READY_ROWS and str(data_end) == target_asof else "PENDING"
            )
            fundamental = fundamentals.get(symbol, {})
            report_count = int(fundamental.get("report_count") or 0)
            valuation_date = fundamental.get("valuation_date")
            fundamental_status = (
                "CACHED" if report_count and valuation_date else "PENDING"
            )
            rows.append((
                job_id, symbol, str(member["name"]), market_status,
                fundamental_status, row_count, history.get("data_start"), data_end,
                report_count, valuation_date, stamp,
            ))
        conn.executemany(
            """INSERT INTO sector_cache_items
               (job_id,symbol,name,market_status,fundamental_status,row_count,
                data_start,data_end,report_count,valuation_date,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        _sync_job_counts(conn, job_id)
        conn.commit()
        job = conn.execute("SELECT * FROM sector_cache_jobs WHERE id=?", (job_id,)).fetchone()
    _ensure_worker()
    return _job_payload(job)


def trigger_active_sector_cache(force: bool = False) -> dict:
    return trigger_sector_cache(DEFAULT_RESEARCH_SECTORS, force=force)


def recover_sector_cache_jobs() -> int:
    initialize()
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT id FROM sector_cache_jobs
               WHERE status IN ('RUNNING_MARKET','RUNNING_FUNDAMENTALS')"""
        ).fetchall()
        if rows:
            conn.execute(
                """UPDATE sector_cache_jobs SET status='QUEUED',current_symbol=NULL,
                   updated_at=? WHERE status IN ('RUNNING_MARKET','RUNNING_FUNDAMENTALS')""",
                (_now(),),
            )
            conn.commit()
    if rows:
        _ensure_worker()
    return len(rows)
