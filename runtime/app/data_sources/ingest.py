"""Audited AKShare ingestion with a local-cache safety net.

The adapter writes the upstream response before normalization.  If a network or
schema error occurs, the existing database remains untouched and the failure is
recorded for maintenance.  It never silently turns cached data into "live" data.
"""

import argparse
import hashlib
import json
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from ..db import DATA_LAKE, connect, ensure_data_lake, initialize

HOLDINGS = {
    "512400": "有色ETF南方",
    "562500": "机器人ETF华夏",
}
REQUIRED_COLUMNS = {"日期", "开盘", "收盘", "最高", "最低", "成交量"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_run(conn, source_id: int, symbol: str) -> int:
    return conn.execute(
        """INSERT INTO ingestion_runs(source_id,dataset,asset_symbol,started_at,status)
           VALUES(?,?,?,?,?)""",
        (source_id, "fund_etf_daily", symbol, _now(), "RUNNING"),
    ).lastrowid


def _failure_copy(symbol: str, error: Exception, source: str = "akshare") -> str:
    target = DATA_LAKE / "dead_letter" / f"{source}_{symbol}_{datetime.now():%Y%m%dT%H%M%S}.json"
    target.write_text(json.dumps({"source": source, "symbol": symbol, "error": repr(error), "captured_at": _now()}, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def fetch_akshare(symbol: str, start_date: str, end_date: str):
    import akshare as ak

    frame = ak.fund_etf_hist_em(
        symbol=symbol, period="daily", start_date=start_date,
        end_date=end_date, adjust="",
    )
    missing = REQUIRED_COLUMNS.difference(frame.columns)
    if missing:
        raise ValueError(f"AKShare schema changed; missing columns: {sorted(missing)}")
    if frame.empty:
        raise ValueError("AKShare returned no rows")
    return frame


def fetch_baostock(symbol: str, start_date: str, end_date: str):
    import baostock as bs
    import pandas as pd

    code = f"sh.{symbol}" if symbol.startswith(("5", "6", "9")) else f"sz.{symbol}"
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    try:
        result = bs.query_history_k_data_plus(
            code, "date,open,high,low,close,volume,amount",
            start_date=f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}",
            end_date=f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}",
            frequency="d", adjustflag="3",
        )
        rows = []
        while result.error_code == "0" and result.next():
            rows.append(result.get_row_data())
        if result.error_code != "0":
            raise RuntimeError(f"BaoStock query failed: {result.error_msg}")
        frame = pd.DataFrame(rows, columns=["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"])
        if frame.empty:
            raise ValueError("BaoStock returned no rows")
        return frame
    finally:
        bs.logout()


def _price_rows(frame, asset_id: int, source_id: int, captured_at: str, raw_path: Path):
    rows = []
    for record in frame.to_dict("records"):
        trade_date = record["日期"]
        if hasattr(trade_date, "isoformat"):
            trade_date = trade_date.isoformat()
        rows.append((
            asset_id, str(trade_date), float(record["开盘"]), float(record["最高"]),
            float(record["最低"]), float(record["收盘"]), float(record["成交量"]),
            float(record.get("成交额") or 0), source_id, captured_at, str(raw_path), 0,
        ))
    return rows


def _upsert_rows(conn, asset_id: int, rows) -> None:
    conn.execute("DELETE FROM prices WHERE asset_id=? AND is_demo=1", (asset_id,))
    conn.executemany(
        """INSERT INTO prices(asset_id,trade_date,open,high,low,close,volume,amount,source_id,captured_at,raw_path,is_demo)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(asset_id,trade_date) DO UPDATE SET
             open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
             volume=excluded.volume,amount=excluded.amount,source_id=excluded.source_id,
             captured_at=excluded.captured_at,raw_path=excluded.raw_path,is_demo=0""", rows,
    )


def ingest_akshare(symbol: str, start_date: str, end_date: str) -> dict:
    initialize()
    ensure_data_lake()
    with closing(connect()) as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE code='akshare'").fetchone()
        asset = conn.execute("SELECT * FROM assets WHERE symbol=?", (symbol,)).fetchone()
        if not asset:
            raise KeyError(f"unknown asset: {symbol}")
        run_id = _start_run(conn, source["id"], symbol)
        conn.commit()
        try:
            frame = fetch_akshare(symbol, start_date, end_date)
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            raw_path = DATA_LAKE / "raw" / "market" / "akshare" / symbol / f"daily_{stamp}.csv"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_csv(raw_path, index=False, encoding="utf-8-sig")
            checksum = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            captured_at = _now()
            rows = _price_rows(frame, asset["id"], source["id"], captured_at, raw_path)
            _upsert_rows(conn, asset["id"], rows)
            latest = rows[-1][5]
            conn.execute("UPDATE assets SET data_status='REAL' WHERE id=?", (asset["id"],))
            conn.execute(
                """UPDATE positions SET current_price=?,highest_since_entry=MAX(highest_since_entry,?)
                   WHERE asset_id=?""", (latest, latest, asset["id"]),
            )
            conn.execute(
                """UPDATE ingestion_runs SET finished_at=?,status='SUCCESS',row_count=?,raw_path=? WHERE id=?""",
                (captured_at, len(rows), str(raw_path), run_id),
            )
            conn.execute(
                """UPDATE data_sources SET health_status='HEALTHY',last_success_at=?,last_error=NULL WHERE id=?""",
                (captured_at, source["id"]),
            )
            conn.commit()
            return {"symbol": symbol, "status": "SUCCESS", "rows": len(rows),
                    "from": rows[0][1], "to": rows[-1][1], "raw_path": str(raw_path),
                    "sha256": checksum}
        except Exception as exc:
            failed_at = _now()
            artifact = _failure_copy(symbol, exc)
            cached_rows = []
            cached_path = None
            cached = sorted((DATA_LAKE / "raw" / "market" / "akshare" / symbol).glob("daily_*.csv"))
            if cached:
                import pandas as pd
                cached_path = cached[-1]
                cached_frame = pd.read_csv(cached_path)
                missing = REQUIRED_COLUMNS.difference(cached_frame.columns)
                if not missing and not cached_frame.empty:
                    cached_at = datetime.fromtimestamp(cached_path.stat().st_mtime, timezone.utc).isoformat()
                    cached_rows = _price_rows(cached_frame, asset["id"], source["id"], cached_at, cached_path)
                    _upsert_rows(conn, asset["id"], cached_rows)
                    conn.execute("UPDATE assets SET data_status='STALE_CACHE' WHERE id=?", (asset["id"],))
                    conn.execute(
                        "UPDATE positions SET current_price=?,highest_since_entry=MAX(highest_since_entry,?) WHERE asset_id=?",
                        (cached_rows[-1][5], cached_rows[-1][5], asset["id"]),
                    )
            conn.execute(
                "UPDATE ingestion_runs SET finished_at=?,status=?,row_count=?,error=?,raw_path=? WHERE id=?",
                (failed_at, "FALLBACK_CACHE" if cached_rows else "FAILED", len(cached_rows), repr(exc),
                 str(cached_path) if cached_path else artifact, run_id),
            )
            conn.execute(
                "UPDATE data_sources SET health_status='DEGRADED',last_error_at=?,last_error=? WHERE id=?",
                (failed_at, repr(exc), source["id"]),
            )
            conn.commit()
            return {"symbol": symbol, "status": "FALLBACK_CACHE" if cached_rows else "FAILED",
                    "rows": len(cached_rows), "error": repr(exc), "fallback": "local_cache",
                    "cached_path": str(cached_path) if cached_path else None, "dead_letter": artifact}


def ingest_baostock(symbol: str, start_date: str, end_date: str) -> dict:
    initialize()
    with closing(connect()) as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE code='baostock'").fetchone()
        asset = conn.execute("SELECT * FROM assets WHERE symbol=?", (symbol,)).fetchone()
        run_id = _start_run(conn, source["id"], symbol)
        conn.commit()
        try:
            frame = fetch_baostock(symbol, start_date, end_date)
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            raw_path = DATA_LAKE / "raw" / "market" / "baostock" / symbol / f"daily_{stamp}.csv"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_csv(raw_path, index=False, encoding="utf-8-sig")
            captured_at = _now()
            overlap = 0
            max_diff = None
            ak_cached = sorted((DATA_LAKE / "raw" / "market" / "akshare" / symbol).glob("daily_*.csv"))
            if ak_cached:
                import pandas as pd
                ak_frame = pd.read_csv(ak_cached[-1])
                left = ak_frame.loc[:, ["日期", "开盘", "最高", "最低", "收盘"]].copy()
                right = frame.loc[:, ["日期", "开盘", "最高", "最低", "收盘"]].copy()
                left["日期"] = left["日期"].astype(str)
                right["日期"] = right["日期"].astype(str)
                merged = left.merge(right, on="日期", suffixes=("_ak", "_bs"))
                overlap = len(merged)
                if overlap:
                    columns = ("开盘", "最高", "最低", "收盘")
                    max_diff = max(float((merged[f"{col}_ak"].astype(float) - merged[f"{col}_bs"].astype(float)).abs().max()) for col in columns)
                    conn.execute(
                        """INSERT INTO data_quality_checks(asset_symbol,check_name,source_a,source_b,status,metric,details_json,checked_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (symbol, "OHLC_OVERLAP", "akshare", "baostock", "PASS" if max_diff <= .001 else "FAIL",
                         max_diff, json.dumps({"overlap_rows": overlap}, ensure_ascii=False), captured_at),
                    )
                    if max_diff > .001:
                        raise ValueError(f"cross-source OHLC mismatch: max absolute difference {max_diff}")
            rows = _price_rows(frame, asset["id"], source["id"], captured_at, raw_path)
            _upsert_rows(conn, asset["id"], rows)
            latest = rows[-1][5]
            requested_start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
            partial = rows[0][1] > requested_start
            conn.execute("UPDATE assets SET data_status=? WHERE id=?",
                         ("REAL_BACKUP_PARTIAL" if partial else "REAL_BACKUP", asset["id"]))
            conn.execute("UPDATE positions SET current_price=?,highest_since_entry=MAX(highest_since_entry,?) WHERE asset_id=?",
                         (latest, latest, asset["id"]))
            conn.execute("UPDATE ingestion_runs SET finished_at=?,status='SUCCESS',row_count=?,raw_path=? WHERE id=?",
                         (captured_at, len(rows), str(raw_path), run_id))
            conn.execute("""UPDATE data_sources SET enabled=1,health_status=?,last_success_at=?,
                            last_error=NULL WHERE id=?""",
                         ("LIMITED" if partial else "HEALTHY", captured_at, source["id"]))
            conn.commit()
            status = "SUCCESS_BACKUP_PARTIAL" if partial else "SUCCESS_BACKUP"
            return {"symbol": symbol, "status": status, "source": "baostock",
                    "rows": len(rows), "from": rows[0][1], "to": rows[-1][1], "raw_path": str(raw_path),
                    "cross_source": {"overlap_rows": overlap, "max_ohlc_abs_diff": max_diff}}
        except Exception as exc:
            failed_at = _now()
            artifact = _failure_copy(symbol, exc, "baostock")
            conn.execute("UPDATE ingestion_runs SET finished_at=?,status='FAILED',error=?,raw_path=? WHERE id=?",
                         (failed_at, repr(exc), artifact, run_id))
            conn.execute("UPDATE data_sources SET health_status='DEGRADED',last_error_at=?,last_error=? WHERE id=?",
                         (failed_at, repr(exc), source["id"]))
            conn.commit()
            return {"symbol": symbol, "status": "FAILED", "source": "baostock", "error": repr(exc)}


def ingest_holdings(years: int = 3, symbols: Iterable[str] = HOLDINGS) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=366 * years + 7)
    results = []
    for symbol in symbols:
        primary = ingest_akshare(symbol, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        if primary["status"] == "SUCCESS":
            results.append(primary)
            continue
        backup = ingest_baostock(symbol, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        if backup["status"] in {"SUCCESS_BACKUP", "SUCCESS_BACKUP_PARTIAL"}:
            backup["primary_failure"] = primary.get("error")
            results.append(backup)
        else:
            primary["backup_failure"] = backup.get("error")
            results.append(primary)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest free daily ETF data into the local Argus data lake")
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--symbol", action="append", choices=sorted(HOLDINGS))
    args = parser.parse_args()
    results = ingest_holdings(args.years, args.symbol or HOLDINGS)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] in {"SUCCESS", "SUCCESS_BACKUP", "SUCCESS_BACKUP_PARTIAL", "FALLBACK_CACHE"} for item in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
