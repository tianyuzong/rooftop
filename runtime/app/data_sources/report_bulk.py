"""Resumable full-market research-report ingestion and daily increments."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

from ..db import connect
from ..persistence import persist_source_documents

REPORT_API_URL = "https://reportapi.eastmoney.com/report/list"
PAGE_SIZE = 100
_thread_guard = threading.Lock()
_active_thread: threading.Thread | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_local_date(value: str | None, local_timezone=None) -> date | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        target_timezone = local_timezone or datetime.now().astimezone().tzinfo or timezone.utc
        return parsed.astimezone(target_timezone).date()
    except (TypeError, ValueError):
        return None


def _rate_delay() -> float:
    try:
        return max(0.1, min(5.0, float(os.environ.get("ARGUS_REPORT_BULK_DELAY_SECONDS", "0.25"))))
    except ValueError:
        return 0.25


def _ensure_jobs(conn) -> None:
    now = _now()
    for key in ("full", "incremental"):
        conn.execute(
            """INSERT OR IGNORE INTO report_sync_jobs(job_key,mode,status,updated_at)
               VALUES(?,?,'IDLE',?)""",
            (key, key, now),
        )


def _row_dict(row) -> dict:
    item = dict(row)
    total = int(item.get("total_pages") or 0)
    processed = int(item.get("processed_pages") or 0)
    item["progress_pct"] = round(processed / total * 100, 2) if total else 0.0
    return item


def bulk_report_status() -> dict:
    with closing(connect()) as conn:
        _ensure_jobs(conn)
        conn.commit()
        rows = conn.execute("SELECT * FROM report_sync_jobs ORDER BY job_key").fetchall()
        document_count = conn.execute(
            "SELECT COUNT(*) FROM source_documents WHERE document_type='broker_research'"
        ).fetchone()[0]
        symbol_count = conn.execute(
            """SELECT COUNT(DISTINCT json_extract(metadata_json,'$.股票代码'))
               FROM source_documents WHERE document_type='broker_research'"""
        ).fetchone()[0]
    active = _active_thread is not None and _active_thread.is_alive()
    return {
        "jobs": {row["job_key"]: _row_dict(row) for row in rows},
        "active": active,
        "documents": int(document_count),
        "symbols": int(symbol_count),
        "page_size": PAGE_SIZE,
        "daily_incremental": True,
    }


def _latest_report_date() -> date:
    with closing(connect()) as conn:
        value = conn.execute(
            """SELECT MAX(substr(published_at,1,10)) FROM source_documents
               WHERE document_type='broker_research'"""
        ).fetchone()[0]
    try:
        return date.fromisoformat(value) if value else date.today() - timedelta(days=7)
    except ValueError:
        return date.today() - timedelta(days=7)


def _prepare_job(mode: str, restart: bool = False) -> tuple[dict, bool]:
    if mode not in {"full", "incremental"}:
        raise ValueError("同步模式必须是 full 或 incremental")
    today = date.today()
    with closing(connect()) as conn:
        _ensure_jobs(conn)
        row = conn.execute("SELECT * FROM report_sync_jobs WHERE job_key=?", (mode,)).fetchone()
        current = dict(row)
        if mode == "full" and current["status"] == "SUCCESS" and not restart:
            conn.commit()
            return _row_dict(row), False
        if mode == "incremental" and current["status"] == "SUCCESS" and \
                _timestamp_local_date(current.get("completed_at")) == today and not restart:
            conn.commit()
            return _row_dict(row), False
        resumable = (not restart and current["status"] in {"PAUSED", "FAILED", "INTERRUPTED"}
                     and int(current.get("next_page") or 1) > 1 and current.get("begin_date")
                     and current.get("end_date"))
        if resumable:
            conn.execute(
                """UPDATE report_sync_jobs SET status='RUNNING',stop_requested=0,
                   updated_at=?,last_error=NULL WHERE job_key=?""",
                (_now(), mode),
            )
        else:
            begin_date = date(2000, 1, 1) if mode == "full" else _latest_report_date() - timedelta(days=7)
            conn.execute(
                """UPDATE report_sync_jobs SET status='RUNNING',begin_date=?,end_date=?,next_page=1,
                   total_pages=0,total_hits=0,processed_pages=0,fetched_rows=0,stored_rows=0,
                   error_count=0,stop_requested=0,started_at=?,completed_at=NULL,updated_at=?,last_error=NULL
                   WHERE job_key=?""",
                (begin_date.isoformat(), today.isoformat(), _now(), _now(), mode),
            )
        conn.commit()
        return _row_dict(conn.execute("SELECT * FROM report_sync_jobs WHERE job_key=?", (mode,)).fetchone()), True


def _request_page(begin_date: str, end_date: str, page: int) -> dict:
    params = {
        "industryCode": "*", "pageSize": str(PAGE_SIZE), "industry": "*", "rating": "*",
        "ratingChange": "*", "beginTime": begin_date, "endTime": end_date,
        "pageNo": str(page), "fields": "", "qType": "0", "orgCode": "", "code": "",
        "rcode": "", "p": str(page), "pageNum": str(page), "pageNumber": str(page),
    }
    request = urllib.request.Request(
        REPORT_API_URL + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": "Mozilla/5.0 ArgusResearchArchive/1.0",
                 "Referer": "https://data.eastmoney.com/report/"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload.get("data"), list):
        raise RuntimeError("全市场研报接口返回结构异常")
    return payload


def _request_page_with_retry(begin_date: str, end_date: str, page: int) -> dict:
    error = None
    for attempt in range(4):
        try:
            return _request_page(begin_date, end_date, page)
        except Exception as exc:
            error = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"第 {page} 页连续获取失败：{error}") from error


def _payload_to_documents(payload: dict) -> list[dict]:
    observed = _now()
    documents = []
    for item in payload.get("data") or []:
        code = str(item.get("stockCode") or "").strip()
        name = str(item.get("stockName") or code).strip()
        published = str(item.get("publishDate") or "")[:10] or None
        metadata = {
            "股票代码": code, "股票简称": name,
            "机构": str(item.get("orgSName") or item.get("orgName") or "").strip(),
            "东财评级": str(item.get("emRatingName") or "").strip(),
            "行业": str(item.get("indvInduName") or "").strip(), "日期": published,
        }
        body = "；".join(f"{key}：{value}" for key, value in metadata.items() if value)
        info_code = str(item.get("infoCode") or "").strip()
        documents.append({
            "document_type": "broker_research",
            "title": str(item.get("title") or "未命名研究资料").strip(),
            "body": f"（事实）{body}",
            "source_url": f"https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf" if info_code else None,
            "source_name": "东方财富研报", "published_at": published,
            "observed_at": observed, "metadata": metadata,
        })
    return documents


def _stop_requested(mode: str) -> bool:
    with closing(connect()) as conn:
        row = conn.execute("SELECT stop_requested FROM report_sync_jobs WHERE job_key=?", (mode,)).fetchone()
    return bool(row and row[0])


def _set_job_status(mode: str, status: str, error: str | None = None) -> None:
    completed = _now() if status == "SUCCESS" else None
    with closing(connect()) as conn:
        conn.execute(
            """UPDATE report_sync_jobs SET status=?,completed_at=COALESCE(?,completed_at),
               updated_at=?,last_error=? WHERE job_key=?""",
            (status, completed, _now(), error, mode),
        )
        conn.commit()


def _run_bulk_sync(mode: str) -> None:
    global _active_thread
    try:
        while True:
            with closing(connect()) as conn:
                job = dict(conn.execute("SELECT * FROM report_sync_jobs WHERE job_key=?", (mode,)).fetchone())
            if _stop_requested(mode):
                _set_job_status(mode, "PAUSED")
                return
            page = int(job["next_page"])
            try:
                payload = _request_page_with_retry(job["begin_date"], job["end_date"], page)
                total_pages = int(payload.get("TotalPage") or 0)
                total_hits = int(payload.get("hits") or payload.get("size") or 0)
                documents = _payload_to_documents(payload)
                result = persist_source_documents(
                    "akshare", "research_report_bulk", f"{mode}-{job['begin_date']}-{page:05d}",
                    documents, json.dumps(payload, ensure_ascii=False),
                )
                with closing(connect()) as conn:
                    conn.execute(
                        """UPDATE report_sync_jobs SET total_pages=?,total_hits=?,processed_pages=processed_pages+1,
                           fetched_rows=fetched_rows+?,stored_rows=stored_rows+?,next_page=?,updated_at=?,last_error=NULL
                           WHERE job_key=?""",
                        (total_pages, total_hits, len(documents), result["rows"], page + 1, _now(), mode),
                    )
                    conn.commit()
                if total_pages == 0 or page >= total_pages:
                    _set_job_status(mode, "SUCCESS")
                    return
                time.sleep(_rate_delay())
            except Exception as exc:
                with closing(connect()) as conn:
                    conn.execute(
                        """UPDATE report_sync_jobs SET status='FAILED',error_count=error_count+1,
                           updated_at=?,last_error=? WHERE job_key=?""",
                        (_now(), str(exc), mode),
                    )
                    conn.commit()
                return
    finally:
        with _thread_guard:
            _active_thread = None


def trigger_bulk_report_sync(mode: str = "full", restart: bool = False) -> dict:
    global _active_thread
    with _thread_guard:
        if _active_thread is not None and _active_thread.is_alive():
            return {**bulk_report_status(), "started": False, "message": "已有同步任务正在运行"}
        job, should_start = _prepare_job(mode, restart)
        if not should_start:
            return {**bulk_report_status(), "started": False, "message": "该任务已经完成"}
        _active_thread = threading.Thread(
            target=_run_bulk_sync, args=(mode,), name=f"argus-report-{mode}", daemon=True,
        )
        _active_thread.start()
    return {**bulk_report_status(), "started": True, "job": job}


def pause_bulk_report_sync() -> dict:
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT job_key FROM report_sync_jobs
               WHERE mode IN ('full','incremental') AND status='RUNNING'
               ORDER BY updated_at DESC LIMIT 1"""
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE report_sync_jobs SET status='PAUSING',stop_requested=1,updated_at=? WHERE job_key=?",
                (_now(), row["job_key"]),
            )
            conn.commit()
    return bulk_report_status()


def recover_interrupted_report_sync() -> list[str]:
    with closing(connect()) as conn:
        _ensure_jobs(conn)
        rows = conn.execute(
            """SELECT job_key FROM report_sync_jobs
               WHERE mode IN ('full','incremental') AND status IN ('RUNNING','PAUSING')"""
        ).fetchall()
        keys = [row["job_key"] for row in rows]
        if keys:
            conn.execute(
                """UPDATE report_sync_jobs SET status='INTERRUPTED',stop_requested=0,updated_at=?,
                   last_error='本地服务重启，正在自动续跑'
                   WHERE mode IN ('full','incremental') AND status IN ('RUNNING','PAUSING')""",
                (_now(),),
            )
        conn.commit()
    return keys


def trigger_daily_increment_if_due() -> dict:
    status = bulk_report_status()
    full = status["jobs"]["full"]
    incremental = status["jobs"]["incremental"]
    if full["status"] != "SUCCESS":
        return {**status, "started": False, "message": "等待首次全量同步完成"}
    if incremental["status"] == "SUCCESS" and \
            _timestamp_local_date(incremental.get("completed_at")) == date.today():
        return {**status, "started": False, "message": "今日增量已经完成"}
    return trigger_bulk_report_sync("incremental")
