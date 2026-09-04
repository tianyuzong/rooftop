"""Persist research signals and deliver opt-in notifications.

Signals are review artifacts only. No function in this module can connect to a
broker, create an order, or mark a signal as executed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable

from .alerts import outbox_payload, queue_alert, send_pending, smtp_status
from .db import connect


ACTIONABLE = {"BUY", "SELL", "REBALANCE"}
SIGNAL_ACTIONS = ACTIONABLE | {"HOLD", "WATCH"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


def _confidence(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, min(1.0, number / 100 if number > 1 else number))


def _signal_key(mandate_key: str, version_key: str, symbol: str, action: str,
                shares: int | None, reference_price: float | None, data_asof: str) -> str:
    raw = "|".join(map(str, (
        mandate_key, version_key, symbol, action, shares or 0,
        round(float(reference_price or 0.0), 4), data_asof,
    )))
    return "signal-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _latest_by_action(conn, mandate_id: int, actions: set[str]) -> dict[str, dict]:
    placeholders = ",".join("?" for _ in actions)
    rows = conn.execute(
        f"""SELECT s.* FROM research_signals s JOIN (
                SELECT symbol,MAX(id) latest_id FROM research_signals
                WHERE mandate_id=? AND status!='SUPERSEDED'
                  AND action IN ({placeholders}) GROUP BY symbol
              ) latest ON latest.latest_id=s.id""",
        (int(mandate_id), *sorted(actions)),
    ).fetchall()
    return {str(row["symbol"]): dict(row) for row in rows}


def _signal_row(item: dict, action: str, mandate: dict, version: dict,
                recommendation: dict, previous: dict | None = None) -> dict:
    previous = previous or {}
    symbol = str(item.get("symbol") or previous.get("symbol") or "")
    name = str(item.get("name") or previous.get("name") or symbol)
    data_asof = str(recommendation.get("data_asof") or "")
    version_key = str(version.get("version_key") or f"unversioned-{data_asof}")
    reference_price = item.get("reference_price")
    if reference_price is None:
        reference_price = previous.get("reference_price")
    shares = item.get("shares")
    if shares is None:
        shares = previous.get("shares")
    evidence = {
        "data_asof": data_asof,
        "version_key": version_key,
        "version_status": version.get("status"),
        "model_version": recommendation.get("model_version"),
        "profile": recommendation.get("profile"),
        "profile_label": recommendation.get("profile_label"),
        "gate": version.get("gate") or {},
        "expectation": recommendation.get("expectation") or {},
        "holdout_metrics": recommendation.get("holdout_metrics") or {},
        "research_only": True,
        "order_execution": False,
    }
    if action == "SELL":
        rationale = {"summary": "该股票不再出现在最新正式研究组合中",
                     "review_required": True}
        invalidation = "若后续正式版本重新通过趋势、基本面、模型和风险门禁，可重新评估。"
    elif action == "WATCH":
        rationale = {
            "summary": "当前仅达到观察或快照条件，尚未成为正式研究仓位",
            "blockers": item.get("blockers") or item.get("rejection_reasons") or [],
            "review_required": True,
        }
        invalidation = "盘后样本外门禁未通过、数据覆盖下降或触发风险条件时停止关注。"
    else:
        rationale = {
            "summary": {
                "BUY": "首次进入最新正式研究组合",
                "REBALANCE": "正式研究组合的参考数量或权重发生变化",
                "HOLD": "仍在最新正式研究组合中，参考数量与权重未发生实质变化",
            }[action],
            "composite_score": item.get("composite_score"),
            "probability_up": item.get("probability_up"),
            "fundamental_score": item.get("fundamental_score"),
            "review_required": True,
        }
        invalidation = (
            f"价格触及参考止损 {item.get('stop_price')}，或趋势、基本面、模型概率、"
            "流动性及组合回撤门禁任一失效时重新评估。"
        )
    return {
        "signal_key": _signal_key(
            str(mandate["mandate_key"]), version_key, symbol, action,
            int(shares or 0), float(reference_price or 0.0), data_asof,
        ),
        "mandate_id": int(mandate["id"]), "version_key": version_key,
        "validation_status": str(version.get("status") or "UNKNOWN"),
        "action": action, "symbol": symbol, "name": name,
        "data_asof": data_asof, "reference_price": reference_price,
        "shares": int(shares) if shares is not None else None,
        "weight": item.get("weight"), "stop_price": item.get("stop_price"),
        "take_profit_price": item.get("take_profit_price"),
        "score": item.get("composite_score"),
        "confidence": _confidence(item.get("confidence") or item.get("probability_up")),
        "rationale": rationale, "evidence": evidence,
        "invalidation": invalidation,
        "previous_signal_key": previous.get("signal_key") if previous else None,
    }


def _insert_signal(conn, row: dict) -> tuple[int | None, bool]:
    cursor = conn.execute(
        """INSERT OR IGNORE INTO research_signals
           (signal_key,mandate_id,version_key,validation_status,action,symbol,name,
            status,data_asof,reference_price,shares,weight,stop_price,take_profit_price,
            score,confidence,rationale_json,evidence_json,invalidation,previous_signal_key,
            research_only,order_execution,generated_at)
           VALUES(?,?,?,?,?,?,?,'NEW',?,?,?,?,?,?,?,?,?,?,?,?,1,0,?)""",
        (row["signal_key"], row["mandate_id"], row["version_key"],
         row["validation_status"], row["action"], row["symbol"], row["name"],
         row["data_asof"], row["reference_price"], row["shares"], row["weight"],
         row["stop_price"], row["take_profit_price"], row["score"], row["confidence"],
         _dump(row["rationale"]), _dump(row["evidence"]), row["invalidation"],
         row["previous_signal_key"], _now()),
    )
    if cursor.rowcount != 1:
        existing = conn.execute(
            "SELECT id FROM research_signals WHERE signal_key=?", (row["signal_key"],)
        ).fetchone()
        return (int(existing[0]) if existing else None), False
    signal_id = int(cursor.lastrowid)
    if row["previous_signal_key"]:
        conn.execute(
            "UPDATE research_signals SET status='SUPERSEDED' WHERE signal_key=? AND id!=?",
            (row["previous_signal_key"], signal_id),
        )
    return signal_id, True


def _queue_for_subscribers(signal: dict, signal_id: int,
                           conn_factory: Callable = connect) -> int:
    with closing(conn_factory()) as conn:
        subscriptions = conn.execute(
            """SELECT * FROM signal_subscriptions
               WHERE enabled=1 AND (mandate_id IS NULL OR mandate_id=?) ORDER BY id""",
            (signal["mandate_id"],),
        ).fetchall()
    queued = 0
    for subscription in subscriptions:
        event_kinds = set(_load(subscription["event_kinds_json"], []))
        confidence = float(signal.get("confidence") or 0.0)
        if signal["action"] not in event_kinds or confidence < float(subscription["minimum_confidence"]):
            continue
        subject = f"[Rooftop] {signal['action']} {signal['name']} {signal['symbol']} 人工复核提醒"
        body = (
            f"信号：{signal['action']}\n股票：{signal['name']} {signal['symbol']}\n"
            f"数据截至：{signal['data_asof']}\n参考价：{signal.get('reference_price') or '—'}\n"
            f"参考数量：{signal.get('shares') or 0} 股\n止损参考：{signal.get('stop_price') or '—'}\n"
            f"止盈参考：{signal.get('take_profit_price') or '—'}\n"
            f"原因：{signal['rationale'].get('summary', '')}\n"
            f"失效条件：{signal['invalidation']}\n\n"
            "仅供研究和人工复核，不构成收益承诺；系统不会连接券商或自动下单。"
        )
        if queue_alert(
            f"{signal['signal_key']}:{subscription['id']}", subject, body,
            channel=subscription["channel"], target=subscription["target"],
            signal_id=signal_id, subscription_id=int(subscription["id"]),
            metadata={"signal_key": signal["signal_key"], "action": signal["action"]},
            conn_factory=conn_factory,
        ):
            queued += 1
    return queued


def materialize_decision_signals(decision: dict, *, dispatch: bool = False,
                                 conn_factory: Callable = connect) -> dict:
    mandate = decision.get("mandate") or {}
    result = decision.get("result") or {}
    recommendation = result.get("recommendation") or {}
    version = decision.get("version") or result.get("version") or {}
    if not mandate.get("id") or not recommendation.get("data_asof") or not version.get("version_key"):
        return {"created": 0, "queued": 0, "delivery": {"status": "NOT_APPLICABLE"},
                "research_only": True, "order_execution": False}
    formal = str(version.get("status")) == "ACTIVE"
    positions = list(recommendation.get("positions") or [])
    watchlist = list(recommendation.get("research_recommendations") or [])
    created: list[tuple[int, dict]] = []
    with closing(conn_factory()) as conn:
        already_published = conn.execute(
            "SELECT 1 FROM research_signals WHERE mandate_id=? AND version_key=? LIMIT 1",
            (int(mandate["id"]), str(version["version_key"])),
        ).fetchone()
        if already_published:
            return {"created": 0, "queued": 0,
                    "delivery": {"status": "ALREADY_PUBLISHED", "sent": 0},
                    "signals": [], "research_only": True, "order_execution": False}
        previous_states = _latest_by_action(conn, int(mandate["id"]), {"BUY", "REBALANCE", "HOLD", "SELL"})
        previous_watches = _latest_by_action(conn, int(mandate["id"]), {"WATCH"})
        current_symbols = {str(item.get("symbol")) for item in positions}
        for item in positions:
            symbol = str(item.get("symbol"))
            previous = previous_states.get(symbol) if formal else previous_watches.get(symbol)
            if not formal:
                action = "WATCH"
            elif previous and previous.get("action") in {"BUY", "REBALANCE", "HOLD"}:
                same_shares = int(previous.get("shares") or 0) == int(item.get("shares") or 0)
                same_weight = abs(float(previous.get("weight") or 0) - float(item.get("weight") or 0)) < .005
                action = "HOLD" if same_shares and same_weight else "REBALANCE"
            else:
                action = "BUY"
            row = _signal_row(item, action, mandate, version, recommendation, previous)
            signal_id, inserted = _insert_signal(conn, row)
            if inserted and signal_id:
                created.append((signal_id, row))
        if formal:
            for symbol, previous in previous_states.items():
                if previous.get("action") not in {"BUY", "REBALANCE", "HOLD"} or symbol in current_symbols:
                    continue
                row = _signal_row({}, "SELL", mandate, version, recommendation, previous)
                signal_id, inserted = _insert_signal(conn, row)
                if inserted and signal_id:
                    created.append((signal_id, row))
        for item in watchlist:
            symbol = str(item.get("symbol"))
            if symbol in current_symbols:
                continue
            row = _signal_row(item, "WATCH", mandate, version, recommendation,
                              previous_watches.get(symbol))
            signal_id, inserted = _insert_signal(conn, row)
            if inserted and signal_id:
                created.append((signal_id, row))
        conn.commit()
    queued = sum(_queue_for_subscribers(row, signal_id, conn_factory) for signal_id, row in created)
    delivery = send_pending(conn_factory=conn_factory) if dispatch and queued else {
        "status": "QUEUED" if queued else "NOT_REQUESTED", "sent": 0,
    }
    return {"created": len(created), "queued": queued, "delivery": delivery,
            "signals": [{"id": signal_id, **row} for signal_id, row in created],
            "research_only": True, "order_execution": False}


def signal_feed(mandate_key: str | None = None, limit: int = 100,
                conn_factory: Callable = connect) -> dict:
    where, params = [], []
    if mandate_key:
        where.append("m.mandate_key=?")
        params.append(str(mandate_key))
    sql = """SELECT s.*,m.mandate_key,m.name mandate_name FROM research_signals s
             JOIN quant_mandates m ON m.id=s.mandate_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with closing(conn_factory()) as conn:
        rows = [dict(row) for row in conn.execute(sql, params)]
        unread = conn.execute(
            """SELECT COUNT(*) FROM research_signals
               WHERE status='NEW' AND action IN ('BUY','SELL','REBALANCE')"""
        ).fetchone()[0]
    for row in rows:
        row["rationale"] = _load(row.pop("rationale_json"), {})
        row["evidence"] = _load(row.pop("evidence_json"), {})
        row["research_only"] = bool(row["research_only"])
        row["order_execution"] = False
    return {"signals": rows, "unread_actionable": int(unread),
            "research_only": True, "order_execution": False}


def update_signal_status(signal_id: int, action: str,
                         conn_factory: Callable = connect) -> dict:
    normalized = str(action).upper()
    if normalized not in {"ACKNOWLEDGED", "DISMISSED"}:
        raise ValueError("信号只能标记为 ACKNOWLEDGED 或 DISMISSED")
    stamp = _now()
    column = "acknowledged_at" if normalized == "ACKNOWLEDGED" else "dismissed_at"
    with closing(conn_factory()) as conn:
        cursor = conn.execute(
            f"UPDATE research_signals SET status=?,{column}=? WHERE id=? AND status!='SUPERSEDED'",
            (normalized, stamp, int(signal_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError("信号不存在或已被新版本替代")
        conn.commit()
    return {"id": int(signal_id), "status": normalized, "updated_at": stamp,
            "manual_review_only": True, "order_execution": False}


def create_subscription(payload: dict, conn_factory: Callable = connect) -> dict:
    channel = str(payload.get("channel") or "EMAIL").upper()
    if channel != "EMAIL":
        raise ValueError("MVP 当前只支持 EMAIL；飞书和微信保留为后续适配器")
    target = str(payload.get("target") or "").strip() or None
    if target and ("@" not in target or len(target) > 254):
        raise ValueError("邮件地址格式无效")
    mandate_id = payload.get("mandate_id")
    kinds = payload.get("event_kinds") or sorted(ACTIONABLE)
    if not isinstance(kinds, list) or not set(kinds).issubset(SIGNAL_ACTIONS):
        raise ValueError("订阅事件类型无效")
    minimum = float(payload.get("minimum_confidence", 0) or 0)
    if not 0 <= minimum <= 1:
        raise ValueError("最低置信度必须在 0 到 1 之间")
    stamp = _now()
    with closing(conn_factory()) as conn:
        if mandate_id is not None and not conn.execute(
            "SELECT 1 FROM quant_mandates WHERE id=?", (int(mandate_id),)
        ).fetchone():
            raise ValueError("订阅对应的投资约束不存在")
        cursor = conn.execute(
            """INSERT INTO signal_subscriptions
               (name,mandate_id,channel,target,enabled,event_kinds_json,
                minimum_confidence,created_at,updated_at)
               VALUES(?,?,?,?,1,?,?,?,?)""",
            (str(payload.get("name") or "Rooftop 邮件提醒").strip()[:100],
             int(mandate_id) if mandate_id is not None else None, channel, target,
             _dump(sorted(set(kinds))), minimum, stamp, stamp),
        )
        conn.commit()
        subscription_id = int(cursor.lastrowid)
    return {"id": subscription_id, "status": "ACTIVE", "channel": channel,
            "target": target, "event_kinds": sorted(set(kinds)),
            "minimum_confidence": minimum, "order_execution": False}


def set_subscription_enabled(subscription_id: int, enabled: bool,
                             conn_factory: Callable = connect) -> dict:
    with closing(conn_factory()) as conn:
        cursor = conn.execute(
            "UPDATE signal_subscriptions SET enabled=?,updated_at=? WHERE id=?",
            (1 if enabled else 0, _now(), int(subscription_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError("通知订阅不存在")
        conn.commit()
    return {"id": int(subscription_id), "enabled": bool(enabled), "order_execution": False}


def subscription_payload(conn_factory: Callable = connect) -> dict:
    with closing(conn_factory()) as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT s.*,m.mandate_key,m.name mandate_name FROM signal_subscriptions s
               LEFT JOIN quant_mandates m ON m.id=s.mandate_id ORDER BY s.id DESC"""
        )]
    for row in rows:
        row["event_kinds"] = _load(row.pop("event_kinds_json"), [])
        row["enabled"] = bool(row["enabled"])
    return {"subscriptions": rows, "smtp": smtp_status(),
            "supported_channels": ["EMAIL"], "future_channels": ["FEISHU", "WECHAT"],
            "research_only": True, "order_execution": False}


def queue_test_email(subscription_id: int | None = None,
                     conn_factory: Callable = connect) -> dict:
    target = None
    if subscription_id is not None:
        with closing(conn_factory()) as conn:
            row = conn.execute(
                "SELECT * FROM signal_subscriptions WHERE id=? AND enabled=1",
                (int(subscription_id),),
            ).fetchone()
        if not row:
            raise ValueError("启用中的通知订阅不存在")
        target = row["target"]
    target = target or os.environ.get("ARGUS_ALERT_TO")
    queued = queue_alert(
        f"test:{uuid.uuid4().hex}", "[Rooftop] 邮件通道测试",
        "Rooftop 邮件提醒通道测试成功。系统只发送研究提醒，不连接券商或自动下单。",
        target=target, subscription_id=subscription_id,
        metadata={"kind": "CHANNEL_TEST"}, conn_factory=conn_factory,
    )
    return {"queued": queued, "delivery": send_pending(conn_factory=conn_factory),
            "order_execution": False}


def notification_payload(conn_factory: Callable = connect) -> dict:
    return {"feed": signal_feed(conn_factory=conn_factory),
            "subscriptions": subscription_payload(conn_factory),
            "outbox": outbox_payload(conn_factory=conn_factory),
            "research_only": True, "order_execution": False}
