"""Preserve raw data while selecting a continuous, comparable adjusted-price window."""
from datetime import date
import math


def contiguous_daily_window(rows: list[dict]) -> tuple[list[dict], dict]:
    """Never treat a multi-month data gap as one trading day or bridge unit changes.

    A long suspension is also a boundary: its missing trading observations cannot
    count toward daily-return calibration. We retain the latest segment intact;
    no returns are winsorized and the original database is not modified.
    """
    boundary = 0
    breaks = []
    for index, row in enumerate(rows):
        stamp = str(row.get("trade_date") or row.get("time") or "")[:10]
        try:
            current_date = date.fromisoformat(stamp)
            price = float(row["close"])
            if not math.isfinite(price) or price <= 0:
                raise ValueError("invalid price")
        except (ValueError, TypeError, KeyError):
            boundary = index + 1
            breaks.append({"date": stamp, "reason": "INVALID_PRICE_OR_DATE"})
            continue
        if index == 0 or index <= boundary:
            continue
        previous = rows[index - 1]
        previous_date = date.fromisoformat(str(previous.get("trade_date") or previous.get("time"))[:10])
        gap = (current_date - previous_date).days
        ratio = price / float(previous["close"])
        if gap > 35 or gap <= 0 or ratio > 3 or ratio < 1 / 3:
            boundary = index
            breaks.append({"date": stamp, "previous_date": previous_date.isoformat(),
                           "gap_days": gap, "price_ratio": round(ratio, 6),
                           "reason": "HISTORY_GAP" if gap > 35 else "ADJUSTED_PRICE_DISCONTINUITY"})
    retained = rows[boundary:]
    return retained, {"status": "RECENT_CONTIGUOUS_WINDOW" if boundary else "CONTINUOUS",
                      "original_rows": len(rows), "retained_rows": len(retained),
                      "excluded_rows": boundary, "breaks": breaks,
                      "raw_data_preserved": True,
                      "warning": (f"检测到历史断层或复权价格不连续，研究仅使用最近连续的 {len(retained)} 根日线；"
                                  f"此前 {boundary} 根原始记录保留，但不跨断层计算收益。") if boundary else None}
