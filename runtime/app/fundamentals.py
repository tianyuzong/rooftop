"""Point-in-time fundamental data and profile-aware research scores."""

from __future__ import annotations

import json
import math
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable

from .db import connect
from .model_registry import DEFAULT_VALUATION, FINANCIAL_METRICS, load_model_context


PROFILE_RULES = {
    "aggressive": {
        "dimensions": {"growth": .45, "quality": .20, "cashflow": .10,
                       "safety": .10, "value": .15},
        "fundamental_weight": .25, "minimum_score": -.35, "minimum_coverage": .35,
    },
    "balanced": {
        "dimensions": {"growth": .20, "quality": .25, "cashflow": .20,
                       "safety": .20, "value": .15},
        "fundamental_weight": .40, "minimum_score": -.10, "minimum_coverage": .45,
    },
    "conservative": {
        "dimensions": {"growth": .05, "quality": .20, "cashflow": .25,
                       "safety": .30, "value": .20},
        "fundamental_weight": .55, "minimum_score": .05, "minimum_coverage": .55,
    },
}


REPORT_FIELD_MAP = {
    "revenue": "TOTALOPERATEREVE", "net_profit": "PARENTNETPROFIT",
    "deduct_net_profit": "KCFJCXSYJLR", "revenue_yoy_pct": "TOTALOPERATEREVETZ",
    "net_profit_yoy_pct": "PARENTNETPROFITTZ",
    "deduct_net_profit_yoy_pct": "KCFJCXSYJLRTZ", "roe_pct": "ROEJQ",
    "roic_pct": "ROIC", "gross_margin_pct": "XSMLL", "net_margin_pct": "XSJLL",
    "current_ratio": "LD", "quick_ratio": "SD", "cash_ratio": "XJLLB",
    "debt_ratio_pct": "ZCFZL", "interest_debt_ratio_pct": "INTEREST_DEBT_RATIO",
    "cashflow_to_profit": "NCO_NETPROFIT", "fcff": "FCFF_FORWARD",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date(value: Any) -> str | None:
    if value is None:
        return None
    try:
        parsed = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
        return parsed.date().isoformat() if hasattr(parsed, "date") else str(value)[:10]
    except Exception:
        return str(value)[:10] or None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _exchange_symbol(symbol: str) -> str:
    suffix = "SH" if str(symbol).startswith(("5", "6", "9")) else "SZ"
    return f"{str(symbol).zfill(6)}.{suffix}"


def _fetch_reports(symbol: str) -> list[dict]:
    import akshare as ak

    frame = ak.stock_financial_analysis_indicator_em(
        symbol=_exchange_symbol(symbol), indicator="按报告期"
    )
    rows = []
    for record in frame.to_dict("records"):
        report_date = _date(record.get("REPORT_DATE"))
        notice_date = _date(record.get("NOTICE_DATE"))
        if not report_date or not notice_date:
            continue
        row = {
            "symbol": symbol, "report_date": report_date, "notice_date": notice_date,
            "report_type": str(record.get("REPORT_TYPE") or ""),
            "source_code": "akshare_eastmoney_finance", "observed_at": _now(),
            "raw_json": json.dumps(record, ensure_ascii=False, default=str),
        }
        row.update({name: _number(record.get(source)) for name, source in REPORT_FIELD_MAP.items()})
        rows.append(row)
    return rows


def _fetch_valuation(symbol: str) -> dict:
    from .stock_compare import _fundamentals

    value = _fundamentals(symbol)
    source_name = str(value.get("source") or "")
    source_code = ("eastmoney_quote_profile" if "东方财富" in source_name else
                   "tencent_quote_fallback" if "腾讯" in source_name else "unknown_quote_profile")
    return {
        "symbol": symbol, "source_code": source_code, "observed_at": _now(),
        "market_cap": _number(value.get("market_cap")), "pe_ttm": _number(value.get("pe_ttm")),
        "pe_dynamic": _number(value.get("pe_dynamic")), "pb": _number(value.get("pb")),
        "roe_pct": (_number(value.get("roe")) * 100 if _number(value.get("roe")) is not None else None),
        "raw_json": json.dumps(value, ensure_ascii=False, default=str),
    }


def refresh_fundamental_snapshots(
    symbols: list[str], asof_date: str | None,
    report_fetcher: Callable[[str], list[dict]] = _fetch_reports,
    valuation_fetcher: Callable[[str], dict] = _fetch_valuation,
    conn_factory=connect,
) -> dict:
    """Refresh reports and valuation snapshots without making provider failures fatal."""
    stamp = _now()
    refreshed, report_rows, errors = 0, 0, []
    for symbol in dict.fromkeys(str(item) for item in symbols):
        try:
            reports = report_fetcher(symbol)
            with closing(conn_factory()) as conn:
                for row in reports:
                    columns = ["symbol", "report_date", "notice_date", "report_type", "source_code",
                               "observed_at", *REPORT_FIELD_MAP, "raw_json"]
                    values = [row.get(item) for item in columns]
                    updates = ",".join(f"{item}=excluded.{item}" for item in columns[2:])
                    conn.execute(
                        f"INSERT INTO fundamental_reports ({','.join(columns)}) "
                        f"VALUES ({','.join('?' for _ in columns)}) "
                        f"ON CONFLICT(symbol,report_date,source_code) DO UPDATE SET {updates}", values,
                    )
                if asof_date:
                    value = valuation_fetcher(symbol)
                    value["asof_date"] = str(asof_date)
                    columns = ["symbol", "asof_date", "source_code", "observed_at", "market_cap",
                               "pe_ttm", "pe_dynamic", "pb", "roe_pct", "raw_json"]
                    values = [value.get(item) for item in columns]
                    updates = ",".join(f"{item}=excluded.{item}" for item in columns[3:])
                    conn.execute(
                        f"INSERT INTO fundamental_valuations ({','.join(columns)}) "
                        f"VALUES ({','.join('?' for _ in columns)}) "
                        f"ON CONFLICT(symbol,asof_date,source_code) DO UPDATE SET {updates}", values,
                    )
                conn.commit()
            refreshed += 1
            report_rows += len(reports)
        except Exception as exc:
            errors.append({"symbol": symbol, "error": repr(exc)})
    return {
        "status": "SUCCESS_WITH_WARNINGS" if errors else "SUCCESS",
        "asof_date": asof_date, "symbols": len(symbols), "refreshed": refreshed,
        "report_rows": report_rows, "errors": errors, "updated_at": stamp,
    }


def load_fundamental_timelines(symbols: list[str], conn_factory=connect) -> dict:
    unique = list(dict.fromkeys(str(item) for item in symbols))
    if not unique:
        return {"reports": {}, "valuations": {}, "has_data": False}
    placeholders = ",".join("?" for _ in unique)
    with closing(conn_factory()) as conn:
        reports = conn.execute(
            f"SELECT * FROM fundamental_reports WHERE symbol IN ({placeholders}) "
            "ORDER BY symbol,notice_date,report_date", unique,
        ).fetchall()
        values = conn.execute(
            f"SELECT * FROM fundamental_valuations WHERE symbol IN ({placeholders}) "
            "ORDER BY symbol,asof_date", unique,
        ).fetchall()
    result = {"reports": {item: [] for item in unique},
              "valuations": {item: [] for item in unique}, "has_data": bool(reports or values),
              "model_context": load_model_context(unique, conn_factory)}
    for row in reports:
        result["reports"][row["symbol"]].append(dict(row))
    for row in values:
        result["valuations"][row["symbol"]].append(dict(row))
    return result


def _scale(value: float | None, low: float, high: float, inverse: bool = False) -> float | None:
    if value is None:
        return None
    if high == low:
        return 0.0
    score = max(-1.0, min(1.0, 2 * (float(value) - low) / (high - low) - 1))
    return -score if inverse else score


def _average(*values: float | None) -> float | None:
    available = [float(item) for item in values if item is not None]
    return sum(available) / len(available) if available else None


def _model_value(report: dict, valuation: dict, field: str) -> float | None:
    source = valuation if str(field).startswith("valuation.") else report
    return _number(source.get(str(field).removeprefix("valuation.")))


def _metric_value(report: dict, valuation: dict, metric: dict) -> float | None:
    if metric.get("field"):
        value = _model_value(report, valuation, metric["field"])
        if value is None and metric.get("fallback_field"):
            value = _model_value(report, valuation, metric["fallback_field"])
        return value
    ratio = metric.get("ratio") or {}
    numerator = _model_value(report, valuation, ratio.get("numerator", ""))
    denominator = _model_value(report, valuation, ratio.get("denominator", ""))
    if numerator is None or denominator in {None, 0}:
        return None
    return numerator / denominator


def _dimension_score(report: dict, valuation: dict, metrics: list[dict]) -> float | None:
    return _average(*[
        _scale(
            _metric_value(report, valuation, metric),
            float(metric["low"]), float(metric["high"]), bool(metric.get("inverse", False)),
        )
        for metric in metrics
    ])


def fundamental_snapshot(timelines: dict, symbol: str, signal_date: str,
                         profile: str) -> dict:
    """Return only fundamentals that were publicly available by signal_date."""
    reports = [item for item in timelines.get("reports", {}).get(symbol, [])
               if str(item.get("notice_date") or "") <= signal_date]
    values = [item for item in timelines.get("valuations", {}).get(symbol, [])
              if str(item.get("asof_date") or "") <= signal_date]
    report = max(reports, key=lambda item: (
        str(item.get("report_date") or ""), str(item.get("notice_date") or "")
    )) if reports else None
    valuation = max(values, key=lambda item: str(item.get("asof_date") or "")) if values else None
    if not report and not valuation:
        return {"available": False, "score": None, "coverage": 0.0, "eligible": False,
                "dimensions": {}, "report_date": None, "notice_date": None,
                "valuation_date": None, "reasons": ["无可用基本面快照"]}
    report = report or {}
    valuation = valuation or {}
    selected_models = (
        timelines.get("model_context", {}).get("by_symbol", {})
        .get(symbol, {}).get(profile, {})
    )
    financial_model = selected_models.get("financial") or {
        "model_key": f"financial.legacy.{profile}", "version": "legacy",
        "scope_type": "DEFAULT", "scope_value": "*",
        "specification": {
            "dimensions": FINANCIAL_METRICS,
            "dimension_weights": PROFILE_RULES[profile]["dimensions"],
            "fundamental_weight": PROFILE_RULES[profile]["fundamental_weight"],
            "minimum_score": PROFILE_RULES[profile]["minimum_score"],
            "minimum_coverage": PROFILE_RULES[profile]["minimum_coverage"],
        },
    }
    valuation_model = selected_models.get("valuation") or {
        "model_key": "valuation.legacy.general", "version": "legacy",
        "scope_type": "DEFAULT", "scope_value": "*",
        "specification": DEFAULT_VALUATION,
    }
    financial_spec = financial_model["specification"]
    valuation_spec = valuation_model["specification"]
    metric_dimensions = {
        **financial_spec.get("dimensions", {}),
        **valuation_spec.get("dimensions", {}),
    }
    dimensions = {
        name: _dimension_score(report, valuation, metrics)
        for name, metrics in metric_dimensions.items()
    }
    rules = {
        "dimensions": financial_spec.get(
            "dimension_weights", PROFILE_RULES[profile]["dimensions"]
        ),
        "fundamental_weight": financial_spec.get(
            "fundamental_weight", PROFILE_RULES[profile]["fundamental_weight"]
        ),
        "minimum_score": financial_spec.get(
            "minimum_score", PROFILE_RULES[profile]["minimum_score"]
        ),
        "minimum_coverage": financial_spec.get(
            "minimum_coverage", PROFILE_RULES[profile]["minimum_coverage"]
        ),
    }
    available = {key: value for key, value in dimensions.items() if value is not None}
    used_weight = sum(rules["dimensions"][key] for key in available)
    score = (sum(value * rules["dimensions"][key] for key, value in available.items()) / used_weight
             if used_weight else None)
    coverage = used_weight
    reasons = []
    if coverage < rules["minimum_coverage"]:
        reasons.append(f"基本面覆盖率 {coverage:.0%} 低于 {rules['minimum_coverage']:.0%}")
    if score is None or score < rules["minimum_score"]:
        reasons.append("基本面综合分未达到该风险档位门槛")
    return {
        "available": True, "score": round(score, 6) if score is not None else None,
        "coverage": round(coverage, 6), "eligible": not reasons,
        "dimensions": {key: (round(value, 6) if value is not None else None)
                       for key, value in dimensions.items()},
        "report_date": report.get("report_date"), "notice_date": report.get("notice_date"),
        "valuation_date": valuation.get("asof_date"), "reasons": reasons,
        "models": {
            "financial": {key: financial_model.get(key) for key in
                          ("model_key", "name", "version", "scope_type", "scope_value")},
            "valuation": {key: valuation_model.get(key) for key in
                          ("model_key", "name", "version", "scope_type", "scope_value")},
        },
        "fundamental_weight": rules["fundamental_weight"],
    }
