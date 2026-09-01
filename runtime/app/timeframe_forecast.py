"""Immediate multi-timeframe price ranges derived from cached TDX K-lines.

Daily forecast horizons are admitted only after chronological walk-forward
evaluation, split conformal calibration, and explicit non-regression gates.
Intraday rows remain descriptive context and never extend the plotted path.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from .charting import build_chart_series


TIMEFRAME_SPECS = (
    {"period": "1m", "label": "1分钟", "source_period": "1分钟K", "horizon_bars": 1,
     "minimum_samples": 20, "weight": 0.03, "intraday": True},
    {"period": "5m", "label": "5分钟", "source_period": "5分钟K", "horizon_bars": 1,
     "minimum_samples": 20, "weight": 0.04, "intraday": True},
    {"period": "15m", "label": "15分钟", "source_period": "15分钟K", "horizon_bars": 1,
     "minimum_samples": 16, "weight": 0.05, "intraday": True},
    {"period": "30m", "label": "30分钟", "source_period": "30分钟K", "horizon_bars": 1,
     "minimum_samples": 14, "weight": 0.06, "intraday": True},
    {"period": "60m", "label": "1小时", "source_period": "60分钟K", "horizon_bars": 1,
     "minimum_samples": 12, "weight": 0.07, "intraday": True},
    {"period": "120m", "label": "2小时", "source_period": "120分钟K", "horizon_bars": 1,
     "minimum_samples": 10, "weight": 0.08, "intraday": True},
    {"period": "1d", "label": "下一交易日", "source_period": "前复权日K", "horizon_bars": 1,
     "minimum_samples": 30, "weight": 0.15, "intraday": False},
    {"period": "1w", "label": "未来1周", "source_period": "日K滚动5日", "horizon_bars": 5,
     "minimum_samples": 30, "weight": 0.18, "intraday": False},
    {"period": "1mo", "label": "未来1月", "source_period": "日K滚动21日", "horizon_bars": 21,
     "minimum_samples": 24, "weight": 0.17, "intraday": False},
    {"period": "1q", "label": "未来1季", "source_period": "日K滚动63日", "horizon_bars": 63,
     "minimum_samples": 20, "weight": 0.10, "intraday": False},
    {"period": "1y", "label": "未来1年", "source_period": "日K滚动252日", "horizon_bars": 252,
     "minimum_samples": 20, "weight": 0.07, "intraday": False},
)

DAILY_PATH_PERIODS = {
    "1d": 1,
    "1w": 5,
    "1mo": 21,
    "1q": 63,
    "1y": 252,
}

MIN_FEATURE_HISTORY = 60
FEATURE_DISTANCE_WEIGHTS = (1.2, 1.1, 1.0, 1.0, 0.9, 0.8, 0.9, 0.6, 0.8,
                            0.9, 0.8, 0.7)


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a quantile from an empty sample")
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _returns(rows: list[dict], horizon_bars: int, intraday: bool) -> list[float]:
    output = []
    for index in range(horizon_bars, len(rows)):
        current = rows[index]
        previous = rows[index - horizon_bars]
        if intraday:
            current_date = str(current.get("time") or "")[:10]
            previous_date = str(previous.get("time") or "")[:10]
            if current_date != previous_date:
                continue
        start = float(previous.get("close") or 0.0)
        end = float(current.get("close") or 0.0)
        if start > 0 and end > 0:
            value = end / start - 1.0
            if math.isfinite(value):
                output.append(value)
    return output


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _scaled_daily_features(rows: list[dict]) -> dict[str, tuple[float, ...]]:
    """Build stationary, past-only features with fixed economic scaling."""
    closes = [float(row.get("close") or 0.0) for row in rows]
    volumes = [float(row.get("volume") or 0.0) for row in rows]
    features: dict[str, tuple[float, ...]] = {}
    for index in range(MIN_FEATURE_HISTORY, len(rows)):
        if min(closes[index], closes[index - 60]) <= 0:
            continue
        daily = [
            closes[pos] / closes[pos - 1] - 1.0
            for pos in range(index - 59, index + 1)
            if closes[pos - 1] > 0
        ]
        recent20 = daily[-20:]
        vol20 = statistics.pstdev(recent20) * math.sqrt(252)
        vol60 = statistics.pstdev(daily) * math.sqrt(252)
        volume_window = volumes[index - 19:index + 1]
        volume_mean = statistics.mean(volume_window)
        volume_std = statistics.pstdev(volume_window)
        high60 = max(closes[index - 59:index + 1])
        ma20 = statistics.mean(closes[index - 19:index + 1])
        ma60 = statistics.mean(closes[index - 59:index + 1])
        range20 = statistics.mean(
            (float(rows[pos].get("high") or closes[pos]) -
             float(rows[pos].get("low") or closes[pos])) / closes[pos]
            for pos in range(index - 19, index + 1) if closes[pos] > 0
        )
        values = (
            _clamp((closes[index] / closes[index - 5] - 1.0) / 0.08, -4.0, 4.0),
            _clamp((closes[index] / closes[index - 20] - 1.0) / 0.20, -4.0, 4.0),
            _clamp((closes[index] / closes[index - 60] - 1.0) / 0.35, -4.0, 4.0),
            _clamp((ma20 / ma60 - 1.0) / 0.15, -4.0, 4.0),
            _clamp((vol20 - 0.30) / 0.25, -4.0, 4.0),
            _clamp((vol20 / max(vol60, 0.05) - 1.0) / 0.60, -4.0, 4.0),
            _clamp((closes[index] / high60 - 1.0) / 0.30, -4.0, 1.0),
            _clamp((volumes[index] - volume_mean) / volume_std / 3.0, -4.0, 4.0)
            if volume_std else 0.0,
            _clamp((range20 - 0.025) / 0.035, -4.0, 4.0),
        )
        features[str(rows[index].get("time") or "")[:10]] = values
    return features


def _conditional_features(rows: list[dict], market_rows: list[dict]) -> list[tuple[float, ...] | None]:
    stock = _scaled_daily_features(rows)
    market = _scaled_daily_features(market_rows)
    output: list[tuple[float, ...] | None] = []
    for row in rows:
        date = str(row.get("time") or "")[:10]
        own = stock.get(date)
        benchmark = market.get(date)
        if own is None:
            output.append(None)
            continue
        market_context = (
            benchmark[0], benchmark[1], benchmark[4]
        ) if benchmark is not None else (0.0, 0.0, 0.0)
        output.append((*own, *market_context))
    return output


def _weighted_quantile(values: list[tuple[float, float]], probability: float) -> float:
    ordered = sorted(values, key=lambda item: item[0])
    total = sum(weight for _, weight in ordered)
    threshold = total * probability
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][0]


def _analog_distribution(feature: tuple[float, ...],
                         candidates: list[tuple[tuple[float, ...], float]]) -> dict | None:
    if len(candidates) < 80:
        return None
    ranked = []
    for other, outcome in candidates:
        distance = math.sqrt(sum(
            weight * (left - right) ** 2
            for weight, left, right in zip(FEATURE_DISTANCE_WEIGHTS, feature, other)
        ) / sum(FEATURE_DISTANCE_WEIGHTS))
        ranked.append((distance, outcome))
    ranked.sort(key=lambda item: item[0])
    neighbor_count = min(120, max(40, int(math.sqrt(len(ranked)) * 4)))
    neighbors = ranked[:neighbor_count]
    bandwidth = max(statistics.median(item[0] for item in neighbors), 0.10)
    weighted = [
        (outcome, math.exp(-0.5 * (distance / bandwidth) ** 2) + 0.02)
        for distance, outcome in neighbors
    ]
    total_weight = sum(weight for _, weight in weighted)
    effective = total_weight ** 2 / sum(weight ** 2 for _, weight in weighted)
    quantiles = {
        key: _weighted_quantile(weighted, probability)
        for key, probability in (("p10", 0.10), ("p25", 0.25), ("p50", 0.50),
                                 ("p75", 0.75), ("p90", 0.90))
    }
    quantiles.update({
        "probability_up": sum(weight for value, weight in weighted if value > 0) / total_weight,
        "neighbor_count": neighbor_count,
        "effective_neighbor_count": effective,
    })
    return quantiles


def _unconditional_distribution(
    candidates: list[tuple[tuple[float, ...], float]],
) -> dict:
    outcomes = [outcome for _, outcome in candidates]
    return {
        key: _quantile(outcomes, probability)
        for key, probability in (("p10", 0.10), ("p25", 0.25), ("p50", 0.50),
                                 ("p75", 0.75), ("p90", 0.90))
    } | {
        "probability_up": sum(value > 0 for value in outcomes) / len(outcomes),
        "neighbor_count": len(outcomes),
        "effective_neighbor_count": len(outcomes),
    }


def _blend_distribution(baseline: dict, conditional: dict, weight: float) -> dict:
    blended = {
        key: float(baseline[key]) * (1.0 - weight) + float(conditional[key]) * weight
        for key in ("p10", "p25", "p50", "p75", "p90", "probability_up")
    }
    blended.update({
        "neighbor_count": conditional["neighbor_count"],
        "effective_neighbor_count": conditional["effective_neighbor_count"],
    })
    return blended


def _wilson_interval(successes: int, count: int, z: float = 1.645) -> tuple[float, float]:
    if count <= 0:
        return 0.0, 1.0
    probability = successes / count
    denominator = 1.0 + z * z / count
    center = (probability + z * z / (2.0 * count)) / denominator
    margin = z * math.sqrt(
        probability * (1.0 - probability) / count + z * z / (4.0 * count * count)
    ) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def _conformal_adjustments(points: list[dict]) -> dict[str, float]:
    if not points:
        return {"median_bias": 0.0, "wide": 0.0, "central": 0.0}
    residuals = [point["actual"] - point["forecast"]["p50"] for point in points]
    wide_scores = [max(
        point["forecast"]["p10"] - point["actual"],
        point["actual"] - point["forecast"]["p90"], 0.0,
    ) for point in points]
    central_scores = [max(
        point["forecast"]["p25"] - point["actual"],
        point["actual"] - point["forecast"]["p75"], 0.0,
    ) for point in points]
    return {
        "median_bias": statistics.median(residuals),
        "wide": _quantile(wide_scores, min(1.0, 0.80 * (len(points) + 1) / len(points))),
        "central": _quantile(central_scores, min(1.0, 0.50 * (len(points) + 1) / len(points))),
    }


def _apply_conformal(forecast: dict, adjustments: dict[str, float]) -> dict:
    median = float(forecast["p50"]) + adjustments["median_bias"]
    p10 = float(forecast["p10"]) - adjustments["wide"]
    p90 = float(forecast["p90"]) + adjustments["wide"]
    p25 = float(forecast["p25"]) - adjustments["central"]
    p75 = float(forecast["p75"]) + adjustments["central"]
    return {
        **forecast,
        "p10": min(p10, p25, median),
        "p25": min(max(p25, p10), median),
        "p50": median,
        "p75": max(min(p75, p90), median),
        "p90": max(p90, p75, median),
    }


def _minimum_validation_points(horizon: int) -> int:
    if horizon <= 1:
        return 80
    if horizon <= 5:
        return 30
    if horizon <= 21:
        return 10
    if horizon <= 63:
        return 8
    if horizon <= 126:
        return 6
    return 5


def _historical_baseline_reference(base: dict,
                                   samples: list[tuple[tuple[float, ...], float]],
                                   reason: str,
                                   validation_count: int = 0,
                                   minimum_validation_count: int | None = None,
                                   validation: dict | None = None) -> dict:
    """Return an explicitly unvalidated empirical scenario instead of an empty chart."""
    horizon = max(1, int(base.get("horizon_trading_days") or 1))
    outcomes = [float(outcome) for _, outcome in samples if math.isfinite(float(outcome))]
    if len(outcomes) < 20 or not base.get("last_price"):
        return {
            **base, "status": "REJECTED_UNCALIBRATED",
            "sample_count": len(outcomes), "effective_sample_count": 0,
            "validation_count": validation_count,
            "minimum_validation_count": minimum_validation_count,
            "validation": validation,
            "reason": reason, "signal": "UNAVAILABLE",
            "signal_label": "未通过校准",
        }
    distribution = {
        key: _quantile(outcomes, probability)
        for key, probability in (("p10", 0.10), ("p25", 0.25), ("p50", 0.50),
                                 ("p75", 0.75), ("p90", 0.90))
    }
    last_price = float(base["last_price"])
    decimals = 3 if last_price < 10 else 2
    clipped = {
        key: max(-0.95, float(value)) for key, value in distribution.items()
    }
    raw_up_ratio = sum(value > 0 for value in outcomes) / len(outcomes)
    effective = max(1, len(outcomes) // horizon)
    probability_up = (raw_up_ratio * effective + 2.0) / (effective + 4.0)
    return {
        **base, "status": "BASELINE_REFERENCE",
        "reason": (
            f"{reason}；因此改画历史收益分布基准情景，仅用于观察范围，"
            "不代表模型具有预测优势"
        ),
        "sample_count": len(outcomes), "effective_sample_count": effective,
        "validation_count": validation_count,
        "minimum_validation_count": minimum_validation_count,
        "validation": validation,
        "forecast_basis": "HISTORICAL_BASELINE_REFERENCE",
        "predictive_edge_detected": False, "reference_only": True,
        "p10_return": round(clipped["p10"], 6),
        "p25_return": round(clipped["p25"], 6),
        "p50_return": round(clipped["p50"], 6),
        "p75_return": round(clipped["p75"], 6),
        "p90_return": round(clipped["p90"], 6),
        "p10_price": round(last_price * (1.0 + clipped["p10"]), decimals),
        "p25_price": round(last_price * (1.0 + clipped["p25"]), decimals),
        "p50_price": round(last_price * (1.0 + clipped["p50"]), decimals),
        "p75_price": round(last_price * (1.0 + clipped["p75"]), decimals),
        "p90_price": round(last_price * (1.0 + clipped["p90"]), decimals),
        "historical_up_ratio": round(probability_up, 4),
        "raw_historical_up_ratio": round(raw_up_ratio, 4),
        "up_ratio_method": "historical_baseline_beta_2_2_shrinkage",
        "signal": "REFERENCE", "signal_label": "历史基准",
        "sample_grade": (
            "一般" if effective >= 20 else "有限"
        ),
    }


def _calibrated_daily_period(spec: dict, rows: list[dict],
                             features: list[tuple[float, ...] | None],
                             coverage: dict) -> dict:
    horizon = int(spec["horizon_bars"])
    closes = [float(row.get("close") or 0.0) for row in rows]
    base = {
        "period": spec["period"], "label": spec["label"],
        "source_period": _source_period_label(spec, coverage),
        "last_price": closes[-1] if closes else None,
        "as_of": str(rows[-1].get("time") or "") if rows else coverage.get("last_bar"),
        "coverage": coverage, "horizon_trading_days": horizon,
        "validation_method": "expanding_walk_forward_conditional_analogs_split_conformal",
        "p10_return": None, "p25_return": None, "p50_return": None,
        "p75_return": None, "p90_return": None,
        "p10_price": None, "p25_price": None, "p50_price": None,
        "p75_price": None, "p90_price": None,
        "historical_up_ratio": None,
    }
    if len(rows) <= MIN_FEATURE_HISTORY + horizon + 80 or not closes:
        reference_samples = [
            ((), value) for value in _returns(rows, horizon, False)
        ]
        reason = f"历史不足以对未来 {horizon} 个交易日做滚动样本外校准"
        if reference_samples:
            return _historical_baseline_reference(base, reference_samples, reason)
        return {
            **base, "status": "INSUFFICIENT_HISTORY", "sample_count": 0,
            "effective_sample_count": 0, "validation_count": 0,
            "reason": reason,
            "signal": "UNAVAILABLE", "signal_label": "未通过校准",
        }

    def training(before_index: int) -> list[tuple[tuple[float, ...], float]]:
        return [
            (features[index], closes[index + horizon] / closes[index] - 1.0)
            for index in range(MIN_FEATURE_HISTORY, before_index - horizon + 1)
            if features[index] is not None and closes[index] > 0 and closes[index + horizon] > 0
        ]

    validation_start = max(MIN_FEATURE_HISTORY + horizon + 80, int(len(rows) * 0.70))
    validation_indices = list(range(validation_start, len(rows) - horizon, max(1, horizon)))
    points = []
    for index in validation_indices:
        train = training(index - 1)
        forecast = _analog_distribution(features[index], train) if features[index] else None
        if forecast is None:
            continue
        actual = closes[index + horizon] / closes[index] - 1.0
        baseline = _unconditional_distribution(train)
        points.append({"actual": actual, "conditional": forecast, "baseline": baseline})
    minimum_validation = _minimum_validation_points(horizon)
    final_train = training(len(rows) - 1)
    latest = _analog_distribution(features[-1], final_train) if features[-1] else None
    if len(points) < minimum_validation or latest is None:
        reason = (
            f"只有 {len(points)} 个互不重叠的样本外检验段，"
            f"至少需要 {minimum_validation} 个"
        )
        return _historical_baseline_reference(
            base, final_train, reason, len(points), minimum_validation,
        )

    split = max(1, len(points) // 2)
    calibration_points = points[:split]
    evaluation_points = points[split:]
    blend_candidates = (0.0, 0.25, 0.50, 0.75, 1.0)
    conditional_weight = min(blend_candidates, key=lambda weight: (
        statistics.mean(abs(
            point["actual"] - _blend_distribution(
                point["baseline"], point["conditional"], weight,
            )["p50"]
        ) for point in calibration_points),
        weight,
    ))
    blended_calibration = [
        {**point, "forecast": _blend_distribution(
            point["baseline"], point["conditional"], conditional_weight,
        )}
        for point in calibration_points
    ]
    evaluated = []
    calibration_history = list(blended_calibration)
    for point in evaluation_points:
        blended = _blend_distribution(
            point["baseline"], point["conditional"], conditional_weight,
        )
        calibration = _conformal_adjustments(calibration_history)
        if conditional_weight == 0.0:
            calibration["median_bias"] = 0.0
        evaluated.append({
            **point, "forecast": blended,
            "calibrated": _apply_conformal(blended, calibration),
        })
        calibration_history.append({**point, "forecast": blended})
    wide_hits = sum(
        point["calibrated"]["p10"] <= point["actual"] <= point["calibrated"]["p90"]
        for point in evaluated
    )
    central_hits = sum(
        point["calibrated"]["p25"] <= point["actual"] <= point["calibrated"]["p75"]
        for point in evaluated
    )
    wide_ci = _wilson_interval(wide_hits, len(evaluated))
    central_ci = _wilson_interval(central_hits, len(evaluated))
    model_mae = statistics.mean(
        abs(point["actual"] - point["calibrated"]["p50"]) for point in evaluated
    )
    baseline_mae = statistics.mean(
        abs(point["actual"] - point["baseline"]["p50"]) for point in evaluated
    )
    model_direction = sum(
        (point["calibrated"]["p50"] >= 0) == (point["actual"] >= 0)
        for point in evaluated
    ) / len(evaluated)
    baseline_direction = sum(
        (point["baseline"]["p50"] >= 0) == (point["actual"] >= 0)
        for point in evaluated
    ) / len(evaluated)
    gates = {
        "wide_80_coverage": wide_ci[0] <= 0.80 <= wide_ci[1],
        "median_non_regression": model_mae <= baseline_mae * 1.10 + 1e-9,
        "direction_non_regression": model_direction + 0.05 >= baseline_direction,
    }
    validation = {
        "walk_forward_points": len(points),
        "calibration_points": len(calibration_points),
        "evaluation_points": len(evaluated),
        "wide_80_coverage": round(wide_hits / len(evaluated), 4),
        "wide_80_coverage_ci90": [round(value, 4) for value in wide_ci],
        "central_50_coverage": round(central_hits / len(evaluated), 4),
        "central_50_coverage_ci90": [round(value, 4) for value in central_ci],
        "central_50_coverage_gate": central_ci[0] <= 0.50 <= central_ci[1],
        "median_mae": round(model_mae, 6),
        "baseline_median_mae": round(baseline_mae, 6),
        "direction_accuracy": round(model_direction, 4),
        "baseline_direction_accuracy": round(baseline_direction, 4),
        "conditional_weight": conditional_weight,
        "predictive_edge_detected": bool(
            conditional_weight > 0 and model_mae < baseline_mae
        ),
        "gates": gates,
        "gate_passed": all(gates.values()),
        "future_leakage": False,
    }
    if not validation["gate_passed"]:
        failed = [name for name, passed in gates.items() if not passed]
        return _historical_baseline_reference(
            base, final_train,
            "样本外门禁未通过：" + "、".join(failed),
            len(points), minimum_validation, validation,
        )

    blended_points = [
        {**point, "forecast": _blend_distribution(
            point["baseline"], point["conditional"], conditional_weight,
        )}
        for point in points
    ]
    final_calibration = _conformal_adjustments(blended_points)
    if conditional_weight == 0.0:
        final_calibration["median_bias"] = 0.0
    final_baseline = _unconditional_distribution(final_train)
    final_raw = _blend_distribution(final_baseline, latest, conditional_weight)
    final = _apply_conformal(final_raw, final_calibration)
    probability_up = (float(final["probability_up"]) * final["effective_neighbor_count"] + 2.0) / (
        final["effective_neighbor_count"] + 4.0
    )
    spread = max(final["p90"] - final["p10"], 1e-9)
    threshold = max(spread * 0.08, 0.0005)
    if final["p50"] > threshold and probability_up >= 0.52:
        signal, signal_label = "BULLISH", "偏多"
    elif final["p50"] < -threshold and probability_up <= 0.48:
        signal, signal_label = "BEARISH", "偏空"
    else:
        signal, signal_label = "NEUTRAL", "震荡"
    last_price = closes[-1]
    decimals = 3 if last_price < 10 else 2
    return {
        **base, "status": "AVAILABLE",
        "reason": (
            "条件相似模型通过样本外非退化门禁并完成在线 conformal 校准"
            if conditional_weight > 0 else
            "条件模型未证明增益，自动退回经在线 conformal 校准的历史基准分布"
        ),
        "sample_count": len(final_train),
        "effective_sample_count": round(final["effective_neighbor_count"], 1),
        "validation_count": len(points), "validation": validation,
        "forecast_basis": "CONDITIONAL" if conditional_weight > 0 else "CALIBRATED_BASELINE",
        "predictive_edge_detected": validation["predictive_edge_detected"],
        "p10_return": round(max(-0.95, final["p10"]), 6),
        "p25_return": round(max(-0.95, final["p25"]), 6),
        "p50_return": round(max(-0.95, final["p50"]), 6),
        "p75_return": round(max(-0.95, final["p75"]), 6),
        "p90_return": round(max(-0.95, final["p90"]), 6),
        "p10_price": round(last_price * (1.0 + max(-0.95, final["p10"])), decimals),
        "p25_price": round(last_price * (1.0 + max(-0.95, final["p25"])), decimals),
        "p50_price": round(last_price * (1.0 + max(-0.95, final["p50"])), decimals),
        "p75_price": round(last_price * (1.0 + max(-0.95, final["p75"])), decimals),
        "p90_price": round(last_price * (1.0 + max(-0.95, final["p90"])), decimals),
        "historical_up_ratio": round(probability_up, 4),
        "raw_historical_up_ratio": round(float(final["probability_up"]), 4),
        "up_ratio_method": "conditional_neighbors_beta_2_2_shrinkage",
        "signal": signal, "signal_label": signal_label,
        "sample_grade": (
            "充足" if len(points) >= 60 else "一般" if len(points) >= 20 else "有限"
        ),
    }


def _daily_period_forecasts(rows: list[dict], market_rows: list[dict],
                            coverage: dict, requested_days: int) -> tuple[dict, dict]:
    features = _conditional_features(rows, market_rows)
    reports = {}
    for spec in TIMEFRAME_SPECS:
        if spec["period"] in DAILY_PATH_PERIODS:
            reports[str(spec["period"])] = _calibrated_daily_period(
                spec, rows, features, coverage,
            )
    if requested_days in DAILY_PATH_PERIODS.values():
        period = next(key for key, value in DAILY_PATH_PERIODS.items() if value == requested_days)
        requested = reports[period]
    else:
        requested = _calibrated_daily_period({
            "period": "requested_horizon",
            "label": f"未来{requested_days}个交易日",
            "source_period": f"日K条件预测{requested_days}日",
            "horizon_bars": requested_days, "intraday": False,
        }, rows, features, coverage)
    return reports, requested


def _unavailable(spec: dict, coverage: dict, status: str, reason: str) -> dict:
    return {
        "period": spec["period"], "label": spec["label"],
        "source_period": spec["source_period"], "status": status,
        "reason": reason, "sample_count": 0, "last_price": None,
        "as_of": coverage.get("last_bar"), "coverage": coverage,
        "p10_return": None, "p25_return": None, "p50_return": None,
        "p75_return": None, "p90_return": None,
        "p10_price": None, "p25_price": None, "p50_price": None,
        "p75_price": None, "p90_price": None,
        "historical_up_ratio": None, "signal": "UNAVAILABLE",
        "signal_label": "数据不足",
    }


def _source_period_label(spec: dict, coverage: dict) -> str:
    labels = {
        "tdx_public": "通达信公开行情",
        "tdx_local": "通达信本地行情",
    }
    sources = [labels.get(str(item), str(item)) for item in coverage.get("sources", []) if item]
    source_label = "/".join(sources) if sources else "本地缓存"
    if spec["intraday"]:
        source_minutes = coverage.get("source_interval_minutes")
        requested = int(str(spec["period"])[:-1])
        if source_minutes:
            mode = "原生" if int(source_minutes) == requested else f"{source_minutes}分钟K聚合为"
            return f"{source_label} · {mode}{requested}分钟K"
    return f"{source_label} · {spec['source_period']}"


def _forecast_period(spec: dict, rows: list[dict], coverage: dict) -> dict:
    if not rows:
        return _unavailable(spec, coverage, "NO_DATA", "本地尚未采集该周期K线")
    samples = _returns(rows, int(spec["horizon_bars"]), bool(spec["intraday"]))
    minimum = int(spec["minimum_samples"])
    if len(samples) < minimum:
        result = _unavailable(
            spec, coverage, "INSUFFICIENT_HISTORY",
            f"已有 {len(samples)} 个可用样本，至少需要 {minimum} 个",
        )
        result.update({
            "sample_count": len(samples),
            "last_price": float(rows[-1]["close"]),
            "as_of": str(rows[-1].get("time") or coverage.get("last_bar") or ""),
        })
        return result

    historical_median = statistics.median(samples)
    recent = samples[-min(8, len(samples)):]
    recent_median = statistics.median(recent)
    q20, q80 = _quantile(samples, 0.20), _quantile(samples, 0.80)
    center = min(q80, max(q20, historical_median * 0.65 + recent_median * 0.35))
    shift = center - historical_median
    shifted = [max(-0.95, value + shift) for value in samples]
    p10, p25, p50, p75, p90 = (
        _quantile(shifted, probability)
        for probability in (0.10, 0.25, 0.50, 0.75, 0.90)
    )
    last_price = float(rows[-1]["close"])
    spread = max(p90 - p10, 1e-9)
    raw_up_ratio = sum(value > 0 for value in shifted) / len(shifted)
    effective_samples = max(1, len(shifted) // int(spec["horizon_bars"]))
    up_ratio = (raw_up_ratio * effective_samples + 2.0) / (effective_samples + 4.0)
    threshold = max(spread * 0.08, 0.00005)
    if p50 > threshold and up_ratio >= 0.52:
        signal, signal_label = "BULLISH", "偏多"
    elif p50 < -threshold and up_ratio <= 0.48:
        signal, signal_label = "BEARISH", "偏空"
    else:
        signal, signal_label = "NEUTRAL", "震荡"
    decimals = 3 if last_price < 10 else 2
    result = {
        "period": spec["period"], "label": spec["label"],
        "source_period": _source_period_label(spec, coverage), "status": "AVAILABLE",
        "reason": "历史收益分布结合最近8个同周期样本的中位变化",
        "sample_count": len(samples), "effective_sample_count": effective_samples,
        "last_price": round(last_price, decimals),
        "as_of": str(rows[-1].get("time") or coverage.get("last_bar") or ""),
        "p10_return": round(p10, 6), "p25_return": round(p25, 6),
        "p50_return": round(p50, 6), "p75_return": round(p75, 6),
        "p90_return": round(p90, 6),
        "p10_price": round(last_price * (1 + p10), decimals),
        "p25_price": round(last_price * (1 + p25), decimals),
        "p50_price": round(last_price * (1 + p50), decimals),
        "p75_price": round(last_price * (1 + p75), decimals),
        "p90_price": round(last_price * (1 + p90), decimals),
        "historical_up_ratio": round(up_ratio, 4),
        "raw_historical_up_ratio": round(raw_up_ratio, 4),
        "up_ratio_method": "overlap_adjusted_beta_2_2_shrinkage",
        "signal": signal, "signal_label": signal_label,
        "sample_grade": (
            "充足" if effective_samples >= 100 else
            "一般" if effective_samples >= 30 else "有限"
        ),
        "coverage": coverage,
    }
    return result


def _price_path(daily_rows: list[dict], timeframes: list[dict],
                horizon_months: int, requested_forecast: dict) -> dict:
    history = [
        {
            "date": str(row.get("time") or "")[:10],
            "price": round(float(row["close"]), 3 if float(row["close"]) < 10 else 2),
        }
        for row in daily_rows[-90:]
        if float(row.get("close") or 0.0) > 0
    ]
    if not history:
        requested_days = max(1, int(round(max(1, horizon_months) * 21)))
        return {
            "history_curve": [], "forecast_curve": [], "horizon_trading_days": 0,
            "validated_horizon_trading_days": 0,
            "reference_horizon_trading_days": 0,
            "requested_horizon_trading_days": requested_days,
            "horizon_status": "UNAVAILABLE",
            "horizon_reason": "没有可用于滚动样本外校准的日K数据",
        }

    requested_days = max(1, int(round(max(1, horizon_months) * 21)))
    daily_forecasts = {
        item["period"]: item for item in timeframes
        if item["period"] in DAILY_PATH_PERIODS
        and item["status"] in {"AVAILABLE", "BASELINE_REFERENCE"}
    }
    endpoints = []
    for period, trading_day in DAILY_PATH_PERIODS.items():
        if trading_day <= requested_days and period in daily_forecasts:
            endpoints.append((trading_day, daily_forecasts[period]))
    if (
        requested_forecast.get("status") in {"AVAILABLE", "BASELINE_REFERENCE"}
        and requested_days not in {day for day, _ in endpoints}
    ):
        endpoints.append((requested_days, requested_forecast))

    last_price = history[-1]["price"]
    curve = [{
        "trading_day": 0,
        "label": "当前",
        "p10": last_price, "p25": last_price, "p50": last_price,
        "p75": last_price, "p90": last_price,
        "basis": "CURRENT_PRICE", "validated": True,
    }]
    for trading_day, item in sorted(endpoints, key=lambda pair: pair[0]):
        curve.append({
            "trading_day": trading_day,
            "label": item["label"],
            "p10": item["p10_price"], "p25": item["p25_price"],
            "p50": item["p50_price"], "p75": item["p75_price"],
            "p90": item["p90_price"],
            "basis": item.get("forecast_basis"),
            "validated": item.get("status") == "AVAILABLE",
        })
    plotted_days = max((item["trading_day"] for item in curve), default=0)
    validated_days = max(
        (item["trading_day"] for item in curve if item.get("validated")), default=0
    )
    reference_days = max(
        (item["trading_day"] for item in curve
         if item.get("basis") == "HISTORICAL_BASELINE_REFERENCE"),
        default=0,
    )
    horizon_status = (
        "FULL" if validated_days >= requested_days else
        "PARTIAL" if validated_days > 0 else
        "REFERENCE_FULL" if reference_days >= requested_days else
        "REFERENCE_PARTIAL" if reference_days > 0 else "UNAVAILABLE"
    )
    return {
        "history_curve": history,
        "forecast_curve": curve,
        "horizon_trading_days": plotted_days,
        "validated_horizon_trading_days": validated_days,
        "reference_horizon_trading_days": reference_days,
        "requested_horizon_trading_days": requested_days,
        "horizon_status": horizon_status,
        "horizon_reason": (
            None if horizon_status == "FULL" else
            (
                f"请求未来 {requested_days} 个交易日；严格样本外校准只通过前 "
                f"{validated_days} 个交易日，其余延伸为历史基准情景"
            ) if validated_days and reference_days else
            (
                f"请求未来 {requested_days} 个交易日；条件模型未通过门禁，"
                f"当前展示到 {reference_days} 个交易日的历史基准情景"
            ) if reference_days else requested_forecast.get("reason")
        ),
    }


def _adjusted_weights(horizon_months: int) -> dict[str, float]:
    weights = {str(item["period"]): float(item["weight"]) for item in TIMEFRAME_SPECS}
    if horizon_months <= 3:
        for period in ("1m", "5m", "15m", "30m", "60m", "120m", "1d", "1w"):
            weights[period] *= 1.4
        for period in ("1q", "1y"):
            weights[period] *= 0.55
    elif horizon_months > 12:
        for period in ("1m", "5m", "15m", "30m", "60m", "120m"):
            weights[period] *= 0.55
        for period in ("1mo", "1q", "1y"):
            weights[period] *= 1.35
    return weights


def _summary(timeframes: list[dict], horizon_months: int, risk_profile: str) -> dict:
    available = [
        item for item in timeframes
        if item["status"] == "AVAILABLE" and item["period"] in DAILY_PATH_PERIODS
    ]
    descriptive_intraday = sum(
        item["status"] == "AVAILABLE" and item["period"] not in DAILY_PATH_PERIODS
        for item in timeframes
    )
    weights = _adjusted_weights(horizon_months)
    weighted_total = sum(weights[item["period"]] for item in available)
    direction_values = {"BULLISH": 1.0, "NEUTRAL": 0.0, "BEARISH": -1.0}
    score = (
        sum(weights[item["period"]] * direction_values[item["signal"]] for item in available)
        / weighted_total if weighted_total else 0.0
    )
    key_signals = {
        item["period"]: item["signal"] for item in available
        if item["period"] in {"1d", "1w", "1mo"}
    }
    key_bearish = sum(value == "BEARISH" for value in key_signals.values())
    sell_threshold = {"aggressive": -0.25, "balanced": -0.18,
                      "conservative": -0.10}.get(risk_profile, -0.18)
    buy_threshold = {"aggressive": 0.15, "balanced": 0.22,
                     "conservative": 0.30}.get(risk_profile, 0.22)
    if available and (score <= sell_threshold or key_bearish >= 2):
        action = "SELL_REVIEW"
        action_label = "走势偏空，触发卖出风险复核"
        sell_status = "TRIGGERED"
        sell_label = "触发卖出风险复核"
        sell_reason = "多周期方向偏空，或日/周/月关键周期中至少两个偏空"
    elif available and score < 0:
        action = "REDUCE_REVIEW"
        action_label = "走势偏弱，进入减仓观察"
        sell_status = "WATCH"
        sell_label = "进入减仓观察"
        sell_reason = "多周期综合方向偏弱，但尚未达到卖出复核阈值"
    elif available and score >= buy_threshold:
        action = "BUY_WATCH"
        action_label = "偏多，可关注买入条件"
        sell_status = "NOT_TRIGGERED"
        sell_label = None
        sell_reason = None
    else:
        action = "HOLD_WATCH"
        action_label = (
            "震荡，继续观察" if available else
            "暂无经验证方向，查看历史基准情景"
        )
        sell_status = "NOT_TRIGGERED" if available else "UNAVAILABLE"
        sell_label = None
        sell_reason = None
    direction = "BULLISH" if score >= buy_threshold else "BEARISH" if score <= sell_threshold else "MIXED"
    return {
        "status": "AVAILABLE" if available else "UNAVAILABLE",
        "available_periods": len(available),
        "descriptive_intraday_periods": descriptive_intraday,
        "total_periods": len(timeframes),
        "direction_score": round(score, 4),
        "direction": direction,
        "direction_label": {"BULLISH": "多周期偏多", "BEARISH": "多周期偏空",
                            "MIXED": "多空分化"}[direction],
        "action": action, "action_label": action_label,
        "sell_review": {
            "status": sell_status, "label": sell_label, "reason": sell_reason,
            "requires_real_position_for_quantity": True,
            "requires_real_position_for_cost_stop": True,
        },
        "method": "只汇总通过滚动样本外校准门禁的日/周/月级期限；分钟线仅作观察",
    }


def build_timeframe_forecast(conn, symbol: str, data_asof: str,
                             horizon_months: int, risk_profile: str) -> dict[str, Any]:
    """Build only forecast horizons that pass chronological calibration gates."""
    daily_payload = build_chart_series(conn, symbol, "1d")
    daily_rows = [
        row for row in daily_payload["series"]
        if str(row.get("time") or "")[:10] <= data_asof
    ]
    try:
        market_payload = build_chart_series(conn, "000001.SH", "1d")
        market_rows = [
            row for row in market_payload["series"]
            if str(row.get("time") or "")[:10] <= data_asof
        ]
    except Exception:
        market_rows = []
    daily_coverage = dict(daily_payload.get("coverage") or {})
    requested_days = max(1, int(round(max(1, int(horizon_months)) * 21)))
    daily_reports, requested_forecast = _daily_period_forecasts(
        daily_rows, market_rows, daily_coverage, requested_days,
    )
    timeframes = []
    for spec in TIMEFRAME_SPECS:
        if spec["intraday"]:
            payload = build_chart_series(conn, symbol, str(spec["period"]))
            rows = [
                row for row in payload["series"]
                if str(row.get("time") or "")[:10] <= data_asof
            ]
            coverage = dict(payload.get("coverage") or {})
            timeframes.append(_forecast_period(spec, rows, coverage))
        else:
            timeframes.append(daily_reports[str(spec["period"])])
    path = _price_path(
        daily_rows, timeframes, int(horizon_months),
        requested_forecast,
    )
    validated = [
        item for item in timeframes
        if item["period"] in DAILY_PATH_PERIODS and item["status"] == "AVAILABLE"
    ]
    references = [
        item for item in timeframes
        if item["period"] in DAILY_PATH_PERIODS
        and item["status"] == "BASELINE_REFERENCE"
    ]
    validation_status = (
        "WALK_FORWARD_CALIBRATED_FULL"
        if path["horizon_status"] == "FULL" else
        "WALK_FORWARD_CALIBRATED_PARTIAL_WITH_BASELINE_EXTENSION"
        if validated and references else
        "WALK_FORWARD_CALIBRATED_PARTIAL"
        if validated else
        "BASELINE_SCENARIO_ONLY"
        if references else "UNAVAILABLE_NOT_CALIBRATED"
    )
    return {
        "symbol": symbol, "data_asof": data_asof,
        "timeframes": timeframes,
        "requested_forecast": requested_forecast,
        **path,
        "summary": _summary(timeframes, int(horizon_months), risk_profile),
        "validation_status": validation_status,
        "validation": {
            "method": "expanding_walk_forward_conditional_analogs_split_conformal",
            "future_leakage": False,
            "requested_horizon_trading_days": requested_days,
            "validated_horizon_trading_days": path["validated_horizon_trading_days"],
            "reference_horizon_trading_days": path["reference_horizon_trading_days"],
            "passed_periods": [item["period"] for item in validated],
            "reference_periods": [item["period"] for item in references],
            "rejected_periods": [
                {"period": item["period"], "reason": item.get("reason")}
                for item in timeframes
                if item["period"] in DAILY_PATH_PERIODS and item["status"] != "AVAILABLE"
            ],
        },
        "formal_model_probability": None,
        "disclaimer": (
            "通过门禁的期限展示滚动样本外校准区间；未通过门禁的期限只展示"
            "历史收益分布基准情景。两者都不是确定价格或收益承诺。"
        ),
    }
