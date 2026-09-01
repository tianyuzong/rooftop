"""Local, user-confirmed portfolio snapshots. Broker order execution is out of scope."""

import csv
import hashlib
import io
import json
import math
import re
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .db import connect
from .data_sources.market import normalize_symbol


MAX_POSITIONS = 200
MAX_CSV_CHARS = 1_000_000
PREVIEW_TTL_HOURS = 24

_HEADER_ALIASES = {
    "symbol": {"symbol", "code", "股票代码", "证券代码", "代码"},
    "name": {"name", "股票名称", "证券名称", "名称"},
    "quantity": {"quantity", "qty", "shares", "数量", "持仓数量", "持有数量"},
    "cost_price": {"costprice", "cost", "成本价", "持仓成本", "平均成本", "成本"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _header_key(value) -> str:
    return re.sub(r"[\s_()（）/\-]+", "", str(value or "").lstrip("\ufeff").lower())


def _safe_filename(value) -> str | None:
    text = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(character for character in text if character.isprintable()).strip()
    return text[:120] or None


def _decimal(value, field: str, row_number: int) -> Decimal:
    text = str(value or "").strip().replace(",", "")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"第 {row_number} 行{field}不是有效数字") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"第 {row_number} 行{field}必须大于 0")
    return number


def _resolve_position(conn, raw: dict, row_number: int) -> dict:
    token = str(raw.get("symbol") or raw.get("code") or "").strip()
    supplied_name = str(raw.get("name") or "").strip()
    code_match = re.fullmatch(
        r"(?:SH|SZ|BJ)?[.]?(\d{6})(?:[.](?:SH|SZ|BJ))?", token.upper()
    )
    if code_match:
        symbol = normalize_symbol(code_match.group(1))
        local = conn.execute(
            "SELECT name FROM assets WHERE symbol=?", (symbol,)
        ).fetchone()
        if not local:
            local = conn.execute(
                """SELECT asset_name AS name FROM quote_snapshots
                   WHERE asset_symbol=?
                   ORDER BY julianday(captured_at) DESC,observed_at DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
        name = supplied_name or (local["name"] if local else symbol)
    else:
        if not token:
            raise ValueError(f"第 {row_number} 行缺少股票代码")
        local = conn.execute(
            """SELECT symbol,name FROM assets WHERE name=?
               UNION ALL
               SELECT asset_symbol AS symbol,asset_name AS name FROM quote_snapshots
               WHERE asset_name=? LIMIT 1""",
            (token, token),
        ).fetchone()
        if not local:
            raise ValueError(f"第 {row_number} 行无法识别“{token}”，请使用 6 位 A 股代码")
        symbol, name = normalize_symbol(local["symbol"]), supplied_name or local["name"]
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError(f"第 {row_number} 行目前仅支持 6 位 A 股代码")
    quantity = _decimal(raw.get("quantity"), "持仓数量", row_number)
    if quantity != quantity.to_integral_value():
        raise ValueError(f"第 {row_number} 行持仓数量必须是整数股")
    if quantity > Decimal("1000000000000"):
        raise ValueError(f"第 {row_number} 行持仓数量超过允许范围")
    cost_price = _decimal(raw.get("cost_price"), "成本价", row_number)
    if cost_price > Decimal("1000000000"):
        raise ValueError(f"第 {row_number} 行成本价超过允许范围")
    return {
        "symbol": symbol,
        "name": name[:80],
        "quantity": int(quantity),
        "cost_price": float(cost_price),
    }


def _csv_positions(text: str) -> list[dict]:
    if not text.strip():
        raise ValueError("CSV 文件为空")
    if len(text) > MAX_CSV_CHARS:
        raise ValueError("CSV 文件过大，第一版最多读取 1 MB")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;，")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("CSV 缺少表头")
    mapped = {}
    normalized_headers = {_header_key(header): header for header in reader.fieldnames}
    for field, aliases in _HEADER_ALIASES.items():
        match = next((normalized_headers[_header_key(alias)] for alias in aliases
                      if _header_key(alias) in normalized_headers), None)
        if match:
            mapped[field] = match
    missing = [label for field, label in (("symbol", "股票代码"), ("quantity", "持仓数量"),
                                          ("cost_price", "成本价")) if field not in mapped]
    if missing:
        raise ValueError(f"CSV 缺少必要列：{'、'.join(missing)}")
    rows = []
    for row_number, row in enumerate(reader, start=2):
        if not any(str(value or "").strip() for value in row.values()):
            continue
        item = {field: row.get(header) for field, header in mapped.items()}
        item["_row_number"] = row_number
        rows.append(item)
    return rows


def _raw_positions(payload: dict) -> tuple[str, str | None, list[dict]]:
    mode = str(payload.get("mode") or "manual").strip().lower()
    if mode == "csv":
        filename = _safe_filename(payload.get("filename"))
        return "CSV", filename, _csv_positions(str(payload.get("csv_text") or ""))
    if mode != "manual":
        raise ValueError("导入方式必须是 manual 或 csv")
    rows = payload.get("positions")
    if not isinstance(rows, list):
        raise ValueError("手工录入需要 positions 数组")
    return "MANUAL", None, [dict(row, _row_number=index)
                             for index, row in enumerate(rows, start=1)
                             if isinstance(row, dict)]


def _latest_real_price(conn, symbol: str) -> dict | None:
    quote = conn.execute(
        """SELECT q.price,q.observed_at,ds.code AS source
           FROM quote_snapshots q JOIN data_sources ds ON ds.id=q.source_id
           WHERE q.asset_symbol=?
           ORDER BY julianday(q.captured_at) DESC,q.observed_at DESC,ds.priority LIMIT 1""",
        (symbol,),
    ).fetchone()
    if quote and float(quote["price"]) > 0:
        return {"price": float(quote["price"]), "observed_at": quote["observed_at"],
                "source": quote["source"]}
    daily = conn.execute(
        """SELECT b.close AS price,b.trade_date AS observed_at,ds.code AS source
           FROM market_daily_bars b JOIN data_sources ds ON ds.id=b.source_id
           WHERE b.asset_symbol=? ORDER BY b.trade_date DESC,ds.priority LIMIT 1""",
        (symbol,),
    ).fetchone()
    if daily and float(daily["price"]) > 0:
        return {"price": float(daily["price"]), "observed_at": daily["observed_at"],
                "source": daily["source"]}
    return None


def _reconciliation(conn, rows: list[dict]) -> dict:
    existing = {
        row["symbol"]: dict(row) for row in conn.execute(
            """SELECT a.symbol,p.quantity,p.cost_price FROM positions p
               JOIN assets a ON a.id=p.asset_id"""
        )
    }
    incoming = {row["symbol"]: row for row in rows}
    added = sorted(set(incoming) - set(existing))
    removed = sorted(set(existing) - set(incoming))
    changed, unchanged = [], []
    for symbol in sorted(set(existing) & set(incoming)):
        old, new = existing[symbol], incoming[symbol]
        if (float(old["quantity"]) != float(new["quantity"]) or
                not math.isclose(float(old["cost_price"]), float(new["cost_price"]),
                                 rel_tol=0, abs_tol=1e-8)):
            changed.append(symbol)
        else:
            unchanged.append(symbol)
    return {
        "added": len(added), "changed": len(changed), "removed": len(removed),
        "unchanged": len(unchanged), "added_symbols": added,
        "changed_symbols": changed, "removed_symbols": removed,
    }


def preview_portfolio_import(payload: dict) -> dict:
    source_type, filename, raw_rows = _raw_positions(payload)
    if not raw_rows:
        raise ValueError("请至少录入一只持仓股票")
    if len(raw_rows) > MAX_POSITIONS:
        raise ValueError(f"第一版一次最多导入 {MAX_POSITIONS} 只股票")
    account_name = str(payload.get("account_name") or "我的本地账户").strip()[:80]
    if not account_name:
        raise ValueError("账户名称不能为空")
    as_of = str(payload.get("as_of") or date.today().isoformat()).strip()
    try:
        as_of_date = date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError("持仓日期必须是 YYYY-MM-DD") from exc
    if as_of_date > date.today():
        raise ValueError("持仓日期不能晚于今天")
    errors, rows = [], []
    with closing(connect()) as conn:
        for raw in raw_rows:
            row_number = int(raw.pop("_row_number", len(rows) + 1))
            try:
                rows.append(_resolve_position(conn, raw, row_number))
            except ValueError as exc:
                errors.append({"row": row_number, "message": str(exc)})
        duplicates = sorted(symbol for symbol in {row["symbol"] for row in rows}
                            if sum(item["symbol"] == symbol for item in rows) > 1)
        if duplicates:
            errors.append({"row": None, "message": f"存在重复股票代码：{'、'.join(duplicates)}"})
        if errors:
            return {"can_confirm": False, "errors": errors, "rows": rows,
                    "order_execution": False}
        reconciliation = _reconciliation(conn, rows)
        display_rows = []
        for row in rows:
            price = _latest_real_price(conn, row["symbol"])
            display_rows.append({**row, "valuation": price or {"price": None,
                                "observed_at": None, "source": None}})
        normalized = json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"))
        content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        created = _now()
        import_key = uuid.uuid4().hex
        source_name = (f"CSV 导入 · {filename}" if filename else "CSV 导入") if source_type == "CSV" else "用户手工录入"
        conn.execute(
            """INSERT INTO portfolio_imports
               (import_key,account_name,source_type,source_name,source_file_name,as_of,status,
                replace_existing,position_count,content_hash,normalized_json,reconciliation_json,
                created_at,expires_at)
               VALUES(?,?,?,?,?,?,'PREVIEW',1,?,?,?,?,?,?)""",
            (import_key, account_name, source_type, source_name, filename, as_of, len(rows),
             content_hash, normalized, json.dumps(reconciliation, ensure_ascii=False),
             _timestamp(created), _timestamp(created + timedelta(hours=PREVIEW_TTL_HOURS))),
        )
        conn.commit()
    return {
        "preview_id": import_key, "can_confirm": True, "errors": [],
        "account_name": account_name, "source_type": source_type,
        "source_name": source_name, "as_of": as_of, "rows": display_rows,
        "reconciliation": reconciliation, "expires_at": _timestamp(created + timedelta(hours=PREVIEW_TTL_HOURS)),
        "replace_existing": True, "order_execution": False,
    }


def confirm_portfolio_import(payload: dict) -> dict:
    if payload.get("confirmed") is not True:
        raise ValueError("必须明确确认后才能写入真实持仓")
    preview_id = str(payload.get("preview_id") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", preview_id):
        raise ValueError("导入预览编号无效")
    now = _now()
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        record = conn.execute(
            "SELECT * FROM portfolio_imports WHERE import_key=?", (preview_id,)
        ).fetchone()
        if not record or record["status"] != "PREVIEW":
            raise ValueError("该导入预览不存在或已经处理")
        if record["expires_at"] and datetime.fromisoformat(record["expires_at"]) < now:
            conn.execute("UPDATE portfolio_imports SET status='EXPIRED' WHERE id=?", (record["id"],))
            conn.commit()
            raise ValueError("该导入预览已经过期，请重新预览")
        normalized = str(record["normalized_json"])
        if hashlib.sha256(normalized.encode("utf-8")).hexdigest() != record["content_hash"]:
            raise ValueError("导入预览完整性校验失败")
        rows = json.loads(normalized)
        portfolio = conn.execute("SELECT id FROM portfolios ORDER BY id LIMIT 1").fetchone()
        if portfolio:
            portfolio_id = int(portfolio["id"])
        else:
            portfolio_id = conn.execute(
                """INSERT INTO portfolios
                   (name,as_of,source_type,source_name,imported_at,verification_status,last_import_id)
                   VALUES(?,?,?,?,?,'USER_CONFIRMED',?)""",
                (record["account_name"], record["as_of"], record["source_type"],
                 record["source_name"], _timestamp(now), record["id"]),
            ).lastrowid
        conn.execute("DELETE FROM positions")
        for row in rows:
            symbol = row["symbol"]
            exchange = "SH" if symbol.startswith(("5", "6", "9")) else (
                "BJ" if symbol.startswith(("4", "8")) else "SZ"
            )
            asset_type = "ETF" if symbol.startswith(("1", "5")) else "EQUITY"
            conn.execute(
                """INSERT INTO assets(symbol,exchange_symbol,name,market,asset_type,currency,data_status)
                   VALUES(?,?,?,?,?,'CNY','USER_PORTFOLIO')
                   ON CONFLICT(symbol) DO UPDATE SET
                     name=CASE WHEN excluded.name=excluded.symbol THEN assets.name ELSE excluded.name END,
                     exchange_symbol=excluded.exchange_symbol""",
                (symbol, f"{symbol}.{exchange}", row["name"], "CN", asset_type),
            )
            asset = conn.execute("SELECT id,name FROM assets WHERE symbol=?", (symbol,)).fetchone()
            valuation = _latest_real_price(conn, symbol)
            current_price = float(valuation["price"]) if valuation else 0.0
            highest = current_price if current_price > 0 else float(row["cost_price"])
            conn.execute(
                """INSERT INTO positions
                   (portfolio_id,asset_id,quantity,cost_price,current_price,highest_since_entry,
                    import_id,as_of,source_type,source_name,verification_status,valuation_status,
                    price_observed_at,price_source,tracking_started_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'USER_CONFIRMED',?,?,?,?)""",
                (portfolio_id, asset["id"], row["quantity"], row["cost_price"], current_price,
                 highest, record["id"], record["as_of"], record["source_type"],
                 record["source_name"], "AVAILABLE" if valuation else "UNAVAILABLE",
                 valuation["observed_at"] if valuation else None,
                 valuation["source"] if valuation else None, _timestamp(now)),
            )
        conn.execute(
            """UPDATE portfolios SET name=?,as_of=?,source_type=?,source_name=?,imported_at=?,
               verification_status='USER_CONFIRMED',last_import_id=? WHERE id=?""",
            (record["account_name"], record["as_of"], record["source_type"],
             record["source_name"], _timestamp(now), record["id"], portfolio_id),
        )
        conn.execute(
            """UPDATE portfolio_imports SET status='CONFIRMED',portfolio_id=?,confirmed_at=?
               WHERE id=?""",
            (portfolio_id, _timestamp(now), record["id"]),
        )
        conn.execute(
            """UPDATE portfolio_imports SET status='SUPERSEDED'
               WHERE status='PREVIEW' AND id<>?""",
            (record["id"],),
        )
        conn.commit()
    return {
        "status": "CONFIRMED", "preview_id": preview_id,
        "position_count": len(rows), "source_type": record["source_type"],
        "source_name": record["source_name"], "as_of": record["as_of"],
        "order_execution": False,
    }


def clear_portfolio(payload: dict) -> dict:
    if payload.get("confirmed") is not True:
        raise ValueError("必须明确确认后才能清空持仓")
    now = _now()
    as_of = date.today().isoformat()
    normalized = "[]"
    import_key = uuid.uuid4().hex
    with closing(connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        removed = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        portfolio = conn.execute("SELECT id,name FROM portfolios ORDER BY id LIMIT 1").fetchone()
        portfolio_id = int(portfolio["id"]) if portfolio else None
        reconciliation = {"added": 0, "changed": 0, "removed": removed, "unchanged": 0}
        cursor = conn.execute(
            """INSERT INTO portfolio_imports
               (import_key,portfolio_id,account_name,source_type,source_name,as_of,status,
                replace_existing,position_count,content_hash,normalized_json,reconciliation_json,
                created_at,confirmed_at)
               VALUES(?,?,?,?,?,?,'CONFIRMED',1,0,?,?,?,?,?)""",
            (import_key, portfolio_id, portfolio["name"] if portfolio else "我的本地账户",
             "MANUAL_CLEAR", "用户确认清空", as_of,
             hashlib.sha256(normalized.encode("utf-8")).hexdigest(), normalized,
             json.dumps(reconciliation, ensure_ascii=False), _timestamp(now), _timestamp(now)),
        )
        conn.execute("DELETE FROM positions")
        if portfolio_id:
            conn.execute(
                """UPDATE portfolios SET as_of=?,source_type='MANUAL_CLEAR',source_name='用户确认清空',
                   imported_at=?,verification_status='USER_CONFIRMED',last_import_id=? WHERE id=?""",
                (as_of, _timestamp(now), cursor.lastrowid, portfolio_id),
            )
        conn.commit()
    return {"status": "CLEARED", "removed": removed, "as_of": as_of,
            "order_execution": False}


def portfolio_metadata(conn) -> dict:
    row = conn.execute(
        """SELECT p.name,p.as_of,p.source_type,p.source_name,p.imported_at,
                  p.verification_status,i.confirmed_at,i.reconciliation_json
           FROM portfolios p LEFT JOIN portfolio_imports i ON i.id=p.last_import_id
           ORDER BY p.id LIMIT 1"""
    ).fetchone()
    if not row:
        return {
            "name": "我的本地账户", "as_of": None, "source_type": "UNSET",
            "source_name": "尚未导入", "imported_at": None,
            "verification_status": "UNVERIFIED", "reconciliation": None,
        }
    result = dict(row)
    try:
        result["reconciliation"] = json.loads(result.pop("reconciliation_json") or "null")
    except json.JSONDecodeError:
        result["reconciliation"] = None
    return result
