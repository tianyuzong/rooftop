"""SMTP alert outbox with safe dry-run defaults and deduplication."""

import os
import smtplib
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Callable

from .db import connect
from .process_lock import ProcessLock
from .mail_settings import get_settings, public_settings, smtp_client


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
    return public_settings()


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
    with closing(conn_factory()) as conn:
        database_path = conn.execute("PRAGMA database_list").fetchone()[2]
    if not database_path:
        raise ValueError("mail delivery requires a persistent outbox")
    lock = ProcessLock(Path(database_path).with_suffix(".outbox.lock"))
    if not lock.acquire():
        return {"status": "BUSY", "sent": 0}
    try:
        return _send_pending_locked(limit, conn_factory)
    finally:
        lock.release()


def _send_pending_locked(limit: int, conn_factory: Callable) -> dict:
    status = smtp_status()
    if not status["configured"] or not status["send_enabled"]:
        return {"status": "DRY_RUN", "sent": 0, **status}
    settings = get_settings()
    now = datetime.now(timezone.utc).isoformat()
    with closing(conn_factory()) as conn:
        conn.execute("""UPDATE alert_outbox SET status='CANCELLED',error='订阅已停用'
            WHERE status IN ('PENDING','RETRY') AND subscription_id IS NOT NULL
            AND NOT EXISTS(SELECT 1 FROM signal_subscriptions s WHERE s.id=subscription_id AND s.enabled=1)""")
        conn.commit()
        rows = conn.execute(
            """SELECT * FROM alert_outbox
               WHERE channel='EMAIL' AND status IN ('PENDING','RETRY')
                 AND (next_attempt_at IS NULL OR next_attempt_at<=?)
               ORDER BY id LIMIT ?""", (now, max(1, min(int(limit), 100))),
        ).fetchall()
        if not rows:
            return {"status": "NO_DELIVERY", "sent": 0, "processed": 0, **status}
        sent = 0
        processed = set()
        try:
            with smtp_client(smtplib, settings) as client:
                client.login(settings['user'], settings['password'])
                for row in rows:
                    current = conn.execute("""SELECT o.status,COALESCE(s.enabled,1) enabled
                        FROM alert_outbox o LEFT JOIN signal_subscriptions s ON s.id=o.subscription_id
                        WHERE o.id=?""", (row['id'],)).fetchone()
                    if current['status'] not in ('PENDING','RETRY') or not current['enabled']:
                        processed.add(row['id'])
                        continue
                    target = str(row["target"] or settings['default_target'] or "").strip()
                    if not target:
                        _retry(conn, row, "missing email target")
                        conn.commit()
                        processed.add(row["id"])
                        continue
                    message = EmailMessage()
                    message["From"] = settings["user"]
                    message["To"] = target
                    message["Subject"] = row["subject"]
                    message['Date'] = formatdate(localtime=False)
                    message['Message-ID'] = make_msgid()
                    message.set_content(row["body"])
                    metadata = json.loads(row['metadata_json'] or '{}')
                    if metadata.get('html'):
                        message.add_alternative(metadata['html'], subtype='html')
                    try:
                        refused = client.send_message(message)
                        if isinstance(refused, dict) and refused:
                            raise smtplib.SMTPRecipientsRefused(refused)
                        conn.execute(
                            """UPDATE alert_outbox SET status='SENT',sent_at=?,attempts=attempts+1,
                               next_attempt_at=NULL,error=NULL WHERE id=?""",
                            (datetime.now(timezone.utc).isoformat(), row["id"]),
                        )
                        if row['subscription_id']:
                            conn.execute('UPDATE signal_subscriptions SET last_sent_at=? WHERE id=?', (datetime.now(timezone.utc).isoformat(),row['subscription_id']))
                        sent += 1
                    except Exception as exc:
                        _retry(conn, row, str(exc).replace(settings['password'], '[已隐藏]'))
                    conn.commit()
                    processed.add(row["id"])
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
                if row["id"] not in processed:
                    _retry(conn, row, str(exc).replace(settings['password'], '[已隐藏]'))
            conn.commit()
            return {"status": "SENT" if sent == len(rows) else "FAILED",
                    "sent": sent, "processed": len(rows),
                    "error": str(exc).replace(settings["password"], "[已隐藏]"), **status}


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


def confirm_received(message_id: int, conn_factory: Callable = connect) -> dict:
    with closing(conn_factory()) as conn:
        cursor = conn.execute("UPDATE alert_outbox SET received_at=? WHERE id=? AND status='SENT'", (datetime.now(timezone.utc).isoformat(), int(message_id)))
        if cursor.rowcount != 1:
            raise ValueError('只能确认已由发信服务器接受的邮件')
        conn.commit()
    return {'id':int(message_id), 'status':'RECEIVED'}


def retry_message(message_id: int, conn_factory: Callable = connect) -> dict:
    with closing(conn_factory()) as conn:
        cursor = conn.execute("""UPDATE alert_outbox SET status='PENDING',attempts=0,next_attempt_at=NULL,error=NULL
            WHERE id=? AND status IN ('FAILED','RETRY') AND (subscription_id IS NULL OR
            EXISTS(SELECT 1 FROM signal_subscriptions s WHERE s.id=subscription_id AND s.enabled=1))""", (int(message_id),))
        if cursor.rowcount != 1:
            raise ValueError('这封邮件无需重试，或其订阅已经停用')
        conn.commit()
    return send_pending(conn_factory=conn_factory)
