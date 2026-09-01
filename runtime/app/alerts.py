"""SMTP alert outbox with safe dry-run defaults and deduplication."""

import os
import smtplib
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable

from .db import connect


def queue_alert(dedupe_key: str, subject: str, body: str, *, channel: str = "EMAIL",
                target: str | None = None, signal_id: int | None = None,
                subscription_id: int | None = None, metadata: dict | None = None,
                conn_factory: Callable = connect) -> bool:
    with closing(conn_factory()) as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO alert_outbox
               (dedupe_key,channel,subject,body,target,signal_id,subscription_id,
                metadata_json,status,attempts,created_at)
               VALUES(?,?,?,?,?,?,?,?,'PENDING',0,?)""",
            (dedupe_key, str(channel).upper(), subject, body, target, signal_id,
             subscription_id, json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cursor.rowcount == 1


def smtp_status() -> dict:
    transport = ("ARGUS_SMTP_HOST", "ARGUS_SMTP_USER", "ARGUS_SMTP_PASSWORD")
    return {"configured": all(os.environ.get(key) for key in transport),
            "default_target_configured": bool(os.environ.get("ARGUS_ALERT_TO")),
            "send_enabled": os.environ.get("ARGUS_EMAIL_SEND_ENABLED") == "1",
            "default": "dry_run", "provider": "SMTP", "order_execution": False}


def _retry(conn, row, error: Exception | str) -> None:
    attempts = int(row["attempts"] or 0) + 1
    terminal = attempts >= 5
    next_attempt = None if terminal else (
        datetime.now(timezone.utc) + timedelta(minutes=min(60, 2 ** attempts))
    ).isoformat()
    conn.execute(
        """UPDATE alert_outbox SET status=?,attempts=?,next_attempt_at=?,error=? WHERE id=?""",
        ("FAILED" if terminal else "RETRY", attempts, next_attempt, str(error)[:1000], row["id"]),
    )


def send_pending(limit: int = 20, conn_factory: Callable = connect) -> dict:
    status = smtp_status()
    if not status["configured"] or not status["send_enabled"]:
        return {"status": "DRY_RUN", "sent": 0, **status}
    now = datetime.now(timezone.utc).isoformat()
    with closing(conn_factory()) as conn:
        rows = conn.execute(
            """SELECT * FROM alert_outbox
               WHERE channel='EMAIL' AND status IN ('PENDING','RETRY')
                 AND (next_attempt_at IS NULL OR next_attempt_at<=?)
               ORDER BY id LIMIT ?""", (now, max(1, min(int(limit), 100))),
        ).fetchall()
        sent = 0
        try:
            with smtplib.SMTP_SSL(os.environ["ARGUS_SMTP_HOST"], int(os.environ.get("ARGUS_SMTP_PORT", "465"))) as client:
                client.login(os.environ["ARGUS_SMTP_USER"], os.environ["ARGUS_SMTP_PASSWORD"])
                for row in rows:
                    target = str(row["target"] or os.environ.get("ARGUS_ALERT_TO") or "").strip()
                    if not target:
                        _retry(conn, row, "missing email target")
                        continue
                    message = EmailMessage()
                    message["From"] = os.environ["ARGUS_SMTP_USER"]
                    message["To"] = target
                    message["Subject"] = row["subject"]
                    message.set_content(row["body"])
                    try:
                        client.send_message(message)
                        conn.execute(
                            """UPDATE alert_outbox SET status='SENT',sent_at=?,attempts=attempts+1,
                               next_attempt_at=NULL,error=NULL WHERE id=?""",
                            (datetime.now(timezone.utc).isoformat(), row["id"]),
                        )
                        sent += 1
                    except Exception as exc:
                        _retry(conn, row, exc)
            conn.commit()
            retrying = sum(1 for row in rows if row["id"] not in {
                item[0] for item in conn.execute(
                    "SELECT id FROM alert_outbox WHERE status='SENT' AND id IN (%s)" %
                    ",".join("?" for _ in rows), [item["id"] for item in rows]
                )
            }) if rows else 0
            return {"status": "SENT" if sent else "NO_DELIVERY", "sent": sent,
                    "processed": len(rows), "retrying_or_failed": retrying, **status}
        except Exception as exc:
            for row in rows:
                _retry(conn, row, exc)
            conn.commit()
            return {"status": "FAILED", "sent": sent, "processed": len(rows),
                    "error": repr(exc), **status}


def outbox_payload(limit: int = 50, conn_factory: Callable = connect) -> dict:
    with closing(conn_factory()) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM alert_outbox ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        )]
        counts = {row["status"]: row["count"] for row in conn.execute(
            "SELECT status,COUNT(*) count FROM alert_outbox GROUP BY status"
        )}
    for row in rows:
        try:
            row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            row["metadata"] = {}
    return {"rows": rows, "counts": counts, "smtp": smtp_status(),
            "research_only": True, "order_execution": False}
