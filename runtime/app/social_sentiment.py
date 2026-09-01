"""Multi-source public sentiment collection with explicit coverage health."""

from __future__ import annotations

import json
import os
import time
from contextlib import closing
from datetime import date, datetime, timezone
from typing import Iterable
from urllib.parse import quote
from urllib.request import Request, urlopen

from .db import connect, initialize
from .persistence import persist_source_documents

SOURCE_WEIGHTS = {
    "akshare_eastmoney_news": 0.85,
    "eastmoney_participation": 0.65,
    "bilibili_public": 0.55,
    "x_official": 0.55,
}


class _QuietYtDlpLogger:
    def debug(self, _message):
        pass

    def warning(self, _message):
        pass

    def error(self, _message):
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _score_text(text: str) -> tuple[float, int, int]:
    from .continuous_learning import _news_score
    return _news_score(text)


def _symbol_names(symbols: Iterable[str]) -> dict[str, str]:
    symbols = list(dict.fromkeys(str(symbol) for symbol in symbols))
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with closing(connect()) as conn:
        rows = conn.execute(
            f"""SELECT symbol,name FROM a_share_universe_assets WHERE symbol IN ({placeholders})
                 UNION SELECT symbol,name FROM assets WHERE symbol IN ({placeholders})""",
            (*symbols, *symbols),
        ).fetchall()
    result = {str(row["symbol"]): str(row["name"]) for row in rows}
    return {symbol: result.get(symbol, symbol) for symbol in symbols}


def _health(source_code: str, source_kind: str, status: str, started: float,
            document_count: int = 0, error: str | None = None,
            coverage: dict | None = None) -> None:
    stamp = _now()
    latency = (time.perf_counter() - started) * 1000.0
    with closing(connect()) as conn:
        conn.execute(
            """INSERT INTO sentiment_source_health
               (source_code,source_kind,status,last_attempt_at,last_success_at,
                document_count,latency_ms,error,coverage_json)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_code) DO UPDATE SET source_kind=excluded.source_kind,
                 status=excluded.status,last_attempt_at=excluded.last_attempt_at,
                 last_success_at=CASE WHEN excluded.status='HEALTHY'
                                      THEN excluded.last_success_at ELSE last_success_at END,
                 document_count=excluded.document_count,latency_ms=excluded.latency_ms,
                 error=excluded.error,coverage_json=excluded.coverage_json""",
            (source_code, source_kind, status, stamp, stamp if status == "HEALTHY" else None,
             document_count, latency, error, _dump(coverage or {})),
        )
        conn.commit()


def _fetch_news(symbol: str) -> tuple[list[dict], str]:
    from .continuous_learning import _fetch_eastmoney_news
    return _fetch_eastmoney_news(symbol, page_size=30)


def _fetch_participation(symbol: str, target_date: date) -> tuple[list[dict], str]:
    import akshare as ak
    frame = ak.stock_comment_detail_scrd_desire_em(symbol=symbol)
    records = []
    for row in frame.to_dict(orient="records"):
        day = str(row.get("交易日期") or "")[:10]
        if day and day <= target_date.isoformat():
            normalized = {}
            for key, value in row.items():
                if value != value:
                    normalized[key] = None
                elif isinstance(value, (date, datetime)):
                    normalized[key] = value.isoformat()
                elif hasattr(value, "item"):
                    normalized[key] = value.item()
                else:
                    normalized[key] = value
            records.append(normalized)
    records.sort(key=lambda item: str(item.get("交易日期")))
    selected = records[-1:] if records else []
    documents = []
    for row in selected:
        participation = float(row.get("参与意愿") or 50.0)
        change = float(row.get("参与意愿变化") or 0.0)
        documents.append({
            "document_type": "investor_participation_index",
            "title": f"{symbol} 投资者参与意愿 {participation:.2f}",
            "body": f"参与意愿 {participation:.2f}；当日变化 {change:.2f}。这是聚合热度指标，不是投资者帖子。",
            "source_url": f"https://data.eastmoney.com/stockcomment/stock/{symbol}.html",
            "source_name": "东方财富千股千评",
            "published_at": str(row.get("交易日期")), "observed_at": _now(),
            "metadata": {"symbol": symbol, "participation": participation,
                         "participation_change": change, "aggregate_index": True},
        })
    return documents, _dump(records)


def _fetch_bilibili(name: str, symbol: str, max_results: int = 3) -> tuple[list[dict], str]:
    import yt_dlp
    max_results = max(1, min(int(max_results), 5))
    target = f"bilisearch{max_results}:{name} 股票"
    logger = _QuietYtDlpLogger()
    search_options = {"skip_download": True, "quiet": True, "no_warnings": True,
                      "playlistend": max_results, "extract_flat": "in_playlist",
                      "socket_timeout": 15, "retries": 1, "logger": logger}
    with yt_dlp.YoutubeDL(search_options) as client:
        search = client.extract_info(target, download=False)
    entries = search.get("entries") or []
    documents, raw_items, item_errors = [], [], []
    detail_options = {"skip_download": True, "quiet": True, "no_warnings": True,
                      "noplaylist": True, "socket_timeout": 15, "retries": 1,
                      "logger": logger}
    for entry in entries[:max_results]:
        if not entry:
            continue
        url = entry.get("webpage_url") or entry.get("url")
        try:
            with yt_dlp.YoutubeDL(detail_options) as client:
                item = client.extract_info(url, download=False)
        except Exception as exc:
            item_errors.append({"url": url, "error": repr(exc)[:500]})
            continue
        compact = {key: item.get(key) for key in (
            "id", "title", "description", "timestamp", "uploader", "duration",
            "view_count", "like_count", "comment_count", "tags", "webpage_url")}
        raw_items.append(compact)
        documents.append({
            "document_type": "social_video_metadata",
            "title": str(item.get("title") or item.get("id") or "B站视频"),
            "body": str(item.get("description") or ""),
            "source_url": item.get("webpage_url") or f"https://www.bilibili.com/video/{item.get('id')}",
            "source_name": "哔哩哔哩公开内容", "published_at": item.get("timestamp"),
            "observed_at": _now(),
            "metadata": {**compact, "symbol": symbol, "metadata_only": True},
        })
    return documents, _dump({"query": target, "entries": raw_items,
                             "item_errors": item_errors})


def _fetch_x(name: str, symbol: str, max_results: int = 10) -> tuple[list[dict], str]:
    token = os.environ.get("ARGUS_X_BEARER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ARGUS_X_BEARER_TOKEN is not configured")
    query = f'("{name}" OR {symbol}) lang:zh -is:retweet'
    url = ("https://api.x.com/2/tweets/search/recent?query=" + quote(query)
           + f"&max_results={max(10, min(max_results, 100))}"
             "&tweet.fields=created_at,lang,public_metrics,author_id")
    request = Request(url, headers={"Authorization": f"Bearer {token}",
                                    "User-Agent": "ArgusLocalResearch/1.0"})
    with urlopen(request, timeout=30) as response:
        raw = response.read()
    payload = json.loads(raw)
    documents = [{
        "document_type": "social_post", "title": f"X Post {item['id']}",
        "body": item.get("text", ""),
        "source_url": f"https://x.com/i/web/status/{item['id']}",
        "source_name": "X official API", "published_at": item.get("created_at"),
        "observed_at": _now(),
        "metadata": {"symbol": symbol, "id": item["id"],
                     "author_id": item.get("author_id"), "lang": item.get("lang"),
                     "public_metrics": item.get("public_metrics", {})},
    } for item in payload.get("data", [])]
    return documents, raw.decode("utf-8")


def _component(documents: list[dict], source_code: str) -> dict:
    scores, positives, negatives = [], 0, 0
    for item in documents:
        metadata = item.get("metadata") or {}
        if metadata.get("aggregate_index"):
            participation = float(metadata.get("participation", 50.0))
            change = float(metadata.get("participation_change", 0.0))
            scores.append(max(-1.0, min(1.0, (participation - 50.0) / 35.0 + change / 100.0)))
            continue
        score, positive, negative = _score_text(f"{item.get('title', '')} {item.get('body', '')}")
        scores.append(score)
        positives += positive
        negatives += negative
    raw_score = sum(scores) / len(scores) if scores else 0.0
    confidence = min(1.0, len(documents) / 8.0) * SOURCE_WEIGHTS[source_code]
    return {"score": raw_score, "confidence": confidence,
            "documents": len(documents), "positive_count": positives,
            "negative_count": negatives, "weight": SOURCE_WEIGHTS[source_code]}


def _published_on_or_before(item: dict, target_date: date) -> bool:
    value = item.get("published_at")
    if value in (None, ""):
        return True
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).date() <= target_date
    try:
        return date.fromisoformat(str(value)[:10]) <= target_date
    except ValueError:
        return True


def collect_multisource_sentiment(symbols: Iterable[str], as_of: date | None = None,
                                  max_social_symbols: int = 3) -> dict:
    initialize()
    target_date = as_of or datetime.now().date()
    symbols = list(dict.fromkeys(str(symbol) for symbol in symbols))
    names = _symbol_names(symbols)
    components = {symbol: {} for symbol in symbols}
    errors = []

    news_started = time.perf_counter()
    news_count = 0
    for symbol in symbols:
        try:
            documents, raw = _fetch_news(symbol)
            documents = [item for item in documents if _published_on_or_before(item, target_date)]
            persist_source_documents("akshare", "news", symbol, documents, raw)
            components[symbol]["akshare_eastmoney_news"] = _component(
                documents, "akshare_eastmoney_news")
            news_count += len(documents)
        except Exception as exc:
            errors.append({"source": "akshare_eastmoney_news", "symbol": symbol,
                           "error": repr(exc)})
    _health("akshare_eastmoney_news", "news", "HEALTHY" if news_count else "DEGRADED",
            news_started, news_count, None if news_count else "no documents",
            {"symbols_requested": len(symbols)})

    participation_started = time.perf_counter()
    participation_count = 0
    for symbol in symbols:
        try:
            documents, raw = _fetch_participation(symbol, target_date)
            persist_source_documents("eastmoney", "social_index", symbol, documents, raw)
            components[symbol]["eastmoney_participation"] = _component(
                documents, "eastmoney_participation")
            participation_count += len(documents)
        except Exception as exc:
            errors.append({"source": "eastmoney_participation", "symbol": symbol,
                           "error": repr(exc)})
    _health("eastmoney_participation", "investor_community_index",
            "HEALTHY" if participation_count else "DEGRADED", participation_started,
            participation_count, None if participation_count else "no index rows",
            {"symbols_requested": len(symbols), "aggregate_not_posts": True})

    bilibili_started = time.perf_counter()
    bilibili_count = 0
    social_symbols = symbols[:max(0, int(max_social_symbols))]
    for symbol in social_symbols:
        try:
            documents, raw = _fetch_bilibili(names[symbol], symbol)
            documents = [item for item in documents if _published_on_or_before(item, target_date)]
            persist_source_documents("bilibili", "social", symbol, documents, raw)
            components[symbol]["bilibili_public"] = _component(documents, "bilibili_public")
            bilibili_count += len(documents)
        except Exception as exc:
            errors.append({"source": "bilibili_public", "symbol": symbol, "error": repr(exc)})
    _health("bilibili_public", "social_video_metadata",
            "HEALTHY" if bilibili_count else "DEGRADED", bilibili_started,
            bilibili_count, None if bilibili_count else "no public metadata",
            {"symbols_requested": len(social_symbols), "metadata_only": True})

    x_started = time.perf_counter()
    x_count = 0
    if os.environ.get("ARGUS_X_BEARER_TOKEN"):
        for symbol in social_symbols:
            try:
                documents, raw = _fetch_x(names[symbol], symbol)
                documents = [item for item in documents if _published_on_or_before(item, target_date)]
                persist_source_documents("x_official", "social", symbol, documents, raw)
                components[symbol]["x_official"] = _component(documents, "x_official")
                x_count += len(documents)
            except Exception as exc:
                errors.append({"source": "x_official", "symbol": symbol, "error": repr(exc)})
        x_status = "HEALTHY" if x_count else "DEGRADED"
        x_error = None if x_count else "official API returned no posts"
    else:
        x_status, x_error = "UNCONFIGURED", "official Bearer Token required"
    _health("x_official", "social_post", x_status, x_started, x_count, x_error,
            {"official_api_only": True, "symbols_requested": len(social_symbols)})

    results, stamp = [], _now()
    with closing(connect()) as conn:
        for symbol in symbols:
            source_items = components[symbol]
            weighted_denominator = sum(item["confidence"] for item in source_items.values())
            score = (sum(item["score"] * item["confidence"] for item in source_items.values())
                     / weighted_denominator if weighted_denominator else 0.0)
            document_count = sum(item["documents"] for item in source_items.values())
            positives = sum(item["positive_count"] for item in source_items.values())
            negatives = sum(item["negative_count"] for item in source_items.values())
            source_diversity = sum(item["documents"] > 0 for item in source_items.values())
            confidence = min(1.0, weighted_denominator / 1.4) * min(1.0, source_diversity / 2.0)
            breakdown = {code: item for code, item in source_items.items()}
            conn.execute(
                """INSERT INTO sentiment_daily
                   (symbol,trade_date,score,confidence,document_count,positive_count,
                    negative_count,source_breakdown_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol,trade_date) DO UPDATE SET score=excluded.score,
                     confidence=excluded.confidence,document_count=excluded.document_count,
                     positive_count=excluded.positive_count,negative_count=excluded.negative_count,
                     source_breakdown_json=excluded.source_breakdown_json,
                     updated_at=excluded.updated_at""",
                (symbol, target_date.isoformat(), score, confidence, document_count,
                 positives, negatives, _dump(breakdown), stamp, stamp),
            )
            results.append({"symbol": symbol, "name": names[symbol], "score": score,
                            "confidence": confidence, "documents": document_count,
                            "source_count": source_diversity, "source_breakdown": breakdown})
        conn.commit()
    return {"date": target_date.isoformat(), "symbols": results,
            "documents": sum(item["documents"] for item in results),
            "errors": errors, "coverage": sentiment_coverage_payload()}


def sentiment_coverage_payload() -> dict:
    initialize()
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM sentiment_source_health ORDER BY source_kind,source_code"
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["coverage"] = json.loads(item.pop("coverage_json"))
        except (TypeError, ValueError):
            item["coverage"] = {}
        items.append(item)
    return {"sources": items, "healthy": sum(item["status"] == "HEALTHY" for item in items),
            "configured": sum(item["status"] != "UNCONFIGURED" for item in items),
            "full_internet_claim": False,
            "note": "多来源公开舆情覆盖；受权限或平台限制的来源会明确降级。"}
