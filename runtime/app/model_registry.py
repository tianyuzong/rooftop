"""Versioned, declarative research-model registry.

The registry stores formulas and parameters as validated JSON. It never loads or
executes user supplied Python, spreadsheet macros, shell commands, or broker code.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable

from .db import connect


MODEL_KINDS = {"financial", "valuation", "factor", "strategy", "risk"}
PROFILES = {"aggressive", "balanced", "conservative"}
SCOPE_TYPES = {"DEFAULT", "INDUSTRY", "SYMBOL"}
FUNDAMENTAL_FIELDS = {
    "revenue", "net_profit", "deduct_net_profit", "revenue_yoy_pct",
    "net_profit_yoy_pct", "deduct_net_profit_yoy_pct", "roe_pct",
    "roic_pct", "gross_margin_pct", "net_margin_pct", "current_ratio",
    "quick_ratio", "cash_ratio", "debt_ratio_pct",
    "interest_debt_ratio_pct", "cashflow_to_profit", "fcff",
    "market_cap", "pe_ttm", "pe_dynamic", "pb",
}
FORBIDDEN_TOKENS = {
    "python", "javascript", "shell", "powershell", "subprocess", "import",
    "exec", "eval", "broker", "place_order", "api_key",
}


FINANCIAL_METRICS = {
    "growth": [
        {"field": "revenue_yoy_pct", "low": -20, "high": 40},
        {"field": "net_profit_yoy_pct", "low": -30, "high": 50},
        {"field": "deduct_net_profit_yoy_pct", "low": -30, "high": 50},
    ],
    "quality": [
        {"field": "roe_pct", "fallback_field": "valuation.roe_pct", "low": 3, "high": 25},
        {"field": "roic_pct", "low": 2, "high": 20},
        {"field": "net_margin_pct", "low": 2, "high": 25},
    ],
    "cashflow": [
        {"field": "cashflow_to_profit", "low": 0.4, "high": 1.4},
        {"ratio": {"numerator": "fcff", "denominator": "revenue"}, "low": -0.1, "high": 0.15},
    ],
    "safety": [
        {"field": "debt_ratio_pct", "low": 25, "high": 75, "inverse": True},
        {"field": "interest_debt_ratio_pct", "low": 10, "high": 60, "inverse": True},
        {"field": "current_ratio", "low": 0.7, "high": 2.0},
        {"field": "quick_ratio", "low": 0.5, "high": 1.5},
    ],
}

PROFILE_DEFAULTS = {
    "aggressive": {
        "dimension_weights": {"growth": .45, "quality": .20, "cashflow": .10,
                              "safety": .10, "value": .15},
        "fundamental_weight": .25, "minimum_score": -.35, "minimum_coverage": .35,
    },
    "balanced": {
        "dimension_weights": {"growth": .20, "quality": .25, "cashflow": .20,
                              "safety": .20, "value": .15},
        "fundamental_weight": .40, "minimum_score": -.10, "minimum_coverage": .45,
    },
    "conservative": {
        "dimension_weights": {"growth": .05, "quality": .20, "cashflow": .25,
                              "safety": .30, "value": .20},
        "fundamental_weight": .55, "minimum_score": .05, "minimum_coverage": .55,
    },
}

DEFAULT_VALUATION = {
    "schema_version": 1,
    "dimensions": {
        "value": [
            {"field": "valuation.pe_ttm", "fallback_field": "valuation.pe_dynamic",
             "low": 8, "high": 60, "inverse": True},
            {"field": "valuation.pb", "low": .8, "high": 8, "inverse": True},
        ]
    },
    "missing_data_policy": "preserve_missing_and_reduce_coverage",
    "research_only": True,
    "order_execution": False,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value: dict) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _walk_forbidden(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered == "order_execution":
                if item not in (None, False):
                    raise ValueError("研究模型不能启用自动交易执行")
                continue
            if any(token in lowered for token in FORBIDDEN_TOKENS):
                raise ValueError(f"模型字段不允许包含 {key}")
            _walk_forbidden(item)
    elif isinstance(value, list):
        for item in value:
            _walk_forbidden(item)
    elif isinstance(value, str):
        lowered = value.lower()
        if any(token in lowered for token in FORBIDDEN_TOKENS):
            raise ValueError("模型内容包含不允许的执行或交易能力")


def _validate_metric(metric: dict) -> dict:
    if not isinstance(metric, dict):
        raise ValueError("指标定义必须是对象")
    field = metric.get("field")
    ratio = metric.get("ratio")
    if bool(field) == bool(ratio):
        raise ValueError("指标必须且只能指定 field 或 ratio")
    if field:
        base = str(field).removeprefix("valuation.")
        if base not in FUNDAMENTAL_FIELDS:
            raise ValueError(f"不支持的财务字段：{field}")
        fallback = metric.get("fallback_field")
        if fallback and str(fallback).removeprefix("valuation.") not in FUNDAMENTAL_FIELDS:
            raise ValueError(f"不支持的备用字段：{fallback}")
    else:
        if not isinstance(ratio, dict):
            raise ValueError("ratio 必须是对象")
        for key in ("numerator", "denominator"):
            if str(ratio.get(key, "")).removeprefix("valuation.") not in FUNDAMENTAL_FIELDS:
                raise ValueError(f"不支持的比率字段：{ratio.get(key)}")
    try:
        low, high = float(metric["low"]), float(metric["high"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("指标必须提供数字 low/high") from exc
    if low >= high:
        raise ValueError("指标 low 必须小于 high")
    output = dict(metric)
    output["low"], output["high"] = low, high
    output["inverse"] = bool(output.get("inverse", False))
    return output


def validate_model_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("模型定义必须是对象")
    kind = str(payload.get("model_kind", "")).strip().lower()
    if kind not in MODEL_KINDS:
        raise ValueError("model_kind 必须是 financial、valuation、factor、strategy 或 risk")
    profile = str(payload.get("profile") or "").strip().lower() or None
    if profile and profile not in PROFILES:
        raise ValueError("模型风险档位无效")
    specification = payload.get("specification")
    if not isinstance(specification, dict):
        raise ValueError("specification 必须是对象")
    _walk_forbidden(specification)
    if specification.get("order_execution") not in (None, False):
        raise ValueError("研究模型不能启用自动交易执行")
    specification = json.loads(_dump(specification))
    if kind in {"financial", "valuation"}:
        dimensions = specification.get("dimensions")
        if not isinstance(dimensions, dict) or not dimensions:
            raise ValueError("财务或估值模型必须定义 dimensions")
        specification["dimensions"] = {
            str(name): [_validate_metric(item) for item in metrics]
            for name, metrics in dimensions.items()
            if isinstance(metrics, list) and metrics
        }
        if not specification["dimensions"]:
            raise ValueError("模型至少需要一个有效维度")
    specification["research_only"] = True
    specification["order_execution"] = False
    model_key = str(payload.get("model_key") or "").strip().lower()
    if not model_key or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for char in model_key):
        raise ValueError("model_key 只能包含小写字母、数字、点、下划线和连字符")
    version = str(payload.get("version") or "").strip()
    if not version or len(version) > 40:
        raise ValueError("模型 version 不能为空且最多 40 个字符")
    return {
        "model_key": model_key,
        "model_kind": kind,
        "name": str(payload.get("name") or model_key).strip()[:100],
        "description": str(payload.get("description") or "").strip()[:500],
        "profile": profile,
        "version": version,
        "specification": specification,
    }


def _default_payloads() -> list[dict]:
    rows = []
    for profile, rules in PROFILE_DEFAULTS.items():
        rows.append({
            "model_key": f"financial.default.{profile}",
            "model_kind": "financial",
            "name": f"{profile} 默认财务模型",
            "description": "公告日可见的成长、质量、现金流和安全维度。",
            "profile": profile,
            "version": "1.0.0",
            "specification": {
                "schema_version": 1,
                "dimensions": FINANCIAL_METRICS,
                **rules,
                "missing_data_policy": "preserve_missing_and_reduce_coverage",
                "research_only": True,
                "order_execution": False,
            },
        })
    rows.append({
        "model_key": "valuation.default.general",
        "model_kind": "valuation",
        "name": "通用估值模型",
        "description": "默认 PE/PB 相对区间；可由行业或个股模型覆盖。",
        "profile": None,
        "version": "1.0.0",
        "specification": DEFAULT_VALUATION,
    })
    return rows


def ensure_default_models(conn_factory: Callable = connect) -> None:
    with closing(conn_factory()) as conn:
        stamp = _now()
        for raw in _default_payloads():
            item = validate_model_payload(raw)
            cursor = conn.execute(
                """INSERT OR IGNORE INTO research_model_definitions
                   (model_key,model_kind,name,description,profile,version,status,
                    specification_json,checksum,created_by,created_at,activated_at)
                   VALUES(?,?,?,?,?,?,'ACTIVE',?,?, 'system',?,?)""",
                (item["model_key"], item["model_kind"], item["name"], item["description"],
                 item["profile"], item["version"], _dump(item["specification"]),
                 _checksum(item["specification"]), stamp, stamp),
            )
            model_id = cursor.lastrowid
            if not model_id:
                model_id = conn.execute(
                    "SELECT id FROM research_model_definitions WHERE model_key=? AND version=?",
                    (item["model_key"], item["version"]),
                ).fetchone()[0]
            exists = conn.execute(
                """SELECT 1 FROM research_model_assignments
                   WHERE model_id=? AND scope_type='DEFAULT' AND scope_value='*'
                     AND COALESCE(profile,'')=COALESCE(?,'') AND status='ACTIVE'""",
                (model_id, item["profile"]),
            ).fetchone()
            if not exists:
                conn.execute(
                    """INSERT INTO research_model_assignments
                       (model_id,model_kind,scope_type,scope_value,profile,status,
                        approved_by,created_at,activated_at)
                       VALUES(?,?,'DEFAULT','*',?,'ACTIVE','system',?,?)""",
                    (model_id, item["model_kind"], item["profile"], stamp, stamp),
                )
        conn.commit()


def _insert_model_definition(payload: dict, created_by: str,
                             conn_factory: Callable = connect) -> dict:
    """Internal implementation split out to keep SQL parameter count explicit."""
    item = validate_model_payload(payload)
    stamp = _now()
    with closing(conn_factory()) as conn:
        cursor = conn.execute(
            """INSERT INTO research_model_definitions
               (model_key,model_kind,name,description,profile,version,status,
                specification_json,checksum,created_by,created_at)
               VALUES(?,?,?,?,?,?,'DRAFT',?,?,?,?)""",
            (item["model_key"], item["model_kind"], item["name"], item["description"],
             item["profile"], item["version"], _dump(item["specification"]),
             _checksum(item["specification"]), str(created_by).strip(), stamp),
        )
        conn.commit()
        model_id = int(cursor.lastrowid)
    return {"id": model_id, **item, "status": "DRAFT", "checksum": _checksum(item["specification"]),
            "research_only": True, "order_execution": False}


def create_model_definition(payload: dict, created_by: str,
                            conn_factory: Callable = connect) -> dict:
    actor = str(created_by or "").strip()
    if not actor:
        raise ValueError("创建模型必须填写维护人")
    return _insert_model_definition(payload, actor, conn_factory)


def activate_model_definition(model_id: int, scope_type: str, scope_value: str,
                              profile: str | None, approved_by: str, confirmed: bool,
                              conn_factory: Callable = connect) -> dict:
    if not confirmed or not str(approved_by or "").strip():
        raise ValueError("激活模型必须明确确认并填写批准人")
    scope = str(scope_type or "DEFAULT").upper()
    if scope not in SCOPE_TYPES:
        raise ValueError("scope_type 必须是 DEFAULT、INDUSTRY 或 SYMBOL")
    value = "*" if scope == "DEFAULT" else str(scope_value or "").strip()
    if not value:
        raise ValueError("行业或个股作用域不能为空")
    normalized_profile = str(profile or "").strip().lower() or None
    if normalized_profile and normalized_profile not in PROFILES:
        raise ValueError("模型风险档位无效")
    stamp = _now()
    with closing(conn_factory()) as conn:
        row = conn.execute(
            "SELECT * FROM research_model_definitions WHERE id=?", (int(model_id),)
        ).fetchone()
        if not row:
            raise ValueError("模型版本不存在")
        model_profile = row["profile"]
        if model_profile and normalized_profile and model_profile != normalized_profile:
            raise ValueError("模型定义和作用域风险档位不一致")
        effective_profile = normalized_profile or model_profile
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE research_model_assignments SET status='ARCHIVED'
               WHERE model_kind=? AND scope_type=? AND scope_value=?
                 AND COALESCE(profile,'')=COALESCE(?,'') AND status='ACTIVE'""",
            (row["model_kind"], scope, value, effective_profile),
        )
        conn.execute(
            """INSERT INTO research_model_assignments
               (model_id,model_kind,scope_type,scope_value,profile,status,
                approved_by,created_at,activated_at)
               VALUES(?,?,?,?,?,'ACTIVE',?,?,?)""",
            (int(model_id), row["model_kind"], scope, value, effective_profile,
             str(approved_by).strip(), stamp, stamp),
        )
        conn.execute(
            "UPDATE research_model_definitions SET status='ACTIVE',activated_at=? WHERE id=?",
            (stamp, int(model_id)),
        )
        conn.commit()
    return {"id": int(model_id), "status": "ACTIVE", "scope_type": scope,
            "scope_value": value, "profile": effective_profile,
            "approved_by": str(approved_by).strip(), "activated_at": stamp,
            "research_only": True, "order_execution": False}


def model_registry_payload(conn_factory: Callable = connect) -> dict:
    ensure_default_models(conn_factory)
    with closing(conn_factory()) as conn:
        definitions = []
        for row in conn.execute(
            "SELECT * FROM research_model_definitions ORDER BY model_kind,model_key,created_at DESC"
        ):
            item = dict(row)
            item["specification"] = json.loads(item.pop("specification_json"))
            definitions.append(item)
        assignments = [dict(row) for row in conn.execute(
            """SELECT a.*,d.model_key,d.version,d.name FROM research_model_assignments a
               JOIN research_model_definitions d ON d.id=a.model_id
               ORDER BY a.status='ACTIVE' DESC,a.activated_at DESC"""
        )]
    return {"definitions": definitions, "assignments": assignments,
            "allowed_kinds": sorted(MODEL_KINDS), "allowed_scopes": sorted(SCOPE_TYPES),
            "declarative_only": True, "excel_import": "VALIDATED_JSON_CONVERSION_REQUIRED",
            "research_only": True, "order_execution": False}


def load_model_context(symbols: list[str], conn_factory: Callable = connect) -> dict:
    ensure_default_models(conn_factory)
    unique = list(dict.fromkeys(str(item) for item in symbols))
    with closing(conn_factory()) as conn:
        sectors = {symbol: [] for symbol in unique}
        if unique:
            placeholders = ",".join("?" for _ in unique)
            for row in conn.execute(
                f"SELECT symbol,sector_name FROM a_share_sector_memberships WHERE symbol IN ({placeholders})",
                unique,
            ):
                sectors[row["symbol"]].append(str(row["sector_name"]))
        rows = conn.execute(
            """SELECT a.*,d.model_key,d.name,d.version,d.specification_json
               FROM research_model_assignments a
               JOIN research_model_definitions d ON d.id=a.model_id
               WHERE a.status='ACTIVE' ORDER BY a.activated_at DESC,a.id DESC"""
        ).fetchall()
    output: dict[str, dict] = {}
    for symbol in unique:
        output[symbol] = {}
        for profile in sorted(PROFILES):
            selected = {}
            for kind in ("financial", "valuation"):
                candidates = []
                for row in rows:
                    if row["model_kind"] != kind or (row["profile"] and row["profile"] != profile):
                        continue
                    priority = 0
                    if row["scope_type"] == "SYMBOL" and row["scope_value"] == symbol:
                        priority = 3
                    elif row["scope_type"] == "INDUSTRY" and any(
                        row["scope_value"] in sector for sector in sectors.get(symbol, [])
                    ):
                        priority = 2
                    elif row["scope_type"] == "DEFAULT":
                        priority = 1
                    if priority:
                        candidates.append((priority, int(row["id"]), row))
                if candidates:
                    row = max(candidates, key=lambda item: (item[0], item[1]))[2]
                    selected[kind] = {
                        "model_key": row["model_key"], "name": row["name"],
                        "version": row["version"], "scope_type": row["scope_type"],
                        "scope_value": row["scope_value"],
                        "specification": json.loads(row["specification_json"]),
                    }
            output[symbol][profile] = selected
    return {"by_symbol": output, "sectors": sectors, "declarative_only": True}
