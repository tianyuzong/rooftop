"""Auditable A-share prediction, scoring, and model-evolution loop.

The loop is research-only. It records predictions before outcomes exist, scores
them after the close, and promotes only models that pass out-of-sample gates.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .db import ROOT, connect, initialize
from .persistence import persist_source_documents


SHANGHAI = ZoneInfo("Asia/Shanghai")
FEATURE_NAMES = (
    "bias", "mom_5", "mom_20", "ma_spread_5_20", "vol_20",
    "volume_z20", "sentiment", "market_mom_5",
)
INITIAL_COEFFICIENTS = {
    "bias": 0.0,
    "mom_5": 0.42,
    "mom_20": 0.22,
    "ma_spread_5_20": 0.28,
    "vol_20": -0.10,
    "volume_z20": 0.04,
    "sentiment": 0.24,
    "market_mom_5": 0.22,
}
POSITIVE_TERMS = (
    "增长", "增持", "回购", "中标", "突破", "创新高", "盈利", "扭亏", "上调",
    "超预期", "利好", "扩产", "签约", "获批", "改善", "分红", "景气", "领先",
)
NEGATIVE_TERMS = (
    "下滑", "减持", "亏损", "处罚", "调查", "诉讼", "违约", "爆雷", "下调",
    "不及预期", "利空", "停产", "终止", "风险", "退市", "质押", "裁员", "暴跌",
)
DEFAULT_UNIVERSE = ("600519", "000858", "300750")
RESEARCH_TURNOVER_COST = 0.0015
CYCLE_HEARTBEAT_SECONDS = 15
CYCLE_LEASE_TIMEOUT_SECONDS = 90

STAGE_LABELS = {
    "calendar": "刷新交易日历",
    "market_refresh": "刷新日线与分钟行情",
    "fundamentals": "刷新基本面与估值快照",
    "sentiment": "采集多源舆情",
    "settle_predictions": "结算历史预测",
    "online_model": "滚动评估在线模型",
    "next_predictions": "生成下一交易日预测",
    "deep_learning": "训练 Qwen3 数值适配器",
    "intraday_evolution": "进化 5 分钟策略",
    "code_evolution": "评测受控源码候选",
    "quant_portfolios": "刷新活动量化组合",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cycle_stage_keys(phase: str, inputs: dict) -> list[str]:
    stages = []
    if inputs.get("refresh_calendar", True):
        stages.append("calendar")
    if inputs.get("refresh_data", phase != "BACKFILL"):
        stages.append("market_refresh")
    if phase in {"POST_CLOSE", "BACKFILL"} and inputs.get("refresh_fundamentals", True):
        stages.append("fundamentals")
    if inputs.get("collect_sentiment", True):
        stages.append("sentiment")
    stages.extend(("settle_predictions", "online_model", "next_predictions"))
    if phase in {"POST_CLOSE", "BACKFILL"} and inputs.get("train_deep_model", True):
        stages.append("deep_learning")
    if phase in {"POST_CLOSE", "BACKFILL"} and inputs.get("evolve_intraday", True):
        stages.append("intraday_evolution")
    if phase == "POST_CLOSE" and inputs.get("evolve_source_code", True):
        stages.append("code_evolution")
    if phase == "POST_CLOSE" and inputs.get("refresh_quant_portfolios", True):
        stages.append("quant_portfolios")
    return stages


def _initial_cycle_progress(stage_keys: list[str]) -> dict:
    stamp = _now()
    return {
        "status": "QUEUED",
        "current_stage": None,
        "current_label": "等待后台执行",
        "completed": 0,
        "total": len(stage_keys),
        "percent": 0.0,
        "updated_at": stamp,
        "stages": [
            {"key": key, "label": STAGE_LABELS[key], "status": "PENDING"}
            for key in stage_keys
        ],
    }


def _timestamp_age_seconds(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, ((now or datetime.now(timezone.utc)) - parsed).total_seconds())
    except (TypeError, ValueError):
        return None


class _CycleProgress:
    TERMINAL_STAGE_STATUSES = {"SUCCEEDED", "WARNING", "FAILED", "SKIPPED"}

    def __init__(self, cycle_id: int, worker_token: str, progress: dict):
        self.cycle_id = cycle_id
        self.worker_token = worker_token
        self.progress = progress
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"argus-learning-heartbeat-{cycle_id}",
            daemon=True,
        )

    def start_heartbeat(self) -> None:
        self._thread.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(CYCLE_HEARTBEAT_SECONDS):
            try:
                with closing(connect()) as conn:
                    conn.execute(
                        """UPDATE harness_learning_cycles SET heartbeat_at=?
                           WHERE id=? AND worker_token=? AND status='RUNNING'""",
                        (_now(), self.cycle_id, self.worker_token),
                    )
                    conn.commit()
            except Exception:
                # Progress writes remain the authoritative state; a transient
                # heartbeat failure must not abort a research cycle.
                continue

    def _persist(self) -> None:
        stamp = _now()
        self.progress["updated_at"] = stamp
        completed = sum(
            item["status"] in self.TERMINAL_STAGE_STATUSES
            for item in self.progress["stages"]
        )
        self.progress["completed"] = completed
        total = max(1, int(self.progress.get("total", 0)))
        self.progress["percent"] = round(min(100.0, completed / total * 100.0), 2)
        with closing(connect()) as conn:
            conn.execute(
                """UPDATE harness_learning_cycles SET progress_json=?,heartbeat_at=?
                   WHERE id=? AND worker_token=?""",
                (_dump(self.progress), stamp, self.cycle_id, self.worker_token),
            )
            conn.commit()

    def start(self, stage_key: str, detail: str = "") -> None:
        stage = next(item for item in self.progress["stages"] if item["key"] == stage_key)
        stage.update({"status": "RUNNING", "started_at": _now(), "detail": detail})
        self.progress.update({
            "status": "RUNNING",
            "current_stage": stage_key,
            "current_label": stage["label"],
        })
        self._persist()

    def finish(self, stage_key: str, status: str = "SUCCEEDED", detail: str = "") -> None:
        stage = next(item for item in self.progress["stages"] if item["key"] == stage_key)
        stage.update({"status": status, "finished_at": _now(), "detail": detail})
        self._persist()

    def finalize(self, status: str, detail: str = "") -> None:
        if status == "FAILED":
            for stage in self.progress["stages"]:
                if stage["status"] == "RUNNING":
                    stage.update({"status": "FAILED", "finished_at": _now(), "detail": detail})
        self.progress.update({
            "status": status,
            "current_stage": None,
            "current_label": detail or ("本轮完成" if status == "SUCCESS" else "本轮失败"),
        })
        if status in {"SUCCESS", "SUCCESS_WITH_WARNINGS"}:
            self.progress["percent"] = 100.0
        self._persist()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sigmoid(value: float) -> float:
    value = _clamp(value, -30.0, 30.0)
    return 1.0 / (1.0 + math.exp(-value))


def _prediction(weights: dict[str, float], features: dict[str, float]) -> tuple[float, float]:
    logit = sum(float(weights.get(name, 0.0)) * float(features.get(name, 0.0))
                for name in FEATURE_NAMES)
    probability = _sigmoid(logit)
    return probability, _clamp((probability - 0.5) * 0.04, -0.02, 0.02)


def _is_a_share(symbol: str) -> bool:
    return bool(re.fullmatch(r"(?:00|30|60|68)\d{4}", str(symbol)))


def learning_universe(limit: int = 20) -> list[str]:
    initialize()
    with closing(connect()) as conn:
        quant_rows = conn.execute(
            """SELECT DISTINCT c.symbol
               FROM quant_portfolio_versions v
               JOIN quant_portfolio_candidates c ON c.run_id=v.run_id
               WHERE v.status='ACTIVE'
               ORDER BY c.liquidity_rank,c.symbol LIMIT ?""",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        rows = conn.execute(
            """SELECT symbol FROM comparison_watchlist
               WHERE length(symbol)=6 ORDER BY last_compared_at DESC LIMIT ?""",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    symbols = [str(row[0]) for row in quant_rows if _is_a_share(str(row[0]))]
    symbols.extend(str(row[0]) for row in rows
                   if _is_a_share(str(row[0])) and str(row[0]) not in symbols)
    for symbol in DEFAULT_UNIVERSE:
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols[:max(3, min(int(limit), 100))]


def refresh_trading_calendar() -> dict:
    """Cache the official calendar exposed by AKShare; fail-soft for offline runs."""
    initialize()
    try:
        import akshare as ak
        frame = ak.tool_trade_date_hist_sina()
        values = [str(item)[:10] for item in frame.iloc[:, 0].tolist()]
        stamp = _now()
        with closing(connect()) as conn:
            conn.executemany(
                """INSERT INTO trading_calendar(trade_date,market,is_open,source,updated_at)
                   VALUES(?,'CN',1,'akshare_sina',?)
                   ON CONFLICT(trade_date,market) DO UPDATE SET
                     is_open=1,source=excluded.source,updated_at=excluded.updated_at""",
                [(item, stamp) for item in values],
            )
            conn.commit()
        return {"status": "REFRESHED", "open_dates": len(values)}
    except Exception as exc:
        return {"status": "DEGRADED", "open_dates": 0, "error": repr(exc)}


def is_trading_day(value: date) -> bool:
    initialize()
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
    return value.weekday() < 5


def next_trading_day(value: date) -> date:
    initialize()
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT trade_date FROM trading_calendar
               WHERE market='CN' AND is_open=1 AND trade_date>? ORDER BY trade_date LIMIT 1""",
            (value.isoformat(),),
        ).fetchone()
    if row:
        return date.fromisoformat(str(row[0]))
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _strip_markup(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()


def _news_score(text: str) -> tuple[float, int, int]:
    positive = sum(text.count(term) for term in POSITIVE_TERMS)
    negative = sum(text.count(term) for term in NEGATIVE_TERMS)
    total = positive + negative
    return ((positive - negative) / total if total else 0.0), positive, negative


def _fetch_eastmoney_news(symbol: str, page_size: int = 20) -> tuple[list[dict], str]:
    """Use the public endpoint behind AKShare's documented stock_news_em adapter."""
    from curl_cffi import requests

    callback = "jQuery35101792940631092459_1764599530165"
    inner = {
        "uid": "", "keyword": symbol, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "default", "pageIndex": 1,
            "pageSize": max(1, min(int(page_size), 100)),
            "preTag": "<em>", "postTag": "</em>",
        }},
    }
    response = requests.get(
        "https://search-api-web.eastmoney.com/search/jsonp",
        params={"cb": callback, "param": json.dumps(inner, ensure_ascii=False),
                "_": str(int(time.time() * 1000))},
        headers={"referer": f"https://so.eastmoney.com/news/s?keyword={symbol}",
                 "user-agent": "Mozilla/5.0 ArgusResearch/1.0"},
        timeout=15,
    )
    if response.status_code != 200:
        raise RuntimeError(f"news HTTP {response.status_code}")
    raw = response.text
    prefix = f"{callback}("
    if not raw.startswith(prefix) or not raw.endswith(")"):
        raise RuntimeError("unexpected news response")
    payload = json.loads(raw[len(prefix):-1])
    rows = payload.get("result", {}).get("cmsArticleWebOld", []) or []
    documents = []
    for row in rows:
        code = str(row.get("code") or "").strip()
        documents.append({
            "document_type": "a_share_news",
            "title": _strip_markup(row.get("title")),
            "body": _strip_markup(row.get("content")),
            "source_url": (str(row.get("url") or "").strip()
                           or (f"https://finance.eastmoney.com/a/{code}.html" if code else None)),
            "source_name": str(row.get("mediaName") or "东方财富"),
            "published_at": str(row.get("date") or "") or None,
            "observed_at": _now(),
            "metadata": {"symbol": symbol, "adapter": "akshare_stock_news_em"},
        })
    return documents, raw


def collect_a_share_sentiment(symbols: Iterable[str], as_of: date | None = None) -> dict:
    target_date = as_of or datetime.now(SHANGHAI).date()
    results, errors = [], []
    for symbol in dict.fromkeys(str(item) for item in symbols if _is_a_share(str(item))):
        try:
            documents, raw = _fetch_eastmoney_news(symbol)
            persisted = persist_source_documents("akshare", "news", symbol, documents, raw)
            selected = []
            for item in documents:
                published = str(item.get("published_at") or "")[:10]
                if not published or published <= target_date.isoformat():
                    selected.append(item)
            weighted, positives, negatives = [], 0, 0
            for item in selected:
                score, positive, negative = _news_score(f"{item['title']} {item['body']}")
                positives += positive
                negatives += negative
                weighted.append(score)
            score = statistics.fmean(weighted) if weighted else 0.0
            term_count = positives + negatives
            confidence = min(1.0, (len(selected) / 10.0) * (0.35 + min(term_count, 12) / 18.0))
            stamp = _now()
            with closing(connect()) as conn:
                conn.execute(
                    """INSERT INTO sentiment_daily
                       (symbol,trade_date,score,confidence,document_count,positive_count,
                        negative_count,source_breakdown_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?, ?,?,?)
                       ON CONFLICT(symbol,trade_date) DO UPDATE SET
                         score=excluded.score,confidence=excluded.confidence,
                         document_count=excluded.document_count,
                         positive_count=excluded.positive_count,
                         negative_count=excluded.negative_count,
                         source_breakdown_json=excluded.source_breakdown_json,
                         updated_at=excluded.updated_at""",
                    (symbol, target_date.isoformat(), score, confidence, len(selected), positives,
                     negatives, _dump({"akshare_eastmoney": len(selected)}), stamp, stamp),
                )
                conn.commit()
            results.append({"symbol": symbol, "score": round(score, 4),
                            "confidence": round(confidence, 4),
                            "documents": len(selected), "persisted": persisted["rows"]})
        except Exception as exc:
            errors.append({"symbol": symbol, "error": repr(exc)})
    return {"date": target_date.isoformat(), "symbols": results, "errors": errors,
            "documents": sum(item["documents"] for item in results)}


def _refresh_market_data_isolated(symbols: Iterable[str], include_minutes: bool) -> dict:
    arguments = [sys.executable, "-m", "app.data_sources.market", "--no-history"]
    if not include_minutes:
        arguments.append("--no-minute")
    for symbol in symbols:
        arguments.extend(("--symbol", str(symbol)))
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    completed = subprocess.run(
        arguments, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, creationflags=flags,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"market refresh returned invalid output: {completed.stderr[-500:]}"
        ) from exc
    if completed.returncode not in {0, 2}:
        raise RuntimeError(f"market refresh failed: {completed.stderr[-500:]}")
    return result


def _load_bars(symbols: Iterable[str]) -> dict[str, list[dict]]:
    from .market_quality import contiguous_daily_window
    initialize()
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with closing(connect()) as conn:
        rows = conn.execute(
            f"""WITH ranked AS (
                 SELECT b.asset_symbol,b.trade_date,b.open,b.high,b.low,b.close,b.volume,
                        ROW_NUMBER() OVER (
                          PARTITION BY b.asset_symbol,b.trade_date
                          ORDER BY ds.priority,b.captured_at DESC) AS rn
                 FROM market_daily_bars b JOIN data_sources ds ON ds.id=b.source_id
                 WHERE b.asset_symbol IN ({placeholders}) AND b.adjust_mode='qfq'
               )
               SELECT asset_symbol,trade_date,open,high,low,close,volume
               FROM ranked WHERE rn=1 ORDER BY asset_symbol,trade_date""",
            symbols,
        ).fetchall()
    result: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        result[str(row["asset_symbol"])].append(dict(row))
    return {symbol: contiguous_daily_window(bars)[0] for symbol, bars in result.items()}


def _load_sentiment(symbols: Iterable[str]) -> dict[tuple[str, str], float]:
    initialize()
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with closing(connect()) as conn:
        rows = conn.execute(
            f"SELECT symbol,trade_date,score FROM sentiment_daily WHERE symbol IN ({placeholders})",
            symbols,
        ).fetchall()
    return {(str(row["symbol"]), str(row["trade_date"])): float(row["score"]) for row in rows}


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _features_at(bars: list[dict], index: int, sentiment: float, market_mom: float) -> dict[str, float]:
    window = bars[max(0, index - 20):index + 1]
    index = len(window) - 1
    closes = [float(item["close"]) for item in window]
    volumes = [float(item["volume"] or 0.0) for item in window]
    returns = [closes[pos] / closes[pos - 1] - 1.0 for pos in range(index - 19, index + 1)]
    volume_window = volumes[index - 19:index + 1]
    volume_mean = _mean(volume_window)
    volume_std = statistics.pstdev(volume_window) if len(volume_window) > 1 else 0.0
    annual_vol = statistics.pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    return {
        "bias": 1.0,
        "mom_5": _clamp((closes[index] / closes[index - 5] - 1.0) / 0.05, -3.0, 3.0),
        "mom_20": _clamp((closes[index] / closes[index - 20] - 1.0) / 0.12, -3.0, 3.0),
        "ma_spread_5_20": _clamp((_mean(closes[index - 4:index + 1]) /
                                      _mean(closes[index - 19:index + 1]) - 1.0) / 0.05, -3.0, 3.0),
        "vol_20": _clamp((annual_vol - 0.25) / 0.15, -3.0, 3.0),
        "volume_z20": _clamp((volumes[index] - volume_mean) / volume_std, -3.0, 3.0)
        if volume_std else 0.0,
        "sentiment": _clamp(sentiment, -1.0, 1.0),
        "market_mom_5": _clamp(market_mom / 0.05, -3.0, 3.0),
    }


def build_walk_forward_samples(symbols: Iterable[str]) -> list[dict]:
    bars_by_symbol = _load_bars(symbols)
    sentiment = _load_sentiment(bars_by_symbol)
    market_by_date: dict[str, list[float]] = defaultdict(list)
    for bars in bars_by_symbol.values():
        for index in range(5, len(bars)):
            market_by_date[str(bars[index]["trade_date"])].append(
                float(bars[index]["close"]) / float(bars[index - 5]["close"]) - 1.0)
    samples = []
    for symbol, bars in bars_by_symbol.items():
        for index in range(20, len(bars) - 1):
            signal_date = str(bars[index]["trade_date"])
            target_date = str(bars[index + 1]["trade_date"])
            actual_return = float(bars[index + 1]["close"]) / float(bars[index]["close"]) - 1.0
            samples.append({
                "symbol": symbol, "signal_date": signal_date, "target_date": target_date,
                "features": _features_at(
                    bars, index, sentiment.get((symbol, signal_date), 0.0),
                    _mean(market_by_date.get(signal_date, [])),
                ),
                "actual_return": actual_return,
            })
    return sorted(samples, key=lambda item: (item["target_date"], item["symbol"]))


def build_online_probability_map(symbols: Iterable[str]) -> dict[str, dict[str, float]]:
    """Build prequential probabilities: each outcome updates only later signals."""
    samples = build_walk_forward_samples(symbols)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        grouped[sample["target_date"]].append(sample)
    weights = dict(INITIAL_COEFFICIENTS)
    probability_map: dict[str, dict[str, float]] = defaultdict(dict)
    for target_date in sorted(grouped):
        batch = grouped[target_date]
        for sample in batch:
            probability, _ = _prediction(weights, sample["features"])
            probability_map[sample["symbol"]][sample["signal_date"]] = probability
        _update_online_weights(weights, batch)
    return {symbol: dict(values) for symbol, values in probability_map.items()}


def _update_online_weights(weights: dict[str, float], batch: list[dict]) -> None:
    """One daily gradient step, independent of stock ordering and universe size."""
    if not batch:
        return
    gradient = {name: 0.0 for name in FEATURE_NAMES}
    for sample in batch:
        probability, _ = _prediction(weights, sample["features"])
        error = float(sample["actual_return"] > 0) - probability
        for name in FEATURE_NAMES:
            gradient[name] += error * float(sample["features"][name]) / len(batch)
    for name in FEATURE_NAMES:
        regularization = 0.0005 * weights.get(name, 0.0) if name != "bias" else 0.0
        weights[name] = _clamp(weights.get(name, 0.0) + 0.05 *
                               (gradient[name] - regularization), -5.0, 5.0)


def latest_symbol_scores(symbols: Iterable[str], data_asof: str | None = None,
                         model_version: str | None = None) -> dict:
    """Score the latest completed bar with the active, versioned research model."""
    requested = list(dict.fromkeys(str(symbol) for symbol in symbols))
    if model_version:
        initialize()
        with closing(connect()) as conn:
            row = conn.execute(
                "SELECT * FROM prediction_model_versions WHERE version_key=?",
                (model_version,),
            ).fetchone()
        if not row:
            raise RuntimeError(f"预测模型版本不存在：{model_version}")
        model = dict(row)
        model["coefficients"] = _load(
            model.pop("coefficients_json"), dict(INITIAL_COEFFICIENTS)
        )
    else:
        model = _ensure_active_model()
    bars_by_symbol = _load_bars(requested)
    if data_asof:
        bars_by_symbol = {
            symbol: [row for row in bars if str(row["trade_date"]) <= str(data_asof)]
            for symbol, bars in bars_by_symbol.items()
        }
    sentiment = _load_sentiment(requested)
    latest_sentiment: dict[str, tuple[str, float]] = {}
    for (symbol, trade_date), value in sentiment.items():
        if data_asof and str(trade_date) > str(data_asof):
            continue
        if symbol not in latest_sentiment or trade_date > latest_sentiment[symbol][0]:
            latest_sentiment[symbol] = (trade_date, float(value))
    latest_returns = [
        float(bars[-1]["close"]) / float(bars[-6]["close"]) - 1.0
        for bars in bars_by_symbol.values() if len(bars) >= 21
    ]
    market_momentum = _mean(latest_returns)
    scores = []
    for symbol in requested:
        bars = bars_by_symbol.get(symbol, [])
        if len(bars) < 21:
            continue
        latest = bars[-1]
        features = _features_at(
            bars, len(bars) - 1,
            latest_sentiment.get(symbol, ("", 0.0))[1], market_momentum,
        )
        probability, predicted_return = _prediction(model["coefficients"], features)
        scores.append({
            "symbol": symbol,
            "signal_date": str(latest["trade_date"]),
            "probability_up": round(probability, 8),
            "predicted_return": round(predicted_return, 8),
            "confidence": round(_clamp(abs(probability - 0.5) * 2.0, 0.0, 1.0), 8),
            "features": features,
            "sentiment_date": latest_sentiment.get(symbol, (None, 0.0))[0],
        })
    return {"model_version": model["version_key"], "scores": scores}


def _evaluate_points(points: list[dict]) -> dict:
    if not points:
        return {"sample_count": 0, "directional_accuracy": 0.0, "brier_score": 1.0,
                "return_mae": 1.0, "strategy_return": 0.0, "annualized_return": 0.0,
                "sharpe": 0.0, "max_drawdown": 0.0}
    daily: dict[str, list[dict]] = defaultdict(list)
    for point in points:
        daily[point["target_date"]].append(point)
    daily_returns, previous_holdings, turnover_total, cost_total = [], set(), 0.0, 0.0
    for key in sorted(daily):
        holdings = {item["symbol"] for item in daily[key] if item["probability_up"] >= 0.55}
        gross_return = _mean([item["actual_return"] for item in daily[key]
                              if item["symbol"] in holdings])
        denominator = max(1, len(holdings), len(previous_holdings))
        turnover = len(holdings.symmetric_difference(previous_holdings)) / denominator
        cost = turnover * RESEARCH_TURNOVER_COST
        daily_returns.append(gross_return - cost)
        turnover_total += turnover
        cost_total += cost
        previous_holdings = holdings
    equity, peak, max_drawdown = 1.0, 1.0, 0.0
    for value in daily_returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1.0 - equity / peak)
    daily_std = statistics.pstdev(daily_returns) if len(daily_returns) > 1 else 0.0
    annualized = equity ** (252 / len(daily_returns)) - 1.0 if daily_returns else 0.0
    return {
        "sample_count": len(points),
        "trading_days": len(daily_returns),
        "directional_accuracy": _mean([float(item["direction_correct"]) for item in points]),
        "brier_score": _mean([float(item["brier_score"]) for item in points]),
        "return_mae": _mean([abs(item["predicted_return"] - item["actual_return"]) for item in points]),
        "strategy_return": equity - 1.0,
        "annualized_return": annualized,
        "sharpe": ((_mean(daily_returns) / daily_std) * math.sqrt(252)) if daily_std else 0.0,
        "max_drawdown": max_drawdown,
        "turnover": turnover_total,
        "transaction_costs": cost_total,
        "execution_assumptions": {"signal_threshold": 0.55,
                                  "equal_weight": True,
                                  "turnover_cost_rate": RESEARCH_TURNOVER_COST},
    }


def _point(sample: dict, probability: float, predicted_return: float) -> dict:
    actual_up = sample["actual_return"] > 0
    predicted_up = probability >= 0.5
    return {
        "symbol": sample["symbol"], "signal_date": sample["signal_date"],
        "target_date": sample["target_date"], "probability_up": probability,
        "predicted_return": predicted_return, "actual_return": sample["actual_return"],
        "direction_correct": int(actual_up == predicted_up),
        "brier_score": (probability - float(actual_up)) ** 2,
    }


def _ensure_active_model() -> dict:
    initialize()
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM prediction_model_versions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            now = _now()
            conn.execute(
                """INSERT INTO prediction_model_versions
                   (version_key,parent_version,status,feature_schema_json,coefficients_json,
                    metrics_json,gate_json,reason,created_at,activated_at)
                   VALUES('prediction-baseline-v1',NULL,'ACTIVE',?,?, '{}','{}',?,?,?)""",
                (_dump(list(FEATURE_NAMES)), _dump(INITIAL_COEFFICIENTS),
                 "可解释的冷启动基线；晋级只能通过样本外门禁", now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM prediction_model_versions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
            ).fetchone()
    item = dict(row)
    item["coefficients"] = _load(item.pop("coefficients_json"), dict(INITIAL_COEFFICIENTS))
    return item


def run_six_month_walk_forward(symbols: Iterable[str], cycle_id: int | None = None,
                               auto_promote: bool = True, max_drawdown: float = 0.15) -> dict:
    symbols = list(dict.fromkeys(symbols))
    active = _ensure_active_model()
    baseline_version = active["version_key"]
    baseline_weights = dict(active["coefficients"])
    samples = build_walk_forward_samples(symbols)
    complete_asof = _latest_data_date(symbols)
    samples = [item for item in samples if complete_asof and item["target_date"] <= complete_asof]
    previous_end = str(active.get("training_end") or "")
    previous_metrics = _load(active.get("metrics_json"), {})
    incremental = bool(previous_end)
    # The incumbent has already seen its training window. Comparing it on that
    # window would leak labels; daily updates use only subsequently realized days.
    dates = sorted({item["target_date"] for item in samples
                    if not previous_end or item["target_date"] > previous_end})
    if not incremental:
        dates = dates[-126:]
    if incremental and not dates:
        unchanged_gate = {**_load(active.get("gate_json"), {}),
                          "new_data_available": False, "evaluation_reused": True}
        with closing(connect()) as conn:
            cursor = conn.execute(
                """INSERT INTO prediction_evaluations
                   (cycle_id,baseline_version,candidate_version,eval_start,eval_end,sample_count,
                    baseline_metrics_json,candidate_metrics_json,gate_json,status,created_at)
                   VALUES(?,?,?,?,?,0,?,?,?,'UNCHANGED',?)""",
                (cycle_id, baseline_version, baseline_version, previous_end, previous_end,
                 _dump(previous_metrics), _dump(previous_metrics),
                 _dump(unchanged_gate), _now()),
            )
            conn.commit()
        return {"evaluation_id": cursor.lastrowid, "baseline_version": baseline_version,
                "candidate_version": baseline_version, "status": "UNCHANGED",
                "gate": unchanged_gate,
                "baseline_metrics": previous_metrics, "candidate_metrics": previous_metrics,
                "training_start": active.get("training_start"), "training_end": previous_end}
    selected = [item for item in samples if item["target_date"] in set(dates)]
    candidate_weights = dict(baseline_weights)
    baseline_points, candidate_points = [], []
    grouped: dict[str, list[dict]] = defaultdict(list)
    for sample in selected:
        grouped[sample["target_date"]].append(sample)
    for target_date in sorted(grouped):
        batch = grouped[target_date]
        for sample in batch:
            bp, br = _prediction(baseline_weights, sample["features"])
            cp, cr = _prediction(candidate_weights, sample["features"])
            baseline_points.append(_point(sample, bp, br))
            candidate_points.append(_point(sample, cp, cr))
        _update_online_weights(candidate_weights, batch)
    baseline_metrics = _evaluate_points(baseline_points)
    candidate_metrics = _evaluate_points(candidate_points)
    prior_samples = int(previous_metrics.get("training_sample_count",
                                            previous_metrics.get("sample_count", 0)))
    training_samples = (prior_samples if incremental else 0) + len(selected)
    enough = bool(selected) and training_samples >= max(
        60, len({item["symbol"] for item in selected}) * 20)
    non_regression = (
        candidate_metrics["directional_accuracy"] >= baseline_metrics["directional_accuracy"] - 0.005
        and candidate_metrics["brier_score"] <= baseline_metrics["brier_score"] + 0.002
    )
    improvement = (
        candidate_metrics["directional_accuracy"] >= baseline_metrics["directional_accuracy"] + 0.005
        or candidate_metrics["brier_score"] <= baseline_metrics["brier_score"] - 0.002
        or candidate_metrics["sharpe"] >= baseline_metrics["sharpe"] + 0.10
    )
    risk_pass = candidate_metrics["max_drawdown"] <= float(max_drawdown)
    eligible = enough and non_regression and risk_pass and (incremental or improvement)
    stamp = _now()
    version_key = f"prediction-{datetime.now(SHANGHAI).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    gate = {"enough_samples": enough, "non_regression": non_regression,
            "measurable_improvement": improvement, "max_drawdown_pass": risk_pass,
            "daily_learning_update": incremental,
            "comparison_version": baseline_version,
            "comparison_after_training_end": previous_end or None,
            "evaluation_scope": "incremental_forward" if incremental else "cold_start_prequential",
            "evaluation_trading_days": len(dates), "evaluation_sample_count": len(selected),
            "training_sample_count": training_samples,
            "thresholds": {"window_trading_days": 126, "max_drawdown": max_drawdown,
                           "accuracy_tolerance": 0.005, "brier_tolerance": 0.002}}
    candidate_metrics["training_sample_count"] = training_samples
    candidate_metrics["evaluation_scope"] = gate["evaluation_scope"]
    same_weights = all(abs(candidate_weights.get(name, 0.0) -
                           float(active["coefficients"].get(name, 0.0))) <= 1e-12
                       for name in FEATURE_NAMES)
    unchanged = bool(dates and active.get("training_end") == dates[-1] and same_weights)
    gate["new_data_available"] = not unchanged
    status = ("UNCHANGED" if unchanged else
              ("ACTIVE" if eligible and auto_promote else
               ("CANDIDATE" if eligible else "REJECTED")))
    candidate_version = active["version_key"] if unchanged else version_key
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT version_key FROM prediction_model_versions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not current or current[0] != baseline_version:
            raise RuntimeError("active prediction model changed during evaluation; retry from its new checkpoint")
        if status == "ACTIVE":
            conn.execute("UPDATE prediction_model_versions SET status='ARCHIVED' WHERE status='ACTIVE'")
        if not unchanged:
            conn.execute(
                """INSERT INTO prediction_model_versions
                   (version_key,parent_version,status,feature_schema_json,coefficients_json,
                    training_start,training_end,metrics_json,gate_json,reason,created_at,activated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (version_key, active["version_key"], status, _dump(list(FEATURE_NAMES)),
                 _dump(candidate_weights), (active.get("training_start") or dates[0]) if dates else None,
                 dates[-1] if dates else None,
                 _dump(candidate_metrics), _dump(gate),
                 ("逐日增量学习；与当前活动模型比较新增已实现样本" if incremental else
                  "冷启动逐日样本外评估") if eligible else "未通过当前模型非退化或风险门禁",
                 stamp, stamp if status == "ACTIVE" else None),
            )
        cursor = conn.execute(
            """INSERT INTO prediction_evaluations
               (cycle_id,baseline_version,candidate_version,eval_start,eval_end,sample_count,
                baseline_metrics_json,candidate_metrics_json,gate_json,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (cycle_id, baseline_version, candidate_version, dates[0] if dates else "",
             dates[-1] if dates else "", len(selected), _dump(baseline_metrics),
             _dump(candidate_metrics), _dump(gate), status, stamp),
        )
        evaluation_id = int(cursor.lastrowid)
        rows = []
        for role, points in (("BASELINE", baseline_points), ("CANDIDATE", candidate_points)):
            rows.extend((evaluation_id, role, item["symbol"], item["signal_date"], item["target_date"],
                         item["probability_up"], item["predicted_return"], item["actual_return"],
                         item["direction_correct"], item["brier_score"]) for item in points)
        conn.executemany(
            """INSERT INTO prediction_backtest_points
               (evaluation_id,model_role,symbol,signal_date,target_date,probability_up,
                predicted_return,actual_return,direction_correct,brier_score)
               VALUES(?,?,?,?,?,?,?,?,?,?)""", rows,
        )
        conn.commit()
    return {"evaluation_id": evaluation_id, "baseline_version": baseline_version,
            "candidate_version": candidate_version, "status": status, "gate": gate,
            "baseline_metrics": baseline_metrics, "candidate_metrics": candidate_metrics,
            "training_start": dates[0] if dates else None, "training_end": dates[-1] if dates else None}


def settle_predictions(cycle_id: int | None = None) -> dict:
    initialize()
    scored = []
    with closing(connect()) as conn:
        pending = conn.execute(
            "SELECT * FROM daily_predictions WHERE status='PENDING' ORDER BY target_date,symbol"
        ).fetchall()
        for row in pending:
            prices = conn.execute(
                """WITH ranked AS (
                     SELECT b.trade_date,b.close,ROW_NUMBER() OVER (
                       PARTITION BY b.trade_date ORDER BY ds.priority,b.captured_at DESC) rn
                     FROM market_daily_bars b JOIN data_sources ds ON ds.id=b.source_id
                     WHERE b.asset_symbol=? AND b.adjust_mode='qfq' AND b.trade_date<=?
                   ) SELECT trade_date,close FROM ranked WHERE rn=1 ORDER BY trade_date DESC LIMIT 2""",
                (row["symbol"], row["target_date"]),
            ).fetchall()
            if len(prices) < 2 or str(prices[0]["trade_date"]) != str(row["target_date"]):
                continue
            actual_return = float(prices[0]["close"]) / float(prices[1]["close"]) - 1.0
            actual_up = actual_return > 0
            correct = int(actual_up == (float(row["probability_up"]) >= 0.5))
            brier = (float(row["probability_up"]) - float(actual_up)) ** 2
            conn.execute(
                """UPDATE daily_predictions SET actual_return=?,actual_direction=?,
                   direction_correct=?,brier_score=?,absolute_error=?,status='SCORED',scored_at=?
                   WHERE id=?""",
                (actual_return, int(actual_up), correct, brier,
                 abs(float(row["predicted_return"]) - actual_return), _now(), row["id"]),
            )
            scored.append({"prediction_key": row["prediction_key"], "symbol": row["symbol"],
                           "target_date": row["target_date"], "correct": bool(correct),
                           "actual_return": actual_return})
        conn.commit()
    return {"scored": len(scored), "predictions": scored}


def create_daily_predictions(symbols: Iterable[str], phase: str, cycle_id: int,
                             signal_date: date, target_date: date) -> dict:
    model = _ensure_active_model()
    symbols = list(dict.fromkeys(symbols))
    bars_by_symbol = _load_bars(symbols)
    sentiment = _load_sentiment(symbols)
    latest_returns = []
    for bars in bars_by_symbol.values():
        if len(bars) >= 6:
            latest_returns.append(float(bars[-1]["close"]) / float(bars[-6]["close"]) - 1.0)
    market_mom = _mean(latest_returns)
    created = []
    stamp = _now()
    with closing(connect()) as conn:
        for symbol in symbols:
            bars = bars_by_symbol.get(symbol, [])
            if len(bars) < 21:
                continue
            latest = bars[-1]
            features = _features_at(
                bars, len(bars) - 1,
                sentiment.get((symbol, signal_date.isoformat()),
                              sentiment.get((symbol, str(latest["trade_date"])), 0.0)),
                market_mom,
            )
            probability, predicted_return = _prediction(model["coefficients"], features)
            key_source = f"{model['version_key']}|{symbol}|{target_date.isoformat()}"
            prediction_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
            confidence = _clamp(abs(probability - 0.5) * 2.0, 0.0, 1.0)
            rationale = {"last_completed_bar": latest["trade_date"],
                         "research_only": True, "order_execution": False}
            conn.execute(
                """INSERT INTO daily_predictions
                   (prediction_key,cycle_id,model_version,symbol,signal_date,target_date,phase,
                    probability_up,predicted_return,confidence,features_json,rationale_json,
                    status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'PENDING',?)
                   ON CONFLICT(prediction_key) DO UPDATE SET
                     cycle_id=excluded.cycle_id,signal_date=excluded.signal_date,phase=excluded.phase,
                     probability_up=excluded.probability_up,predicted_return=excluded.predicted_return,
                     confidence=excluded.confidence,features_json=excluded.features_json,
                     rationale_json=excluded.rationale_json""",
                (prediction_key, cycle_id, model["version_key"], symbol, signal_date.isoformat(),
                 target_date.isoformat(), phase, probability, predicted_return, confidence,
                 _dump(features), _dump(rationale), stamp),
            )
            created.append({"symbol": symbol, "probability_up": probability,
                            "predicted_return": predicted_return, "confidence": confidence})
        conn.commit()
    return {"model_version": model["version_key"], "signal_date": signal_date.isoformat(),
            "target_date": target_date.isoformat(), "count": len(created), "predictions": created}


def market_data_coverage(symbols: Iterable[str], required_date: str | None = None) -> dict:
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        return {"complete": False, "data_asof": None, "missing_symbols": [],
                "stale_symbols": [], "latest_by_symbol": {}}
    placeholders = ",".join("?" for _ in symbols)
    with closing(connect()) as conn:
        rows = conn.execute(
            f"""SELECT b.asset_symbol,MAX(b.trade_date) AS latest
                FROM market_daily_bars b JOIN data_sources s ON s.id=b.source_id
                WHERE b.asset_symbol IN ({placeholders}) AND b.adjust_mode='qfq'
                  AND s.code IN ('tdx_local','tdx_public')
                GROUP BY b.asset_symbol""",
            symbols,
        ).fetchall()
    latest = {str(row["asset_symbol"]): str(row["latest"]) for row in rows if row["latest"]}
    missing = [symbol for symbol in symbols if symbol not in latest]
    stale = [symbol for symbol in symbols if required_date and
             symbol in latest and latest[symbol] < required_date]
    return {"complete": not missing and not stale,
            "data_asof": min(latest.values()) if latest and not missing else None,
            "missing_symbols": missing, "stale_symbols": stale,
            "latest_by_symbol": latest, "required_date": required_date}


def _latest_data_date(symbols: Iterable[str]) -> str | None:
    return market_data_coverage(symbols)["data_asof"]


def market_close_coverage(symbols: Iterable[str], required_date: str) -> dict:
    symbols = list(dict.fromkeys(symbols))
    daily = market_data_coverage(symbols, required_date)
    expected_minutes = set(range(9 * 60 + 31, 11 * 60 + 31)) | set(range(13 * 60 + 1, 15 * 60 + 1))
    observed = {symbol: set() for symbol in symbols}
    end_date = (date.fromisoformat(required_date) + timedelta(days=1)).isoformat()
    if symbols:
        with closing(connect()) as conn:
            rows = conn.execute(
                f"""SELECT m.asset_symbol,m.bar_time FROM minute_bars m
                    JOIN data_sources s ON s.id=m.source_id
                    WHERE m.asset_symbol IN ({','.join('?' for _ in symbols)})
                      AND m.interval_minutes=1 AND m.bar_time>=? AND m.bar_time<?
                      AND s.code IN ('tdx_local','tdx_public')""",
                [*symbols, required_date, end_date],
            ).fetchall()
        for row in rows:
            try:
                stamp = datetime.fromisoformat(row["bar_time"])
                local = stamp.astimezone(SHANGHAI) if stamp.tzinfo else stamp.replace(tzinfo=SHANGHAI)
            except (TypeError, ValueError):
                continue
            if local.date().isoformat() == required_date and local.second == 0:
                observed[row["asset_symbol"]].add(local.hour * 60 + local.minute)
    missing = [symbol for symbol, minutes in observed.items() if not expected_minutes <= minutes]
    return {"complete": daily["complete"] and not missing, "daily": daily,
            "missing_minute_symbols": missing,
            "minute_counts": {symbol: len(minutes & expected_minutes) for symbol, minutes in observed.items()}}


def _cycle_needs_retry(row) -> bool:
    if not row:
        return True
    metrics = _load(row["metrics_json"], {})
    return (row["status"] not in {"SUCCESS", "SUCCESS_WITH_WARNINGS"}
            or bool(metrics.get("retry_required"))
            or bool(metrics.get("quant_portfolios", {}).get("errors")))


def run_continuous_learning_cycle(inputs: dict | None = None) -> dict:
    inputs = dict(inputs or {})
    initialize()
    phase = str(inputs.get("phase", "BACKFILL")).upper()
    if phase not in {"PRE_OPEN", "POST_CLOSE", "BACKFILL"}:
        raise ValueError("phase 必须是 PRE_OPEN、POST_CLOSE 或 BACKFILL")
    symbols = list(inputs.get("symbols") or learning_universe(int(inputs.get("stock_limit", 20))))
    symbols = [str(item) for item in symbols if _is_a_share(str(item))]
    local_now = datetime.now(SHANGHAI)
    requested_date = inputs.get("cycle_date")
    cycle_date = date.fromisoformat(str(requested_date)) if requested_date else local_now.date()
    deterministic = phase in {"PRE_OPEN", "POST_CLOSE"}
    cycle_key = (f"continuous-{phase.lower()}-{cycle_date.isoformat()}" if deterministic
                 else f"continuous-backfill-{uuid.uuid4().hex}")
    started = _now()
    worker_token = uuid.uuid4().hex
    stage_keys = _cycle_stage_keys(phase, inputs)
    progress = _initial_cycle_progress(stage_keys)
    previous_metrics = {}
    retry_quant_keys = None
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM harness_learning_cycles WHERE cycle_key=?", (cycle_key,)
        ).fetchone()
        retry_stale = bool(inputs.get("retry_if_stale")) and phase == "POST_CLOSE"
        stale_success = bool(existing and retry_stale and
                             str(existing["data_asof"] or "") < cycle_date.isoformat())
        if existing and not _cycle_needs_retry(existing) and not stale_success:
            return {"cycle_key": cycle_key, "status": "ALREADY_COMPLETED",
                    "metrics": _load(existing["metrics_json"], {}),
                    "progress": _load(existing["progress_json"], {})}
        if existing and existing["status"] == "RUNNING":
            heartbeat_age = _timestamp_age_seconds(existing["heartbeat_at"])
            started_age = _timestamp_age_seconds(existing["started_at"])
            lease_age = heartbeat_age if heartbeat_age is not None else started_age
            lease_timeout = (CYCLE_LEASE_TIMEOUT_SECONDS if heartbeat_age is not None
                             else int(timedelta(hours=2).total_seconds()))
            if lease_age is not None and lease_age < lease_timeout:
                return {
                    "cycle_key": cycle_key,
                    "status": "ALREADY_RUNNING",
                    "metrics": {},
                    "progress": _load(existing["progress_json"], {}),
                    "heartbeat_at": existing["heartbeat_at"],
                }
        if existing:
            frozen_symbols = _load(existing["universe_json"], [])
            if frozen_symbols:
                if inputs.get("symbols") and set(symbols) != set(frozen_symbols):
                    raise ValueError("cycle universe is frozen; use BACKFILL for a different stock pool")
                symbols = frozen_symbols
        if existing and str(existing["data_asof"] or "") >= cycle_date.isoformat():
            previous_metrics = _load(existing["metrics_json"], {})
            previous_quant = previous_metrics.get("quant_portfolios", {})
            if previous_quant.get("errors"):
                retry_quant_keys = [item["mandate_key"] for item in previous_quant["errors"]]
            for stage, flag in (("calendar", "refresh_calendar"),
                                ("fundamentals", "refresh_fundamentals"),
                                ("deep_learning", "train_deep_model"),
                                ("intraday_evolution", "evolve_intraday"),
                                ("code_evolution", "evolve_source_code")):
                result = previous_metrics.get(stage) or {}
                if result.get("status") in {"SUCCESS", "REFRESHED", "ACTIVE", "UNCHANGED", "REJECTED"}:
                    inputs[flag] = False
            stage_keys = _cycle_stage_keys(phase, inputs)
            progress = _initial_cycle_progress(stage_keys)
        conn.execute(
            """INSERT INTO harness_learning_cycles
               (cycle_key,cycle_date,phase,status,trigger_kind,universe_json,
                progress_json,heartbeat_at,worker_token,started_at)
               VALUES(?,?,?,'RUNNING',?,?,?,?,?,?)
               ON CONFLICT(cycle_key) DO UPDATE SET status='RUNNING',started_at=excluded.started_at,
                 errors_json='[]',progress_json=excluded.progress_json,
                 heartbeat_at=excluded.heartbeat_at,worker_token=excluded.worker_token,
                 finished_at=NULL""",
            (cycle_key, cycle_date.isoformat(), phase,
             str(inputs.get("trigger_kind", "manual")), _dump(symbols),
             _dump(progress), started, worker_token, started),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM harness_learning_cycles WHERE cycle_key=?", (cycle_key,)).fetchone()
        cycle_id = int(row[0])
    errors = []
    market_result = previous_metrics.get("market_refresh")
    tracker = _CycleProgress(cycle_id, worker_token, progress)
    tracker.start_heartbeat()
    try:
        calendar_result = previous_metrics.get("calendar", {"status": "SKIPPED"})
        if "calendar" in stage_keys:
            tracker.start("calendar")
            calendar_result = refresh_trading_calendar()
            tracker.finish("calendar", detail=str(calendar_result.get("status", "完成")))
        if "market_refresh" in stage_keys:
            tracker.start("market_refresh")
            try:
                cached_close = (market_close_coverage(symbols, cycle_date.isoformat())
                                if previous_metrics and phase == "POST_CLOSE" else {})
                if cached_close.get("complete"):
                    market_result = {"status": "CACHED_COMPLETE", "data_asof": cycle_date.isoformat(),
                                     "coverage": cached_close, "errors": []}
                else:
                    market_result = _refresh_market_data_isolated(
                        symbols, include_minutes=phase in {"PRE_OPEN", "POST_CLOSE"})
                market_errors = market_result.get("errors", [])
                errors.extend({"stage": "market_refresh", **item} for item in market_errors)
                tracker.finish(
                    "market_refresh",
                    "WARNING" if market_errors else "SUCCEEDED",
                    f"{market_result.get('status', '完成')} · 错误 {len(market_errors)}",
                )
            except Exception as exc:
                errors.append({"stage": "market_refresh", "error": repr(exc)})
                tracker.finish("market_refresh", "WARNING", repr(exc))
        fundamental_result = previous_metrics.get("fundamentals", {"status": "SKIPPED", "errors": []})
        if "fundamentals" in stage_keys:
            tracker.start("fundamentals")
            try:
                from .fundamentals import refresh_fundamental_snapshots
                preliminary_asof = _latest_data_date(symbols)
                fundamental_result = refresh_fundamental_snapshots(
                    symbols, preliminary_asof, conn_factory=connect
                )
                fundamental_errors = fundamental_result.get("errors", [])
                errors.extend({"stage": "fundamentals", **item} for item in fundamental_errors)
                tracker.finish(
                    "fundamentals", "WARNING" if fundamental_errors else "SUCCEEDED",
                    f"股票 {fundamental_result.get('refreshed', 0)} · 财报 {fundamental_result.get('report_rows', 0)}",
                )
            except Exception as exc:
                fundamental_result = {"status": "FAILED", "error": repr(exc), "errors": []}
                errors.append({"stage": "fundamentals", "error": repr(exc)})
                tracker.finish("fundamentals", "WARNING", repr(exc))
        sentiment_result = {"status": "SKIPPED", "documents": 0, "errors": []}
        if "sentiment" in stage_keys:
            tracker.start("sentiment")
            from .social_sentiment import collect_multisource_sentiment
            sentiment_result = collect_multisource_sentiment(
                symbols, cycle_date, int(inputs.get("max_social_symbols", 3)))
            sentiment_errors = sentiment_result.get("errors", [])
            errors.extend({"stage": "sentiment", **item} for item in sentiment_errors)
            tracker.finish(
                "sentiment",
                "WARNING" if sentiment_errors else "SUCCEEDED",
                f"文档 {sentiment_result.get('documents', 0)} · 错误 {len(sentiment_errors)}",
            )
        tracker.start("settle_predictions")
        scored = settle_predictions(cycle_id)
        from .deep_learning import settle_deep_predictions
        deep_scored = settle_deep_predictions()
        tracker.finish(
            "settle_predictions",
            detail=f"在线 {scored.get('scored', 0)} · 深度 {deep_scored.get('scored', 0)}",
        )
        if phase == "POST_CLOSE":
            coverage = market_data_coverage(symbols, cycle_date.isoformat())
            if not coverage["complete"]:
                unavailable = coverage["missing_symbols"] + coverage["stale_symbols"]
                errors.append({"stage": "data_freshness", **coverage})
                raise RuntimeError(
                    f"盘后数据未完整覆盖 {cycle_date.isoformat()}：{','.join(unavailable)}"
                )
        tracker.start("online_model")
        evaluation = run_six_month_walk_forward(
            symbols, cycle_id, bool(inputs.get("auto_promote", True)),
            float(inputs.get("max_drawdown", 0.15)),
        )
        tracker.finish("online_model", detail=str(evaluation.get("status", "完成")))
        data_asof = _latest_data_date(symbols)
        market_date_complete = bool(
            phase != "POST_CLOSE" or
            (data_asof and str(data_asof) >= cycle_date.isoformat())
        )
        if not market_date_complete:
            errors.append({
                "stage": "data_freshness",
                "error": (f"盘后行情尚未覆盖 {cycle_date.isoformat()}，"
                          f"当前数据截至 {data_asof or '未知'}；调度器将自动重试"),
            })
        if phase == "PRE_OPEN":
            target = cycle_date
        else:
            base = date.fromisoformat(data_asof) if data_asof else cycle_date
            target = next_trading_day(base)
        tracker.start("next_predictions")
        predictions = create_daily_predictions(symbols, phase, cycle_id, cycle_date, target)
        tracker.finish("next_predictions", detail=f"预测 {predictions.get('count', 0)} 条")
        deep_result = previous_metrics.get("deep_learning", {"status": "SKIPPED", "reason": "not_post_close"})
        intraday_result = previous_metrics.get("intraday_evolution", {"status": "SKIPPED", "reason": "not_post_close"})
        code_result = previous_metrics.get("code_evolution", {"status": "SKIPPED", "reason": "not_post_close"})
        if "deep_learning" in stage_keys:
            tracker.start("deep_learning")
            try:
                from .deep_learning import train_deep_model
                deep_result = train_deep_model(
                    symbols, cycle_id, float(inputs.get("max_drawdown", 0.15)),
                    bool(inputs.get("auto_promote", True)),
                    int(inputs.get("deep_epochs", 32)),
                )
                tracker.finish("deep_learning", detail=str(deep_result.get("status", "完成")))
            except Exception as exc:
                deep_result = {"status": "FAILED", "error": repr(exc)}
                errors.append({"stage": "deep_learning", "error": repr(exc)})
                tracker.finish("deep_learning", "WARNING", repr(exc))
        if "intraday_evolution" in stage_keys:
            tracker.start("intraday_evolution")
            try:
                from .intraday_strategy import run_intraday_evolution
                intraday_result = run_intraday_evolution(
                    symbols, cycle_id, int(inputs.get("intraday_interval", 5)),
                    float(inputs.get("max_drawdown", 0.15)),
                    bool(inputs.get("auto_promote", True)),
                )
                tracker.finish("intraday_evolution", detail=str(intraday_result.get("status", "完成")))
            except Exception as exc:
                intraday_result = {"status": "FAILED", "error": repr(exc)}
                errors.append({"stage": "intraday_evolution", "error": repr(exc)})
                tracker.finish("intraday_evolution", "WARNING", repr(exc))
        if "code_evolution" in stage_keys:
            tracker.start("code_evolution")
            try:
                from .code_evolution import run_automatic_code_evolution
                code_result = run_automatic_code_evolution(
                    symbols, float(inputs.get("max_drawdown", 0.15)),
                    bool(inputs.get("auto_promote_code", True)),
                )
                tracker.finish("code_evolution", detail=str(code_result.get("status", "完成")))
            except Exception as exc:
                code_result = {"status": "FAILED", "error": repr(exc)}
                errors.append({"stage": "code_evolution", "error": repr(exc)})
                tracker.finish("code_evolution", "WARNING", repr(exc))
        quant_refresh = {"status": "SKIPPED", "reason": "only_after_close"}
        if "quant_portfolios" in stage_keys:
            tracker.start("quant_portfolios")
            from .quant_portfolio import refresh_active_quant_portfolios
            quant_refresh = refresh_active_quant_portfolios(
                learning_cycle_id=cycle_id, trigger_kind="daily_post_close",
                only_mandate_keys=retry_quant_keys,
            )
            if retry_quant_keys is not None:
                prior_results = previous_metrics.get("quant_portfolios", {}).get("results", [])
                merged = {item["mandate_key"]: item for item in prior_results}
                merged.update({item["mandate_key"]: item for item in quant_refresh["results"]})
                quant_refresh["results"] = list(merged.values())
                quant_refresh["updated"] = len(merged)
                quant_refresh["mandates"] = len(merged) + len(quant_refresh["errors"])
            errors.extend({"stage": "quant_portfolios", **item}
                          for item in quant_refresh.get("errors", []))
            tracker.finish("quant_portfolios",
                           "FAILED" if quant_refresh.get("errors") else "SUCCEEDED",
                           f"完成 {quant_refresh.get('updated', 0)}/{quant_refresh.get('mandates', 0)}")
        retry_required = any(item.get("stage") != "sentiment" for item in errors)
        cycle_status = "PARTIAL" if retry_required else ("SUCCESS_WITH_WARNINGS" if errors else "SUCCESS")
        metrics = {"calendar": calendar_result, "market_refresh": market_result,
                   "fundamentals": fundamental_result,
                   "sentiment": sentiment_result,
                   "scoring": {**scored, "deep_scored": deep_scored.get("scored", 0)},
                   "evaluation": evaluation, "next_predictions": predictions,
                   "deep_learning": deep_result, "intraday_evolution": intraday_result,
                   "code_evolution": code_result,
                   "quant_portfolios": quant_refresh,
                   "data_asof": data_asof, "market_date_complete": market_date_complete,
                   "universe_count": len(symbols),
                   "retry_required": retry_required,
                   "research_only": True, "order_execution": False}
        tracker.finalize(
            cycle_status,
            f"本轮完成 · 警告 {len(errors)}",
        )
        with closing(connect()) as conn:
            updated = conn.execute(
                """UPDATE harness_learning_cycles SET status=?,data_asof=?,metrics_json=?,
                   errors_json=?,heartbeat_at=?,worker_token=NULL,finished_at=?
                   WHERE id=? AND worker_token=?""",
                (cycle_status, data_asof, _dump(metrics), _dump(errors), _now(), _now(), cycle_id,
                 worker_token),
            )
            if updated.rowcount != 1:
                raise RuntimeError("continuous learning cycle lease was lost")
            conn.commit()
        return {"cycle_id": cycle_id, "cycle_key": cycle_key, "phase": phase,
                "status": cycle_status, "metrics": metrics, "errors": errors,
                "progress": tracker.progress}
    except Exception as exc:
        errors.append({"stage": "cycle", "error": repr(exc)})
        tracker.finalize("FAILED", repr(exc))
        with closing(connect()) as conn:
            conn.execute(
                """UPDATE harness_learning_cycles SET status='FAILED',errors_json=?,
                   heartbeat_at=?,worker_token=NULL,finished_at=?
                   WHERE id=? AND worker_token=?""",
                (_dump(errors), _now(), _now(), cycle_id, worker_token),
            )
            conn.commit()
        raise
    finally:
        tracker.stop_heartbeat()


def continuous_learning_payload() -> dict:
    initialize()
    with closing(connect()) as conn:
        cycle = conn.execute(
            "SELECT * FROM harness_learning_cycles ORDER BY id DESC LIMIT 1"
        ).fetchone()
        active = conn.execute(
            "SELECT * FROM prediction_model_versions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        evaluation = conn.execute(
            "SELECT * FROM prediction_evaluations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        summary = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN status='SCORED' THEN 1 ELSE 0 END) scored,
                      SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) pending,
                      AVG(CASE WHEN status='SCORED' THEN direction_correct END) accuracy,
                      AVG(CASE WHEN status='SCORED' THEN brier_score END) brier
               FROM daily_predictions"""
        ).fetchone()
        sentiment = conn.execute(
            "SELECT MAX(trade_date) latest_date,SUM(document_count) documents FROM sentiment_daily"
        ).fetchone()
    cycle_item = dict(cycle) if cycle else None
    if cycle_item:
        cycle_item["metrics"] = _load(cycle_item.pop("metrics_json"), {})
        cycle_item["errors"] = _load(cycle_item.pop("errors_json"), [])
        cycle_item["universe"] = _load(cycle_item.pop("universe_json"), [])
        progress = _load(cycle_item.pop("progress_json"), {})
        if not progress:
            finished = cycle_item["status"] in {"SUCCESS", "SUCCESS_WITH_WARNINGS", "PARTIAL", "FAILED"}
            progress = {
                "status": cycle_item["status"],
                "current_stage": None,
                "current_label": "历史轮次无阶段明细",
                "completed": int(finished),
                "total": int(finished),
                "percent": 100.0 if finished else 0.0,
                "stages": [],
                "updated_at": cycle_item.get("finished_at") or cycle_item.get("started_at"),
            }
        heartbeat_age = _timestamp_age_seconds(cycle_item.get("heartbeat_at"))
        progress["heartbeat_age_seconds"] = (
            round(heartbeat_age, 1) if heartbeat_age is not None else None
        )
        progress["stalled"] = bool(
            cycle_item["status"] == "RUNNING" and heartbeat_age is not None
            and heartbeat_age >= CYCLE_LEASE_TIMEOUT_SECONDS
        )
        cycle_item["progress"] = progress
        cycle_item.pop("worker_token", None)
    active_item = dict(active) if active else None
    if active_item:
        active_item["metrics"] = _load(active_item.pop("metrics_json"), {})
        active_item.pop("coefficients_json", None)
        active_item.pop("feature_schema_json", None)
        active_item.pop("gate_json", None)
    evaluation_item = dict(evaluation) if evaluation else None
    if evaluation_item:
        evaluation_item["baseline_metrics"] = _load(evaluation_item.pop("baseline_metrics_json"), {})
        evaluation_item["candidate_metrics"] = _load(evaluation_item.pop("candidate_metrics_json"), {})
        evaluation_item["gate"] = _load(evaluation_item.pop("gate_json"), {})
    return {"last_cycle": cycle_item, "active_model": active_item,
            "last_evaluation": evaluation_item, "predictions": dict(summary),
            "sentiment": dict(sentiment), "research_only": True, "order_execution": False}


def scheduled_cycle_request(now: datetime | None = None) -> dict | None:
    """Return a due cycle request. The scheduler remains idempotent via cycle_key."""
    initialize()
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    symbols = learning_universe(int(os.environ.get("ARGUS_CONTINUOUS_LEARNING_STOCK_LIMIT", "20")))
    latest = _latest_data_date(symbols)
    catchup_key = f"continuous-post_close-{latest}" if latest else ""
    with closing(connect()) as conn:
        last_post_data = conn.execute(
            """SELECT MAX(data_asof) FROM harness_learning_cycles
               WHERE phase='POST_CLOSE' AND status IN ('SUCCESS','SUCCESS_WITH_WARNINGS')"""
        ).fetchone()[0]
        today_pre = conn.execute(
            """SELECT 1 FROM harness_learning_cycles WHERE cycle_key=?
               AND status IN ('SUCCESS','SUCCESS_WITH_WARNINGS')""",
            (f"continuous-pre_open-{current.date().isoformat()}",),
        ).fetchone()
        today_post = conn.execute(
            """SELECT * FROM harness_learning_cycles
               WHERE cycle_key=?""",
            (f"continuous-post_close-{current.date().isoformat()}",),
        ).fetchone()
        catchup_cycle = (conn.execute(
            """SELECT * FROM harness_learning_cycles
               WHERE cycle_key=?""", (catchup_key,),
        ).fetchone() if catchup_key else None)
    catchup_running = False
    if catchup_cycle and catchup_cycle["status"] == "RUNNING":
        heartbeat_age = _timestamp_age_seconds(catchup_cycle["heartbeat_at"], current)
        started_age = _timestamp_age_seconds(catchup_cycle["started_at"], current)
        lease_age = heartbeat_age if heartbeat_age is not None else started_age
        lease_timeout = (CYCLE_LEASE_TIMEOUT_SECONDS if heartbeat_age is not None
                         else int(timedelta(hours=2).total_seconds()))
        catchup_running = lease_age is None or lease_age < lease_timeout
    retry_minutes = max(5, int(os.environ.get("ARGUS_POST_CLOSE_RETRY_MINUTES", "30")))
    catchup_age = (_timestamp_age_seconds(catchup_cycle["finished_at"], current)
                   if catchup_cycle and catchup_cycle["finished_at"] else None)
    catchup_due = catchup_age is None or catchup_age >= retry_minutes * 60
    if (latest and (not last_post_data or str(latest) > str(last_post_data)
                    or (catchup_cycle and _cycle_needs_retry(catchup_cycle))) and
            (current.hour < 9 or str(latest) < current.date().isoformat()) and
            not catchup_running and catchup_due):
        return {"phase": "POST_CLOSE", "cycle_date": latest, "trigger_kind": "scheduler_catchup",
                "stock_limit": len(symbols), "auto_promote": True,
                "retry_if_stale": True}
    minutes = current.hour * 60 + current.minute
    if is_trading_day(current.date()) and 8 * 60 + 45 <= minutes < 9 * 60 + 25 and not today_pre:
        return {"phase": "PRE_OPEN", "cycle_date": current.date().isoformat(),
                "trigger_kind": "scheduler", "stock_limit": len(symbols), "auto_promote": True}
    post_complete = bool(
        today_post and not _cycle_needs_retry(today_post) and today_post["data_asof"] and
        str(today_post["data_asof"]) >= current.date().isoformat()
    )
    retry_age = (_timestamp_age_seconds(today_post["finished_at"], current)
                 if today_post and today_post["finished_at"] else None)
    retry_due = not today_post or retry_age is None or retry_age >= retry_minutes * 60
    if (is_trading_day(current.date()) and minutes >= 15 * 60 + 1 and
            not post_complete and retry_due):
        return {"phase": "POST_CLOSE", "cycle_date": current.date().isoformat(),
                "trigger_kind": "scheduler", "stock_limit": len(symbols),
                "auto_promote": True, "retry_if_stale": True}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one scheduled A-share learning cycle")
    parser.add_argument("--phase", choices=("PRE_OPEN", "POST_CLOSE", "BACKFILL"), required=True)
    parser.add_argument("--stock-limit", type=int, default=20)
    parser.add_argument("--max-drawdown", type=float, default=0.15)
    parser.add_argument("--trigger-kind", default="command_line")
    args = parser.parse_args()
    today = datetime.now(SHANGHAI).date()
    if args.phase != "BACKFILL" and not is_trading_day(today):
        print(_dump({"status": "SKIPPED_NON_TRADING_DAY", "date": today.isoformat()}))
        return 0
    result = run_continuous_learning_cycle({
        "phase": args.phase, "stock_limit": args.stock_limit,
        "max_drawdown": args.max_drawdown, "trigger_kind": args.trigger_kind,
        "refresh_data": args.phase != "BACKFILL", "collect_sentiment": True,
        "refresh_calendar": True, "auto_promote": True,
        "retry_if_stale": args.phase == "POST_CLOSE",
    })
    print(_dump(result))
    return 0 if result["status"] in {"SUCCESS", "ALREADY_COMPLETED", "ALREADY_RUNNING"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
