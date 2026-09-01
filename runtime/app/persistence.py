"""Mandatory persistence gateways for externally acquired documents.

Production collectors must call these gateways after a successful external query.
The raw response is stored before normalized rows become visible to readers.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone
from typing import Iterable

from .db import DATA_LAKE, connect, ensure_data_lake, initialize


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)[:80] or "query"


def persist_source_documents(source_code: str, dataset: str, query_key: str,
                             documents: Iterable[dict], raw_payload: bytes | str) -> dict:
    """Persist one external document query into raw storage and the relational DB."""
    initialize()
    ensure_data_lake()
    captured = _now()
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    raw = raw_payload.encode("utf-8") if isinstance(raw_payload, str) else raw_payload
    raw_dir = DATA_LAKE / "raw" / ("news" if dataset == "news" else "documents") / _safe(source_code)
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{_safe(dataset)}_{_safe(query_key)}_{stamp}.json"
    raw_path.write_bytes(raw)
    items = list(documents)

    with closing(connect()) as conn:
        source = conn.execute("SELECT id FROM data_sources WHERE code=?", (source_code,)).fetchone()
        if not source:
            raise ValueError(f"unknown data source: {source_code}")
        source_id = int(source[0])
        ids = []
        for item in items:
            title = str(item.get("title") or "未命名文档").strip()
            body = str(item.get("body") or item.get("content") or "").strip()
            source_url = str(item.get("source_url") or item.get("url") or "").strip() or None
            stable = source_url or f"{title}\n{body}"
            content_hash = hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()
            doc_key = hashlib.sha256(f"{source_code}\n{stable}".encode("utf-8")).hexdigest()
            metadata = item.get("metadata") or {}
            conn.execute(
                """INSERT INTO source_documents
                   (doc_key,document_type,title,body,source_url,source_name,published_at,observed_at,
                    captured_at,raw_path,content_hash,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(doc_key) DO UPDATE SET document_type=excluded.document_type,
                     title=excluded.title,body=excluded.body,source_url=excluded.source_url,
                     source_name=excluded.source_name,published_at=excluded.published_at,
                     observed_at=excluded.observed_at,captured_at=excluded.captured_at,
                     raw_path=excluded.raw_path,content_hash=excluded.content_hash,
                     metadata_json=excluded.metadata_json""",
                (doc_key, str(item.get("document_type") or dataset), title, body, source_url,
                 str(item.get("source_name") or source_code), item.get("published_at"),
                 item.get("observed_at"), captured, str(raw_path), content_hash,
                 json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
            )
            document_id = int(conn.execute("SELECT id FROM source_documents WHERE doc_key=?", (doc_key,)).fetchone()[0])
            ids.append(document_id)
            latest_version = conn.execute(
                "SELECT content_hash FROM source_document_versions WHERE document_id=? ORDER BY id DESC LIMIT 1",
                (document_id,),
            ).fetchone()
            if not latest_version or latest_version["content_hash"] != content_hash:
                conn.execute(
                    """INSERT INTO source_document_versions
                       (document_id,captured_at,title,body,content_hash,metadata_json,raw_path)
                       VALUES(?,?,?,?,?,?,?)""",
                    (document_id, captured, title, body, content_hash,
                     json.dumps(metadata, ensure_ascii=False, sort_keys=True), str(raw_path)),
                )
        conn.execute(
            """INSERT INTO ingestion_runs
               (source_id,dataset,asset_symbol,started_at,finished_at,status,row_count,raw_path)
               VALUES(?,?,?,?,?,'SUCCESS',?,?)""",
            (source_id, dataset, query_key, captured, _now(), len(items), str(raw_path)),
        )
        conn.execute("UPDATE data_sources SET health_status='HEALTHY',last_success_at=?,last_error=NULL WHERE id=?",
                     (captured, source_id))
        conn.commit()
    return {"source": source_code, "dataset": dataset, "rows": len(ids), "ids": ids,
            "raw_path": str(raw_path), "storage": "local_sqlite"}
