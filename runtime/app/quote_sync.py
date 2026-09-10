"""Status of the minute quote cycle, independent of daily/history jobs."""
import json
import os
from datetime import datetime, timezone

from .db import DATA_LAKE


def read_status():
    try:
        return json.loads((DATA_LAKE / "cache" / "quote_sync.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_status(**fields):
    data = {**read_status(), **fields}
    path = DATA_LAKE / "cache" / "quote_sync.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
    return data


def status_payload(session_open, enabled, now=None):
    data = read_status()
    now = now or datetime.now(timezone.utc)
    try:
        age = (now - datetime.fromisoformat(data["last_success_at"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        age = None
    status = data.get("status", "WAITING")
    if status == "RUNNING" and data.get("last_result_status") in {"FAILED", "PARTIAL"}:
        status = data["last_result_status"]
    if not enabled:
        status = "DISABLED"
    elif age is None or age > 120:
        status = "STALE" if status not in {"FAILED", "PARTIAL"} else status
    elif status == "RUNNING" and age is not None and age <= 120:
        status = "HEALTHY"
    elif not session_open and status in {"HEALTHY", "RUNNING", "WAITING"}:
        status = "CLOSED"
    return {**data, "status": status, "success_age_seconds": age, "interval_seconds": 60,
            "provider": "tdx_public"}
