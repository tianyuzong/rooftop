"""Generate review alerts from discipline rules; never place orders."""

import json
from contextlib import closing
from datetime import date

from .alerts import queue_alert, send_pending
from .analytics import calculate_risk_lines, evaluate_discipline
from .db import connect, initialize


def run_alert_cycle(send: bool = False) -> dict:
    initialize()
    queued = []
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT a.symbol,a.name,a.market,a.asset_type,p.cost_price,p.current_price,p.highest_since_entry
               FROM positions p JOIN assets a ON a.id=p.asset_id
               WHERE p.verification_status='USER_CONFIRMED'
                 AND p.valuation_status='AVAILABLE' AND p.current_price>0"""
        ).fetchall()
    for row in rows:
        lines = calculate_risk_lines(row["cost_price"], row["current_price"], row["highest_since_entry"], row["market"], row["asset_type"])
        discipline = evaluate_discipline(row["current_price"], lines)
        if discipline["action"] == "HOLD_DISCIPLINE":
            continue
        dedupe = f"{date.today().isoformat()}:{row['symbol']}:{discipline['action']}"
        body = (f"（模型输出）{row['name']} {row['symbol']}：{discipline['reason']}\n"
                f"当前价 {row['current_price']}，成本 {lines['cost']}，止损 {lines['stop_loss']}，"
                f"止盈 {lines['take_profit']}。\n仅为人工复核提醒，不构成投资建议，系统不会下单。")
        if queue_alert(dedupe, f"[Argus] {row['symbol']} {discipline['action']} 复核提醒", body):
            queued.append({"symbol": row["symbol"], "action": discipline["action"]})
    delivery = send_pending() if send else {"status": "NOT_REQUESTED", "sent": 0}
    return {"queued": queued, "queued_count": len(queued), "delivery": delivery}


if __name__ == "__main__":
    print(json.dumps(run_alert_cycle(send=False), ensure_ascii=False, indent=2))
