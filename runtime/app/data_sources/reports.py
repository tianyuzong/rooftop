"""Slow, resilient research-report and disclosure collector."""

from __future__ import annotations

import json
import os
import threading
from contextlib import closing
from datetime import date, datetime, timezone
from typing import Callable

from ..db import connect
from ..persistence import persist_source_documents
from ..social_sentiment import collect_multisource_sentiment

DEFAULT_WATCHLIST = {
    "funds": ("512400", "562500"),
    # Representatives are used only as theme research anchors, not as holdings.
    "research_equities": ("600519", "601600", "603019", "002747", "688777"),
}
_refresh_lock = threading.Lock()
_refresh_state = {
    "status": "IDLE", "started_at": None, "completed_at": None,
    "documents": 0, "errors": [], "started": False,
}
DAILY_MATERIAL_JOB_KEY = "materials_daily"
REPORT_DOCUMENT_TYPES = (
    "broker_research", "fund_announcement", "sec_filing_index", "macro_calendar",
)
NEWS_DOCUMENT_TYPES = (
    "a_share_news", "investor_participation_index", "social_video_metadata", "social_post",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_local_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().date()
    except (TypeError, ValueError):
        return None


def _latest_timestamp(*values: str | None) -> str | None:
    parsed_values = []
    for value in values:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            parsed_values.append((parsed.astimezone(timezone.utc), value))
        except (TypeError, ValueError):
            continue
    return max(parsed_values, key=lambda item: item[0])[1] if parsed_values else None


def _ensure_daily_material_job(conn) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO report_sync_jobs(job_key,mode,status,updated_at)
           VALUES(?,?,'IDLE',?)""",
        (DAILY_MATERIAL_JOB_KEY, DAILY_MATERIAL_JOB_KEY, _now()),
    )


def _document_group_status(conn, document_types: tuple[str, ...]) -> dict:
    placeholders = ",".join("?" for _ in document_types)
    row = conn.execute(
        f"""SELECT COUNT(*) AS document_count, MAX(published_at) AS latest_published_at,
                   MAX(captured_at) AS latest_captured_at
            FROM source_documents WHERE document_type IN ({placeholders})""",
        document_types,
    ).fetchone()
    return {
        "document_count": int(row["document_count"] or 0),
        "latest_published_at": row["latest_published_at"],
        "latest_captured_at": row["latest_captured_at"],
    }


def research_material_sync_status() -> dict:
    """Return persistent freshness evidence for everything exposed by material search."""
    with closing(connect()) as conn:
        _ensure_daily_material_job(conn)
        conn.commit()
        job = dict(conn.execute(
            "SELECT * FROM report_sync_jobs WHERE job_key=?", (DAILY_MATERIAL_JOB_KEY,),
        ).fetchone())
        incremental_row = conn.execute(
            "SELECT * FROM report_sync_jobs WHERE job_key='incremental'"
        ).fetchone()
        incremental = dict(incremental_row) if incremental_row else {
            "status": "IDLE", "completed_at": None, "updated_at": None,
            "last_error": None, "error_count": 0,
        }
        report_group = _document_group_status(conn, REPORT_DOCUMENT_TYPES)
        news_group = _document_group_status(conn, NEWS_DOCUMENT_TYPES)
        tracked_symbols = int(conn.execute(
            "SELECT COUNT(*) FROM report_watchlist WHERE enabled=1"
        ).fetchone()[0])
    today = date.today()
    completion_date = _timestamp_local_date(job.get("completed_at"))
    attempted_date = _timestamp_local_date(job.get("updated_at"))
    material_completed_today = job.get("status") == "SUCCESS" and completion_date == today
    report_increment_completed_today = (
        incremental.get("status") == "SUCCESS"
        and _timestamp_local_date(incremental.get("completed_at")) == today
    )
    completed_today = material_completed_today and report_increment_completed_today
    attempted_today = attempted_date == today
    if completed_today:
        label = "今日已更新"
        tone = "good"
        overall_status = "SUCCESS"
    elif job.get("status") == "RUNNING" or incremental.get("status") in {"RUNNING", "PAUSING"}:
        label = "正在更新"
        tone = "warn"
        overall_status = "RUNNING"
    elif (material_completed_today or report_increment_completed_today
          or (job.get("status") == "DEGRADED" and attempted_today)):
        label = "今日部分完成，缺失来源待重试"
        tone = "warn"
        overall_status = "DEGRADED"
    elif (job.get("status") == "FAILED" and attempted_today
          or incremental.get("status") == "FAILED"):
        label = "今日更新失败，等待重试"
        tone = "bad"
        overall_status = "FAILED"
    else:
        label = "今日待更新"
        tone = "muted"
        overall_status = "IDLE"
    latest_completed_at = _latest_timestamp(
        job.get("completed_at"), incremental.get("completed_at"),
    )
    errors = [value for value in (job.get("last_error"), incremental.get("last_error")) if value]
    return {
        "schedule": "DAILY_WITH_HOURLY_CATCHUP",
        "status": overall_status,
        "status_label": label,
        "status_tone": tone,
        "completed_today": completed_today,
        "material_completed_today": material_completed_today,
        "report_increment_completed_today": report_increment_completed_today,
        "attempted_today": attempted_today,
        "started_at": job.get("started_at"),
        "completed_at": latest_completed_at,
        "updated_at": job.get("updated_at"),
        "last_error": "；".join(errors) if errors else None,
        "error_count": int(job.get("error_count") or 0) + int(incremental.get("error_count") or 0),
        "tracked_symbols": tracked_symbols,
        "components": {
            "watchlist_materials": {
                "status": job.get("status") or "IDLE",
                "completed_at": job.get("completed_at"),
            },
            "market_report_increment": {
                "status": incremental.get("status") or "IDLE",
                "completed_at": incremental.get("completed_at"),
            },
        },
        "groups": {
            "reports": {"label": "研报、公告与财经日历", **report_group},
            "news": {"label": "新闻与公开舆情", **news_group},
        },
    }


def _set_daily_material_job(status: str, result: dict) -> None:
    completed_at = result.get("completed_at") or _now()
    errors = result.get("errors") or []
    last_error = json.dumps(errors, ensure_ascii=False)[:4000] if errors else None
    with closing(connect()) as conn:
        _ensure_daily_material_job(conn)
        conn.execute(
            """UPDATE report_sync_jobs SET status=?,total_hits=?,fetched_rows=?,stored_rows=?,
               error_count=?,completed_at=?,updated_at=?,last_error=? WHERE job_key=?""",
            (status, int(result.get("documents") or 0), int(result.get("documents") or 0),
             int(result.get("documents") or 0), len(errors), completed_at, _now(), last_error,
             DAILY_MATERIAL_JOB_KEY),
        )
        conn.commit()


def recover_interrupted_report_library_sync() -> bool:
    with closing(connect()) as conn:
        _ensure_daily_material_job(conn)
        row = conn.execute(
            "SELECT status FROM report_sync_jobs WHERE job_key=?", (DAILY_MATERIAL_JOB_KEY,),
        ).fetchone()
        interrupted = bool(row and row["status"] == "RUNNING")
        if interrupted:
            conn.execute(
                """UPDATE report_sync_jobs SET status='INTERRUPTED',updated_at=?,
                   last_error='本地服务重启，今日资料同步将自动补跑' WHERE job_key=?""",
                (_now(), DAILY_MATERIAL_JOB_KEY),
            )
        conn.commit()
    return interrupted


def _max_equities() -> int:
    try:
        return max(2, min(20, int(os.environ.get("ARGUS_REPORT_MAX_EQUITIES", "8"))))
    except ValueError:
        return 8


def register_report_equities(stocks: list[dict]) -> None:
    """Persist recently compared equities so report collection follows the user's work."""
    requested_at = datetime.now(timezone.utc).isoformat()
    with closing(connect()) as conn:
        for stock in stocks:
            symbol = str(stock.get("symbol") or "").strip()
            if not symbol.isdigit() or len(symbol) != 6:
                continue
            conn.execute(
                """INSERT INTO report_watchlist(symbol,name,last_requested_at,enabled)
                   VALUES(?,?,?,1) ON CONFLICT(symbol) DO UPDATE SET
                   name=excluded.name,last_requested_at=excluded.last_requested_at,enabled=1""",
                (symbol, str(stock.get("name") or symbol).strip(), requested_at),
            )
        conn.commit()


def current_report_watchlist() -> dict[str, tuple[str, ...]]:
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT symbol FROM report_watchlist WHERE enabled=1
               ORDER BY last_requested_at DESC LIMIT ?""",
            (_max_equities(),),
        ).fetchall()
    equities = tuple(str(row["symbol"]) for row in rows) or DEFAULT_WATCHLIST["research_equities"]
    return {"funds": DEFAULT_WATCHLIST["funds"], "research_equities": equities}


def _string(value) -> str | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
    except Exception:
        pass
    return str(value)


def _rows_to_documents(frame, document_type: str, title_column: str, url_column: str | None,
                       source_name: str, date_column: str | None, metadata_columns: list[str]) -> list[dict]:
    documents = []
    if date_column and date_column in frame.columns:
        frame = frame.sort_values(date_column, ascending=False)
    for row in frame.head(30).to_dict("records"):
        title = _string(row.get(title_column)) or "未命名研究资料"
        metadata = {column: _string(row.get(column)) for column in metadata_columns if column in row}
        body = "；".join(f"{key}：{value}" for key, value in metadata.items() if value)
        documents.append({"document_type": document_type, "title": title, "body": f"（事实）{body}",
                          "source_url": _string(row.get(url_column)) if url_column else None,
                          "source_name": source_name,
                          "published_at": _string(row.get(date_column)) if date_column else None,
                          "observed_at": datetime.now(timezone.utc).isoformat(), "metadata": metadata})
    return documents


def _collect_one(source_code: str, dataset: str, query_key: str, fetch: Callable, converter: Callable) -> dict:
    frame = fetch()
    documents = converter(frame)
    raw = frame.to_json(orient="records", force_ascii=False, date_format="iso")
    return persist_source_documents(source_code, dataset, query_key, documents, raw)


def _collect_research_report(ak, symbol: str) -> dict:
    return _collect_one(
        "akshare", "research_report", symbol,
        lambda: ak.stock_research_report_em(symbol=symbol),
        lambda frame: _rows_to_documents(
            frame, "broker_research", "报告名称", "报告PDF链接", "东方财富研报",
            "日期", ["股票代码", "股票简称", "机构", "东财评级", "行业", "日期"],
        ),
    )


def refresh_stock_report(symbol: str) -> dict:
    """Fetch and persist one requested A-share's latest public research reports."""
    symbol = str(symbol or "").strip()
    if not symbol.isdigit() or len(symbol) != 6:
        raise ValueError("股票代码必须是 6 位数字")
    try:
        import akshare as ak
        return _collect_research_report(ak, symbol)
    except KeyError as exc:
        if exc.args != ("infoCode",):
            raise RuntimeError(f"研报接口字段异常：{exc}") from exc
        result = persist_source_documents("akshare", "research_report", symbol, [], "[]")
        result["message"] = "当前暂无公开券商研报"
        return result
    except Exception as exc:
        raise RuntimeError(f"未能获取股票 {symbol} 的公开研报：{exc}") from exc


def refresh_report_library() -> dict:
    """Run one auditable daily material refresh across reports, news and public sentiment."""
    if not _refresh_lock.acquire(blocking=False):
        return {**_refresh_state, "status": "RUNNING", "started": False}
    results, errors = [], []
    started_at = _now()
    _refresh_state.update(
        status="RUNNING", started_at=started_at, completed_at=None,
        documents=0, errors=[], started=True,
    )
    with closing(connect()) as conn:
        _ensure_daily_material_job(conn)
        conn.execute(
            """UPDATE report_sync_jobs SET status='RUNNING',started_at=?,completed_at=NULL,
               updated_at=?,last_error=NULL WHERE job_key=?""",
            (started_at, started_at, DAILY_MATERIAL_JOB_KEY),
        )
        conn.commit()
    try:
        import akshare as ak
        watchlist = current_report_watchlist()
        for symbol in watchlist["funds"]:
            try:
                results.append(_collect_one(
                "akshare", "fund_announcement", symbol,
                lambda symbol=symbol: ak.fund_announcement_report_em(symbol=symbol),
                lambda frame: _rows_to_documents(frame, "fund_announcement", "公告标题", None, "东方财富基金公告",
                                                 "公告日期", ["基金代码", "基金名称", "公告日期", "报告ID"]),
                ))
            except Exception as exc:
                errors.append({"dataset": "fund_announcement", "symbol": symbol, "error": repr(exc)})
        for symbol in watchlist["research_equities"]:
            try:
                results.append(_collect_research_report(ak, symbol))
            except Exception as exc:
                errors.append({"dataset": "research_report", "symbol": symbol, "error": repr(exc)})
        try:
            date_key = datetime.now().strftime("%Y%m%d")
            results.append(_collect_one(
                "akshare", "macro_calendar", date_key,
                lambda: ak.macro_info_ws(date=date_key),
                lambda frame: _rows_to_documents(frame, "macro_calendar", "事件", "链接", "华尔街见闻财经日历",
                                                 "时间", ["时间", "地区", "重要性", "今值", "预期", "前值"]),
            ))
        except Exception as exc:
            errors.append({"dataset": "macro_calendar", "symbol": "GLOBAL", "error": repr(exc)})

        sentiment = None
        try:
            sentiment = collect_multisource_sentiment(
                watchlist["research_equities"], as_of=date.today(),
                max_social_symbols=max(0, int(os.environ.get("ARGUS_DAILY_SOCIAL_SYMBOLS", "3"))),
            )
            errors.extend({"dataset": "news_and_public_sentiment", **item}
                          for item in sentiment.get("errors") or [])
        except Exception as exc:
            errors.append({
                "dataset": "news_and_public_sentiment", "symbol": "WATCHLIST",
                "error": repr(exc),
            })

        report_documents = sum(int(item.get("rows") or 0) for item in results)
        sentiment_documents = int((sentiment or {}).get("documents") or 0)
        had_success = bool(results or (sentiment or {}).get("symbols"))
        status = "SUCCESS" if not errors else ("DEGRADED" if had_success else "FAILED")
        result = {
            "status": status, "started": True, "started_at": started_at,
            "completed_at": _now(), "queries": len(results) + len(watchlist["research_equities"]),
            "documents": report_documents + sentiment_documents,
            "report_documents": report_documents,
            "news_and_sentiment_documents": sentiment_documents,
            "tracked_symbols": len(watchlist["research_equities"]),
            "errors": errors,
        }
        _set_daily_material_job(status, result)
        _refresh_state.update(result)
        return result
    except Exception as exc:
        result = {
            "status": "FAILED", "started": True, "started_at": started_at,
            "completed_at": _now(), "documents": 0,
            "errors": [{"dataset": "daily_materials", "error": repr(exc)}],
        }
        _set_daily_material_job("FAILED", result)
        _refresh_state.update(result)
        return result
    finally:
        _refresh_lock.release()


def refresh_report_library_if_due() -> dict:
    sync = research_material_sync_status()
    if sync["material_completed_today"]:
        return {
            "status": "UP_TO_DATE", "started": False,
            "completed_at": sync.get("completed_at"),
            "documents": sum(int(item.get("document_count") or 0)
                             for item in sync["groups"].values()),
            "errors": [], "message": "今日关注股票资料已经同步完成",
        }
    return refresh_report_library()


def trigger_report_refresh() -> dict:
    if _refresh_lock.locked():
        return {**_refresh_state, "status": "RUNNING", "started": False}
    thread = threading.Thread(target=refresh_report_library, name="argus-report-manual", daemon=True)
    thread.start()
    return {**_refresh_state, "status": "RUNNING", "started": True}


def report_library_payload(limit: int = 80) -> dict:
    with closing(connect()) as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT id,document_type,title,body,source_url,source_name,published_at,captured_at,metadata_json
               FROM source_documents WHERE document_type IN ('fund_announcement','broker_research','sec_filing_index','macro_calendar')
               ORDER BY COALESCE(published_at,captured_at) DESC LIMIT ?""", (limit,)
        )]
        last_run = conn.execute(
            """SELECT finished_at,status,row_count,error FROM ingestion_runs
               WHERE dataset IN ('fund_announcement','research_report','filings','macro_calendar') ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    for row in rows:
        row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
    return {"documents": rows, "last_run": dict(last_run) if last_run else None,
            "refresh": dict(_refresh_state), "daily_sync": research_material_sync_status(),
            "poll_seconds": 3600, "watchlist": current_report_watchlist()}
