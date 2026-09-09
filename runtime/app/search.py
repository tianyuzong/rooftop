"""Page-aware exact and local semantic search."""

from __future__ import annotations

import hashlib
import threading
from array import array
from contextlib import closing
from datetime import datetime, timezone

from .analytics import policy_catalog
from .db import DATA_LAKE, connect, initialize

MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_DIM = 512
REPORT_DOCUMENT_TYPES = {"fund_announcement", "broker_research", "sec_filing_index", "macro_calendar"}
_engine = None
_engine_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_model_snapshot(model_cache):
    repository = model_cache / ("models--" + MODEL_NAME.replace("/", "--"))
    snapshots = repository / "snapshots"
    main_ref = repository / "refs" / "main"
    if main_ref.is_file():
        candidate = snapshots / main_ref.read_text(encoding="utf-8").strip()
        if (candidate / "config.json").is_file():
            return candidate
    if snapshots.is_dir():
        for candidate in sorted(snapshots.iterdir(), reverse=True):
            if candidate.is_dir() and (candidate / "config.json").is_file():
                return candidate
    return None


def collect_documents(conn, include_source_documents: bool = True) -> list[dict]:
    docs = []
    for row in conn.execute("SELECT id,title,statement,falsification_rule,status FROM hypotheses WHERE status!='ARCHIVED'"):
        docs.append({"doc_key": f"hypothesis:{row['id']}", "page": "research", "title": row["title"],
                     "body": f"{row['statement']} 状态：{row['status']}。反证条件：{row['falsification_rule']}",
                     "source_ref": f"trad://hypotheses/{row['id']}"})
    for row in conn.execute("""SELECT e.id,e.claim,e.label,e.status,e.independent_check,
                                      e.observed_at,e.captured_at,s.name,s.url
                               FROM evidence e LEFT JOIN sources s ON s.id=e.source_id"""):
        docs.append({"doc_key": f"evidence:{row['id']}", "page": "research", "title": row["claim"],
                     "body": f"{row['label']}；{row['status']}；{row['independent_check']}；来源：{row['name'] or '未知'}",
                     "source_ref": row["url"] or f"trad://evidence/{row['id']}",
                     "source_name": row["name"] or "本地证据记录",
                     "published_at": row["observed_at"] or row["captured_at"]})
    for row in conn.execute("SELECT id,title,report_type,body FROM reports"):
        docs.append({"doc_key": f"report:{row['id']}", "page": "reports", "title": row["title"],
                     "body": f"{row['report_type']} {row['body']}", "source_ref": f"trad://reports/{row['id']}"})
    if include_source_documents:
        for row in conn.execute(
            """SELECT id,title,document_type,body,source_url,source_name,
                      published_at,captured_at FROM source_documents"""
        ):
            page = "reports" if row["document_type"] in REPORT_DOCUMENT_TYPES else "research"
            docs.append({"doc_key": f"source-document:{row['id']}", "page": page, "title": row["title"],
                         "body": f"{row['document_type']} {row['body']}",
                         "source_ref": row["source_url"] or f"trad://source-documents/{row['id']}",
                         "source_name": row["source_name"] or "公开资料",
                         "published_at": row["published_at"] or row["captured_at"],
                         "document_type": row["document_type"]})
    asset_symbols = set()
    for row in conn.execute("SELECT symbol,name,market,asset_type,data_status FROM assets"):
        asset_symbols.add(row["symbol"])
        docs.append({"doc_key": f"asset:{row['symbol']}", "page": "research", "title": f"{row['name']} {row['symbol']}",
                     "body": f"{row['market']} {row['asset_type']} 数据状态 {row['data_status']}",
                     "source_ref": f"trad://assets/{row['symbol']}"})
    for row in conn.execute("SELECT symbol,name,compare_count,last_compared_at FROM comparison_watchlist"):
        if row["symbol"] in asset_symbols:
            continue
        docs.append({"doc_key": f"comparison-watchlist:{row['symbol']}", "page": "research",
                     "title": f"{row['name']} {row['symbol']}",
                     "body": f"股票对比历史；累计对比 {row['compare_count']} 次；最近对比 {row['last_compared_at']}",
                     "source_ref": f"trad://comparison-watchlist/{row['symbol']}",
                     "source_name": "本地股票档案", "published_at": row["last_compared_at"]})
    for index, policy in enumerate(policy_catalog()):
        docs.append({"doc_key": f"risk-policy:{index}", "page": "strategy",
                     "title": f"{policy['market']} {policy['asset_type']} 风险纪律",
                     "body": " ".join(f"{key}={value}" for key, value in policy.items()),
                     "source_ref": f"trad://risk-policies/{index}"})
    for row in conn.execute(
        """SELECT s.id,s.name,s.category,s.description,s.status,r.stop_loss_pct,r.take_profit_pct,
                  r.trailing_stop_pct,r.max_position_pct,r.max_drawdown_pct,r.liquidity_rule
           FROM strategies s JOIN strategy_risk_policies r ON r.strategy_id=s.id"""
    ):
        docs.append({"doc_key": f"strategy:{row['id']}", "page": "strategy", "title": row["name"],
                     "body": (f"{row['category']}；{row['description']}；状态 {row['status']}；"
                              f"止损 {row['stop_loss_pct']:.1%}；止盈 {row['take_profit_pct']:.1%}；"
                              f"移动止损 {row['trailing_stop_pct']:.1%}；仓位上限 {row['max_position_pct']:.1%}；"
                              f"回撤上限 {row['max_drawdown_pct']:.1%}；{row['liquidity_rule']}"),
                     "source_ref": f"trad://strategies/{row['id']}"})
    for row in conn.execute("SELECT id,name,family,expression,description,status FROM factors"):
        docs.append({"doc_key": f"factor:{row['id']}", "page": "strategy", "title": row["name"],
                     "body": f"{row['family']}；{row['expression']}；{row['description']}；状态 {row['status']}",
                     "source_ref": f"trad://factors/{row['id']}"})
    return docs


def _snippet(body: str, query: str, length: int = 180) -> str:
    lower = body.lower()
    index = lower.find(query.lower())
    start = max(0, index - 50) if index >= 0 else 0
    text = body[start:start + length]
    return ("…" if start else "") + text + ("…" if start + length < len(body) else "")


def _exact_source_documents(conn, query: str, page: str | None, limit: int) -> list[dict]:
    if page == "strategy":
        return []
    report_types = tuple(sorted(REPORT_DOCUMENT_TYPES))
    placeholders = ",".join("?" for _ in report_types)
    conditions = ["(instr(lower(title),lower(?))>0 OR instr(lower(body),lower(?))>0)"]
    params: list[object] = [query, query]
    if page == "reports":
        conditions.append(f"document_type IN ({placeholders})")
        params.extend(report_types)
    elif page == "research":
        conditions.append(f"document_type NOT IN ({placeholders})")
        params.extend(report_types)
    params.extend([query, query, max(80, limit * 4)])
    rows = conn.execute(
        f"""SELECT id,title,document_type,body,source_url,source_name,published_at,captured_at
            FROM source_documents
            WHERE {' AND '.join(conditions)}
            ORDER BY CASE WHEN lower(title)=lower(?) THEN 0
                          WHEN instr(lower(title),lower(?))>0 THEN 1 ELSE 2 END,
                     COALESCE(published_at,captured_at) DESC LIMIT ?""",
        params,
    ).fetchall()
    documents = []
    for row in rows:
        doc_page = "reports" if row["document_type"] in REPORT_DOCUMENT_TYPES else "research"
        documents.append({
            "doc_key": f"source-document:{row['id']}", "page": doc_page, "title": row["title"],
            "body": f"{row['document_type']} {row['body']}",
            "source_ref": row["source_url"] or f"trad://source-documents/{row['id']}",
            "source_name": row["source_name"] or "公开资料",
            "published_at": row["published_at"] or row["captured_at"],
            "document_type": row["document_type"],
        })
    return documents


def exact_search(query: str, page: str | None = None, limit: int = 20) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    with closing(connect()) as conn:
        documents = collect_documents(conn, include_source_documents=False)
        documents.extend(_exact_source_documents(conn, query, page, limit))
    matches = []
    needle = query.casefold()
    for doc in documents:
        if page and doc["page"] != page:
            continue
        title, body = doc["title"].casefold(), doc["body"].casefold()
        if needle not in title and needle not in body:
            continue
        score = 1.0 if title == needle else (.92 if needle in title else .75)
        matches.append({**doc, "score": score, "snippet": _snippet(doc["body"], query), "match_mode": "exact"})
    return sorted(
        matches,
        key=lambda item: (item.get("published_at") or "", item["score"], item["title"]),
        reverse=True,
    )[:limit]


class EmbeddingEngine:
    def __init__(self):
        import torch
        from sentence_transformers import SentenceTransformer

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        model_cache = DATA_LAKE / "models"
        model_cache.mkdir(parents=True, exist_ok=True)
        local_snapshot = _local_model_snapshot(model_cache)
        self.model = SentenceTransformer(
            str(local_snapshot) if local_snapshot else MODEL_NAME,
            device=self.device,
            cache_folder=str(model_cache),
            local_files_only=local_snapshot is not None,
        )

    def encode_documents(self, texts: list[str]):
        vectors = self.model.encode(texts, batch_size=16, normalize_embeddings=True,
                                    convert_to_numpy=True, show_progress_bar=False)
        return self._truncate(vectors)

    def encode_query(self, text: str):
        instruction = "Instruct: Retrieve relevant passages from a local financial research database\nQuery:" + text
        vectors = self.model.encode([instruction], normalize_embeddings=True,
                                    convert_to_numpy=True, show_progress_bar=False)
        return self._truncate(vectors)[0]

    @staticmethod
    def _truncate(vectors):
        import numpy as np
        vectors = vectors[:, :EMBEDDING_DIM].astype("float32")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-12)


def get_engine() -> EmbeddingEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = EmbeddingEngine()
    return _engine


def semantic_status() -> dict:
    try:
        import torch
        import sentence_transformers  # noqa: F401
        available = True
        device = "cuda" if torch.cuda.is_available() else "cpu"
        gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    except Exception:
        available, device, gpu = False, "unavailable", None
    with connect() as conn:
        indexed = conn.execute("SELECT COUNT(*) FROM semantic_documents WHERE embedding IS NOT NULL AND model_name=?",
                               (MODEL_NAME,)).fetchone()[0]
    return {"available": available, "loaded": _engine is not None, "model": MODEL_NAME,
            "dimension": EMBEDDING_DIM, "device": device, "gpu": gpu, "indexed_documents": indexed}


def sync_semantic_index(include_source_documents: bool = False) -> int:
    initialize()
    engine = get_engine()
    with connect() as conn:
        # Interactive searches index the compact research catalog. The full
        # report archive is searched directly in SQL instead of blocking one
        # browser request while embedding more than 100,000 documents.
        docs = collect_documents(conn, include_source_documents=include_source_documents)
        pending = []
        for doc in docs:
            digest = hashlib.sha256(
                (doc["page"] + "\n" + doc["title"] + "\n" + doc["body"] + "\n" + (doc["source_ref"] or "")).encode("utf-8")
            ).hexdigest()
            current = conn.execute("SELECT content_hash,model_name,embedding_dim FROM semantic_documents WHERE doc_key=?",
                                   (doc["doc_key"],)).fetchone()
            doc["content_hash"] = digest
            if not current or current["content_hash"] != digest or current["model_name"] != MODEL_NAME or current["embedding_dim"] != EMBEDDING_DIM:
                pending.append(doc)
        if pending:
            vectors = engine.encode_documents([doc["title"] + "\n" + doc["body"] for doc in pending])
            for doc, vector in zip(pending, vectors):
                blob = array("f", vector.tolist()).tobytes()
                conn.execute(
                    """INSERT INTO semantic_documents(doc_key,page,title,body,source_ref,content_hash,embedding,embedding_dim,model_name,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(doc_key) DO UPDATE SET page=excluded.page,title=excluded.title,
                       body=excluded.body,source_ref=excluded.source_ref,content_hash=excluded.content_hash,
                       embedding=excluded.embedding,embedding_dim=excluded.embedding_dim,model_name=excluded.model_name,
                       updated_at=excluded.updated_at""",
                    (doc["doc_key"], doc["page"], doc["title"], doc["body"], doc["source_ref"], doc["content_hash"],
                     blob, EMBEDDING_DIM, MODEL_NAME, _now()),
                )
            conn.commit()
    return len(pending)


def semantic_search(query: str, page: str | None = None, limit: int = 20) -> list[dict]:
    if not query.strip():
        return []
    sync_semantic_index()
    vector = get_engine().encode_query(query)
    with connect() as conn:
        if page:
            rows = conn.execute("SELECT * FROM semantic_documents WHERE embedding IS NOT NULL AND model_name=? AND page=?",
                                (MODEL_NAME, page)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM semantic_documents WHERE embedding IS NOT NULL AND model_name=?",
                                (MODEL_NAME,)).fetchall()
    with connect() as conn:
        metadata = {
            item["doc_key"]: item
            for item in collect_documents(conn, include_source_documents=False)
        }
    scored = []
    for row in rows:
        stored = array("f")
        stored.frombytes(row["embedding"])
        score = sum(a * b for a, b in zip(vector, stored))
        details = metadata.get(row["doc_key"], {})
        scored.append({"doc_key": row["doc_key"], "page": row["page"], "title": row["title"],
                       "body": row["body"], "source_ref": row["source_ref"], "score": round(float(score), 6),
                       "snippet": _snippet(row["body"], query), "match_mode": "semantic",
                       "source_name": details.get("source_name"),
                       "published_at": details.get("published_at"),
                       "document_type": details.get("document_type")})
    return sorted(
        scored,
        key=lambda item: (item.get("published_at") or "", item["score"], item["title"]),
        reverse=True,
    )[:limit]
