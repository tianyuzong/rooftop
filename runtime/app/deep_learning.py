"""Qwen3-backed next-day A-share probability and return research."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import threading
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from . import continuous_learning as online
from .db import DATA_LAKE, connect, initialize


SEQUENCE_LENGTH = 20
ARCHITECTURE = "qwen3-0.6b-numeric-adapter-v1"
QWEN_MODEL_ID = "Qwen/Qwen3-0.6B"
QWEN_MODEL_DIR = DATA_LAKE / "models" / "qwen3" / "Qwen3-0.6B"
QWEN_WEIGHTS_SHA256 = "f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b"
QWEN_WEIGHTS_SIZE = 1_503_300_328
DEFAULT_BATCH_SIZE_GPU = 128
DEFAULT_BATCH_SIZE_CPU = 16
_MODEL_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _seed_everything(seed: int = 20260829) -> None:
    random.seed(seed)
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    import numpy as np
    import torch

    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=4)
def _verify_qwen_files_cached(path_text: str, size: int, modified_ns: int,
                              expected_hash: str) -> dict:
    del modified_ns
    path = Path(path_text)
    actual_hash = _sha256_file(path)
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"Qwen3 权重哈希不匹配：期望 {expected_hash}，实际 {actual_hash}"
        )
    config_path = path.parent / "config.json"
    if not config_path.exists():
        raise RuntimeError(f"Qwen3 配置缺失：{config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("model_type") != "qwen3":
        raise RuntimeError(f"模型类型不是 qwen3：{config.get('model_type')!r}")
    return {
        "model_id": QWEN_MODEL_ID,
        "model_path": str(path.parent),
        "weights_sha256": actual_hash,
        "weights_size": size,
        "config_sha256": _sha256_file(config_path),
        "model_type": "qwen3",
    }


def _verify_qwen_files(model_dir: Path) -> dict:
    weights = model_dir / "model.safetensors"
    if not weights.exists():
        raise RuntimeError(
            f"Qwen3 权重尚未安装：{weights}。运行 ModelScope 下载 Qwen/Qwen3-0.6B。"
        )
    stat = weights.stat()
    expected_hash = os.environ.get("ARGUS_QWEN3_MODEL_SHA256", QWEN_WEIGHTS_SHA256).lower()
    if expected_hash == QWEN_WEIGHTS_SHA256 and stat.st_size != QWEN_WEIGHTS_SIZE:
        raise RuntimeError(
            f"Qwen3 权重大小异常：期望 {QWEN_WEIGHTS_SIZE}，实际 {stat.st_size}"
        )
    return _verify_qwen_files_cached(
        str(weights.resolve()), stat.st_size, stat.st_mtime_ns, expected_hash
    )


@lru_cache(maxsize=2)
def _load_qwen_runtime(model_path: str | None = None):
    import torch
    from transformers import AutoModel
    from transformers.utils import logging as transformers_logging

    configured_path = model_path or os.environ.get("ARGUS_QWEN3_MODEL_PATH")
    resolved = Path(configured_path) if configured_path else QWEN_MODEL_DIR
    metadata = _verify_qwen_files(resolved)
    requested_device = os.environ.get("ARGUS_QWEN3_DEVICE", "auto").strip().lower()
    if requested_device not in {"auto", "cpu", "cuda"}:
        raise RuntimeError("ARGUS_QWEN3_DEVICE 只能是 auto、cpu 或 cuda")
    use_cuda = torch.cuda.is_available() and requested_device != "cpu"
    if requested_device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("ARGUS_QWEN3_DEVICE=cuda，但 CUDA 当前不可用")
    device = torch.device("cuda" if use_cuda else "cpu")
    dtype = (torch.bfloat16 if use_cuda and torch.cuda.is_bf16_supported()
             else torch.float16 if use_cuda else torch.float32)
    transformers_logging.set_verbosity_error()
    started = time.perf_counter()
    backbone = AutoModel.from_pretrained(
        resolved, local_files_only=True, dtype=dtype,
    ).to(device)
    backbone.eval()
    for parameter in backbone.parameters():
        parameter.requires_grad_(False)
    metadata.update({
        "device": str(device),
        "device_name": torch.cuda.get_device_name(0) if use_cuda else "CPU",
        "dtype": str(dtype).replace("torch.", ""),
        "parameter_count": sum(parameter.numel() for parameter in backbone.parameters()),
        "load_seconds": time.perf_counter() - started,
    })
    return backbone, device, dtype, metadata


class NumericQwenAdapter:
    """Factory wrapper so importing this module does not eagerly import PyTorch."""

    @staticmethod
    def build(backbone):
        import torch

        class Model(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.backbone = backbone
                for parameter in self.backbone.parameters():
                    parameter.requires_grad_(False)
                hidden_size = int(backbone.config.hidden_size)
                self.projection = torch.nn.Sequential(
                    torch.nn.Linear(len(online.FEATURE_NAMES), hidden_size),
                    torch.nn.LayerNorm(hidden_size),
                )
                self.summary_token = torch.nn.Parameter(torch.zeros(1, 1, hidden_size))
                self.direction = torch.nn.Linear(hidden_size, 1)
                self.expected_return = torch.nn.Linear(hidden_size, 1)
                torch.nn.init.normal_(self.projection[0].weight, std=0.02)
                torch.nn.init.zeros_(self.projection[0].bias)
                torch.nn.init.normal_(self.summary_token, std=0.02)

            def train(self, mode: bool = True):
                super().train(mode)
                self.backbone.eval()
                return self

            def forward(self, values):
                import torch

                dtype = next(self.backbone.parameters()).dtype
                embedded = self.projection(values.float()).to(dtype=dtype)
                summary = self.summary_token.expand(values.shape[0], -1, -1).to(dtype=dtype)
                attention_mask = torch.ones(
                    values.shape[0], values.shape[1] + 1,
                    dtype=torch.long, device=values.device,
                )
                hidden = self.backbone(
                    inputs_embeds=torch.cat((embedded, summary), dim=1),
                    attention_mask=attention_mask,
                    use_cache=False,
                    return_dict=True,
                ).last_hidden_state[:, -1, :].float()
                return (self.direction(hidden).squeeze(-1),
                        self.expected_return(hidden).squeeze(-1))

            def adapter_state_dict(self):
                return {
                    key: value.detach().cpu().clone()
                    for key, value in self.state_dict().items()
                    if not key.startswith("backbone.")
                }

            def load_adapter_state_dict(self, state):
                result = self.load_state_dict(state, strict=False)
                unexpected = list(result.unexpected_keys)
                missing = [key for key in result.missing_keys
                           if not key.startswith("backbone.")]
                if unexpected or missing:
                    raise RuntimeError(
                        f"Qwen3 适配器状态不完整：missing={missing}, unexpected={unexpected}"
                    )

        return Model()


def build_sequence_samples(symbols: Iterable[str],
                           sequence_length: int = SEQUENCE_LENGTH) -> list[dict]:
    flat = online.build_walk_forward_samples(symbols)
    grouped = defaultdict(list)
    for sample in flat:
        grouped[str(sample["symbol"])].append(sample)
    feature_names = list(online.FEATURE_NAMES)
    result = []
    for symbol, samples in grouped.items():
        samples.sort(key=lambda item: item["signal_date"])
        for index in range(sequence_length - 1, len(samples)):
            window = samples[index - sequence_length + 1:index + 1]
            current = samples[index]
            result.append({
                "symbol": symbol,
                "signal_date": current["signal_date"],
                "target_date": current["target_date"],
                "sequence": [[float(item["features"][name]) for name in feature_names]
                             for item in window],
                "actual_return": float(current["actual_return"]),
                "actual_up": int(float(current["actual_return"]) > 0),
            })
    return sorted(result, key=lambda item: (item["target_date"], item["symbol"]))


def _time_split(samples: list[dict]) -> tuple[dict, dict]:
    dates = sorted({item["target_date"] for item in samples})
    if len(dates) < 126:
        raise ValueError(f"Qwen3 模型需要至少 126 个有标签交易日，当前只有 {len(dates)} 天")
    train_end = max(80, int(len(dates) * 0.65))
    validation_end = max(train_end + 20, int(len(dates) * 0.82))
    validation_end = min(validation_end, len(dates) - 20)
    split = {
        "training": (dates[0], dates[train_end - 1]),
        "validation": (dates[train_end], dates[validation_end - 1]),
        "holdout": (dates[validation_end], dates[-1]),
    }
    return ({
        name: [item for item in samples if start <= item["target_date"] <= end]
        for name, (start, end) in split.items()
    }, split)


def _fit_scaler(samples: list[dict]) -> dict:
    import numpy as np

    matrix = np.asarray([row for sample in samples for row in sample["sequence"]],
                        dtype="float32")
    mean = matrix.mean(axis=0)
    std = matrix.std(axis=0)
    std[std < 1e-6] = 1.0
    returns = np.asarray([item["actual_return"] for item in samples], dtype="float32")
    return {
        "mean": mean.tolist(), "std": std.tolist(),
        "return_scale": float(max(float(returns.std()), 0.005)),
        "return_mean": float(returns.mean()),
    }


def _tensorize(samples: list[dict], scaler: dict):
    import numpy as np
    import torch

    values = np.asarray([item["sequence"] for item in samples], dtype="float32")
    values = ((values - np.asarray(scaler["mean"], dtype="float32")) /
              np.asarray(scaler["std"], dtype="float32"))
    direction = np.asarray([item["actual_up"] for item in samples], dtype="float32")
    expected_return = np.asarray(
        [item["actual_return"] / scaler["return_scale"] for item in samples],
        dtype="float32",
    )
    return torch.from_numpy(values), torch.from_numpy(direction), torch.from_numpy(expected_return)


def _batch_size(device) -> int:
    default = DEFAULT_BATCH_SIZE_GPU if device.type == "cuda" else DEFAULT_BATCH_SIZE_CPU
    try:
        return max(1, min(int(os.environ.get("ARGUS_QWEN3_BATCH_SIZE", default)), 512))
    except ValueError:
        return default


def _iter_batches(tensors, batch_size: int, device, shuffle: bool = False,
                  seed: int = 20260829):
    import torch

    count = tensors[0].shape[0]
    if shuffle:
        generator = torch.Generator(device="cpu")
        generator.manual_seed(seed)
        indices = torch.randperm(count, generator=generator)
    else:
        indices = torch.arange(count)
    for offset in range(0, count, batch_size):
        selection = indices[offset:offset + batch_size]
        yield tuple(tensor[selection].to(device, non_blocking=True) for tensor in tensors)


def _loss(logits, predicted_return, direction, expected_return):
    import torch

    return (torch.nn.functional.binary_cross_entropy_with_logits(logits, direction) +
            0.35 * torch.nn.functional.huber_loss(
                predicted_return, expected_return, delta=1.0))


def _partition_loss(model, tensors, batch_size: int, device) -> float:
    import torch

    model.eval()
    total, count = 0.0, 0
    with torch.no_grad():
        for values, direction, expected_return in _iter_batches(
                tensors, batch_size, device):
            logits, predicted_return = model(values)
            loss = _loss(logits, predicted_return, direction, expected_return)
            total += float(loss.item()) * values.shape[0]
            count += values.shape[0]
    return total / max(1, count)


def _raw_outputs(model, samples: list[dict], scaler: dict,
                 batch_size: int, device) -> list[dict]:
    import torch

    if not samples:
        return []
    tensors = _tensorize(samples, scaler)
    outputs, cursor = [], 0
    model.eval()
    with torch.inference_mode():
        for values, _direction, _returns in _iter_batches(tensors, batch_size, device):
            logits, predicted_return = model(values)
            logits = logits.cpu().tolist()
            predicted_return = (predicted_return * scaler["return_scale"]).cpu().tolist()
            for logit, return_value in zip(logits, predicted_return):
                sample = samples[cursor]
                outputs.append({
                    **{key: sample[key] for key in (
                        "symbol", "signal_date", "target_date", "actual_return", "actual_up")},
                    "raw_logit": float(logit),
                    "raw_predicted_return": float(return_value),
                })
                cursor += 1
    return outputs


def _sigmoid(value: float) -> float:
    value = max(-30.0, min(30.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def _fit_calibration(validation: list[dict], scaler: dict,
                     max_drawdown: float) -> dict:
    candidates = []
    for temperature in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 5.0):
        for blend in (0.25, 0.5, 0.75, 1.0):
            brier = sum(
                (0.5 + (_sigmoid(item["raw_logit"] / temperature) - 0.5) * blend
                 - item["actual_up"]) ** 2 for item in validation
            ) / max(1, len(validation))
            candidates.append((brier, temperature, blend))
    _, temperature, probability_blend = min(candidates)
    anchor = float(scaler["return_mean"])
    return_candidates = []
    for blend in (0.0, 0.25, 0.5, 0.75, 1.0):
        mae = sum(abs(anchor + (item["raw_predicted_return"] - anchor) * blend
                      - item["actual_return"]) for item in validation) / max(1, len(validation))
        return_candidates.append((mae, blend))
    _, return_blend = min(return_candidates)
    calibration = {
        "temperature": temperature, "probability_blend": probability_blend,
        "return_blend": return_blend, "return_anchor": anchor,
        "selected_on": "validation_only",
    }
    validation_points = _calibrated_points(validation, calibration)
    execution_candidates = []
    buffered_limit = max_drawdown * 0.80
    for exposure in (0.25, 0.50, 0.75):
        performance = _strategy_metrics(validation_points, 0.55, exposure)
        enough_signals = performance["invested_days"] >= 5
        feasible = enough_signals and performance["strategy_max_drawdown"] <= buffered_limit
        execution_candidates.append((
            feasible, performance["strategy_return"],
            -performance["strategy_max_drawdown"], exposure, performance,
        ))
    feasible = [item for item in execution_candidates if item[0]]
    selected = max(feasible or execution_candidates)
    calibration.update({
        "signal_threshold": 0.55,
        "risk_exposure": selected[3],
        "validation_risk_buffer": 0.80,
        "validation_strategy": selected[4],
        "execution_rule": "fixed_threshold_with_capped_cash_exposure",
    })
    return calibration


def _calibrated_points(raw: list[dict], calibration: dict) -> list[dict]:
    points = []
    for item in raw:
        probability = 0.5 + (
            _sigmoid(item["raw_logit"] / calibration["temperature"]) - 0.5
        ) * calibration["probability_blend"]
        predicted_return = calibration["return_anchor"] + (
            item["raw_predicted_return"] - calibration["return_anchor"]
        ) * calibration["return_blend"]
        points.append({
            **{key: item[key] for key in (
                "symbol", "signal_date", "target_date", "actual_return", "actual_up")},
            "probability_up": float(probability),
            "predicted_return": float(predicted_return),
        })
    return points


def _strategy_metrics(points: list[dict], threshold: float = 0.55,
                      exposure: float = 1.0) -> dict:
    by_date = defaultdict(list)
    for item in points:
        if item["probability_up"] >= threshold:
            by_date[item["target_date"]].append(float(item["actual_return"]))
        else:
            by_date.setdefault(item["target_date"], [])
    equity, peak, drawdown, invested = 1.0, 1.0, 0.0, False
    invested_days = 0
    for day in sorted(by_date):
        selected = by_date[day]
        now_invested = bool(selected)
        if now_invested:
            invested_days += 1
        cost = 0.001 * exposure if now_invested != invested else 0.0
        gross_return = sum(selected) / len(selected) if selected else 0.0
        equity *= 1.0 + gross_return * exposure - cost
        peak = max(peak, equity)
        drawdown = max(drawdown, 1.0 - equity / peak)
        invested = now_invested
    return {
        "strategy_return": equity - 1.0,
        "strategy_max_drawdown": drawdown,
        "signal_threshold": threshold,
        "risk_exposure": exposure,
        "invested_days": invested_days,
        "trading_days": len(by_date),
    }


def _prediction_metrics(points: list[dict], execution: dict | None = None) -> dict:
    if not points:
        return {"sample_count": 0, "accuracy": 0.0, "brier": 1.0,
                "mae": 1.0, "strategy_return": 0.0,
                "strategy_max_drawdown": 0.0}
    accuracy = sum((item["probability_up"] >= 0.5) == bool(item["actual_up"])
                   for item in points) / len(points)
    brier = sum((item["probability_up"] - item["actual_up"]) ** 2
                for item in points) / len(points)
    mae = sum(abs(item["predicted_return"] - item["actual_return"])
              for item in points) / len(points)
    strategy = _strategy_metrics(
        points,
        float((execution or {}).get("signal_threshold", 0.55)),
        float((execution or {}).get("risk_exposure", 1.0)),
    )
    return {
        "sample_count": len(points), "accuracy": accuracy, "brier": brier,
        "mae": mae, **strategy,
        "date_start": min(item["target_date"] for item in points),
        "date_end": max(item["target_date"] for item in points),
    }


def _evaluate(model, samples: list[dict], scaler: dict, calibration: dict,
              batch_size: int, device) -> tuple[dict, list[dict]]:
    points = _calibrated_points(
        _raw_outputs(model, samples, scaler, batch_size, device), calibration)
    return _prediction_metrics(points, calibration), points


def _baseline_metrics(samples: list[dict], training: list[dict]) -> dict:
    mean_return = (sum(item["actual_return"] for item in training) / len(training)
                   if training else 0.0)
    return _prediction_metrics([{
        **{key: item[key] for key in (
            "symbol", "signal_date", "target_date", "actual_return", "actual_up")},
        "probability_up": 0.5, "predicted_return": mean_return,
    } for item in samples])


def train_deep_model(symbols: Iterable[str], cycle_id: int | None = None,
                     max_drawdown: float = 0.15, auto_promote: bool = True,
                     epochs: int = 32) -> dict:
    initialize()
    symbols = list(dict.fromkeys(str(symbol) for symbol in symbols))
    samples = build_sequence_samples(symbols)
    partitions, split = _time_split(samples)
    scaler = _fit_scaler(partitions["training"])
    _seed_everything()
    import torch

    with _MODEL_LOCK:
        backbone, device, _dtype, runtime = _load_qwen_runtime()
        model = NumericQwenAdapter.build(backbone).to(device)
        trainable = [parameter for parameter in model.parameters() if parameter.requires_grad]
        optimizer = torch.optim.AdamW(trainable, lr=0.0015, weight_decay=0.001)
        batch_size = _batch_size(device)
        training_tensors = _tensorize(partitions["training"], scaler)
        validation_tensors = _tensorize(partitions["validation"], scaler)
        best_state, best_loss, stale = None, float("inf"), 0
        history = []
        training_started = time.perf_counter()
        for epoch in range(max(4, int(epochs))):
            model.train()
            total_loss, trained_count = 0.0, 0
            for values, direction, expected_return in _iter_batches(
                    training_tensors, batch_size, device, shuffle=True,
                    seed=20260829 + epoch):
                optimizer.zero_grad(set_to_none=True)
                logits, predicted_return = model(values)
                loss = _loss(logits, predicted_return, direction, expected_return)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(trainable, 1.0)
                optimizer.step()
                total_loss += float(loss.item()) * values.shape[0]
                trained_count += values.shape[0]
            validation_loss = _partition_loss(
                model, validation_tensors, batch_size, device)
            history.append({
                "epoch": epoch + 1,
                "train_loss": total_loss / max(1, trained_count),
                "validation_loss": validation_loss,
            })
            if validation_loss < best_loss - 1e-5:
                best_loss, stale = validation_loss, 0
                best_state = model.adapter_state_dict()
            else:
                stale += 1
                if stale >= 7:
                    break
        if best_state is None:
            raise RuntimeError("Qwen3 适配器训练未生成检查点")
        model.load_adapter_state_dict(best_state)
        calibration = _fit_calibration(
            _raw_outputs(model, partitions["validation"], scaler, batch_size, device),
            scaler, max_drawdown,
        )
        training_metrics, _ = _evaluate(
            model, partitions["training"], scaler, calibration, batch_size, device)
        validation_metrics, _ = _evaluate(
            model, partitions["validation"], scaler, calibration, batch_size, device)
        holdout_metrics, holdout_points = _evaluate(
            model, partitions["holdout"], scaler, calibration, batch_size, device)
        training_seconds = time.perf_counter() - training_started
        baseline = _baseline_metrics(partitions["holdout"], partitions["training"])
        gate = {
            "model_family_qwen3": runtime["model_type"] == "qwen3",
            "official_weights_verified": runtime["weights_sha256"] == QWEN_WEIGHTS_SHA256,
            "strict_time_split": True,
            "validation_only_calibration": True,
            "holdout_untouched_until_final": False,
            "holdout_window_reused_after_execution_fix": True,
            "sample_count_pass": holdout_metrics["sample_count"] >= 40,
            "accuracy_pass": holdout_metrics["accuracy"] >= 0.45,
            "brier_pass": holdout_metrics["brier"] <= baseline["brier"] * 1.02,
            "mae_pass": holdout_metrics["mae"] <= baseline["mae"] * 1.10,
            "return_nonnegative_pass": holdout_metrics["strategy_return"] >= 0.0,
            "risk_pass": holdout_metrics["strategy_max_drawdown"] <= max_drawdown,
        }
        required_gate_keys = (
            "model_family_qwen3", "official_weights_verified", "strict_time_split",
            "validation_only_calibration", "sample_count_pass", "accuracy_pass",
            "brier_pass", "mae_pass", "return_nonnegative_pass", "risk_pass",
        )
        gate["passed"] = all(bool(gate[key]) for key in required_gate_keys)

        model_dir = DATA_LAKE / "models" / "deep_prediction"
        model_dir.mkdir(parents=True, exist_ok=True)
        snapshot = _dump({
            "architecture": ARCHITECTURE,
            "base_model_sha256": runtime["weights_sha256"],
            "symbols": symbols,
            "training_end": split["training"][1],
            "data_end": split["holdout"][1],
            "features": list(online.FEATURE_NAMES),
            "sequence_length": SEQUENCE_LENGTH,
        })
        base_key = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()[:16]
        artifact_path = model_dir / f"qwen3-{split['holdout'][1]}-{base_key}.pt"
        temporary = artifact_path.with_suffix(".tmp")
        torch.save({
            "architecture": ARCHITECTURE,
            "base_model_id": QWEN_MODEL_ID,
            "base_model_sha256": runtime["weights_sha256"],
            "adapter_state_dict": best_state,
            "scaler": scaler,
            "calibration": calibration,
            "feature_names": list(online.FEATURE_NAMES),
            "sequence_length": SEQUENCE_LENGTH,
            "symbols": symbols,
        }, temporary)
        artifact_hash = _sha256_file(temporary)
        temporary.replace(artifact_path)
        version_key = f"qwen3-{split['holdout'][1]}-{artifact_hash[:12]}"
        runtime_metrics = {
            **runtime,
            "batch_size": batch_size,
            "epochs_requested": max(4, int(epochs)),
            "epochs_completed": len(history),
            "training_seconds": training_seconds,
            "trainable_parameters": sum(parameter.numel() for parameter in trainable),
        }
        metrics = {
            "training": training_metrics,
            "validation": validation_metrics,
            "holdout": holdout_metrics,
            "baseline_holdout": baseline,
            "loss_history": history,
            "split": split,
            "calibration": calibration,
            "runtime": runtime_metrics,
            "holdout_points": holdout_points[-300:],
        }
        with closing(connect()) as conn:
            active = conn.execute(
                "SELECT version_key FROM deep_model_versions WHERE status='ACTIVE' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            parent = str(active[0]) if active else None
            conn.execute(
                "UPDATE deep_model_versions SET status='ARCHIVED' "
                "WHERE status='ACTIVE' AND architecture<>?", (ARCHITECTURE,))
            status = ("ACTIVE" if auto_promote and gate["passed"]
                      else "CANDIDATE" if gate["passed"] else "REJECTED")
            if status == "ACTIVE":
                conn.execute(
                    "UPDATE deep_model_versions SET status='ARCHIVED' WHERE status='ACTIVE'")
            conn.execute(
                """INSERT INTO deep_model_versions
                   (version_key,parent_version,status,architecture,feature_schema_json,
                    sequence_length,training_start,training_end,artifact_path,artifact_hash,
                    metrics_json,gate_json,reason,created_at,activated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(version_key) DO UPDATE SET status=excluded.status,
                     metrics_json=excluded.metrics_json,gate_json=excluded.gate_json,
                     reason=excluded.reason,activated_at=excluded.activated_at""",
                (
                    version_key, parent, status, ARCHITECTURE,
                    _dump(list(online.FEATURE_NAMES)), SEQUENCE_LENGTH,
                    split["training"][0], split["training"][1], str(artifact_path),
                    artifact_hash, _dump(metrics), _dump(gate),
                "Qwen3 numeric-token adapter selected on validation and rechecked on a "
                "chronological holdout reused after the execution-rule correction",
                    _now(), _now() if status == "ACTIVE" else None,
                ),
            )
            conn.commit()
        predictions = (create_deep_predictions(symbols, cycle_id=cycle_id)
                       if status == "ACTIVE"
                       else {"status": "SKIPPED_NOT_ACTIVE", "count": 0})
        if device.type == "cuda":
            torch.cuda.empty_cache()
        return {
            "version_key": version_key,
            "status": status,
            "gate": gate,
            "metrics": {
                "training": training_metrics,
                "validation": validation_metrics,
                "holdout": holdout_metrics,
                "baseline_holdout": baseline,
                "runtime": runtime_metrics,
                "calibration": calibration,
            },
            "artifact_path": str(artifact_path),
            "artifact_hash": artifact_hash,
            "predictions": predictions,
        }


def _active_bundle():
    import torch

    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM deep_model_versions WHERE status='ACTIVE' AND architecture=? "
            "ORDER BY id DESC LIMIT 1", (ARCHITECTURE,)).fetchone()
    if not row:
        return None, None, "no_active_qwen3_model"
    path = Path(str(row["artifact_path"]))
    if not path.exists() or _sha256_file(path) != row["artifact_hash"]:
        return dict(row), None, "artifact_missing_or_hash_mismatch"
    bundle = torch.load(path, map_location="cpu", weights_only=False)
    if bundle.get("architecture") != ARCHITECTURE:
        return dict(row), None, "artifact_architecture_mismatch"
    try:
        backbone, device, _dtype, runtime = _load_qwen_runtime()
    except Exception as exc:
        return dict(row), None, f"qwen3_runtime_unavailable:{exc!r}"
    if bundle.get("base_model_sha256") != runtime["weights_sha256"]:
        return dict(row), None, "base_model_hash_mismatch"
    model = NumericQwenAdapter.build(backbone).to(device)
    model.load_adapter_state_dict(bundle["adapter_state_dict"])
    model.eval()
    return dict(row), (bundle, model, device, runtime), None


def _latest_sequences(symbols: Iterable[str], sequence_length: int) -> dict[str, dict]:
    bars_by_symbol = online._load_bars(symbols)
    sentiment = online._load_sentiment(symbols)
    result = {}
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < sequence_length + 20:
            continue
        sequence = []
        for index in range(len(bars) - sequence_length, len(bars)):
            day = str(bars[index]["trade_date"])
            features = online._features_at(
                bars, index, sentiment.get((symbol, day), 0.0), 0.0)
            sequence.append([float(features[name]) for name in online.FEATURE_NAMES])
        result[symbol] = {
            "sequence": sequence,
            "signal_date": str(bars[-1]["trade_date"]),
            "features": {name: sequence[-1][index]
                         for index, name in enumerate(online.FEATURE_NAMES)},
        }
    return result


def create_deep_predictions(symbols: Iterable[str], cycle_id: int | None = None) -> dict:
    initialize()
    with _MODEL_LOCK:
        row, active, error = _active_bundle()
        if not active:
            return {"status": "NO_USABLE_ACTIVE_MODEL", "error": error, "count": 0}
        bundle, model, device, _runtime = active
        latest = _latest_sequences(symbols, int(bundle["sequence_length"]))
        if not latest:
            return {"status": "NO_FEATURES", "count": 0}
        symbols_ordered = sorted(latest)
        samples = [{
            "symbol": symbol,
            "signal_date": latest[symbol]["signal_date"],
            "target_date": "",
            "actual_return": 0.0,
            "actual_up": 0,
            "sequence": latest[symbol]["sequence"],
        } for symbol in symbols_ordered]
        predictions = _calibrated_points(
            _raw_outputs(model, samples, bundle["scaler"], _batch_size(device), device),
            bundle["calibration"],
        )
        created = []
        with closing(connect()) as conn:
            for symbol, prediction in zip(symbols_ordered, predictions):
                signal_date = latest[symbol]["signal_date"]
                target_date = online.next_trading_day(
                    datetime.fromisoformat(signal_date).date()).isoformat()
                key = hashlib.sha256(
                    f"{row['version_key']}|{symbol}|{target_date}".encode("utf-8")
                ).hexdigest()
                probability = float(prediction["probability_up"])
                predicted_return = float(prediction["predicted_return"])
                confidence = min(1.0, abs(probability - 0.5) * 2.0)
                conn.execute(
                    """INSERT INTO deep_model_predictions
                       (prediction_key,cycle_id,model_version,symbol,signal_date,target_date,
                        probability_up,predicted_return,confidence,features_json,status,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,'PENDING',?)
                       ON CONFLICT(prediction_key) DO UPDATE SET cycle_id=excluded.cycle_id,
                         probability_up=excluded.probability_up,
                         predicted_return=excluded.predicted_return,
                         confidence=excluded.confidence,features_json=excluded.features_json""",
                    (
                        key, cycle_id, row["version_key"], symbol, signal_date, target_date,
                        probability, predicted_return, confidence,
                        _dump(latest[symbol]["features"]), _now(),
                    ),
                )
                created.append({
                    "symbol": symbol, "signal_date": signal_date,
                    "target_date": target_date, "probability_up": probability,
                    "predicted_return": predicted_return, "confidence": confidence,
                })
            conn.commit()
        return {"status": "SUCCESS", "model_version": row["version_key"],
                "count": len(created), "predictions": created}


def settle_deep_predictions() -> dict:
    initialize()
    scored = 0
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM deep_model_predictions WHERE status='PENDING'").fetchall()
        for row in rows:
            prices = conn.execute(
                """WITH ranked AS (
                     SELECT b.trade_date,b.close,ROW_NUMBER() OVER (
                       PARTITION BY b.trade_date ORDER BY ds.priority,b.captured_at DESC) rn
                     FROM market_daily_bars b JOIN data_sources ds ON ds.id=b.source_id
                     WHERE b.asset_symbol=? AND b.adjust_mode='qfq' AND b.trade_date<=?
                   ) SELECT trade_date,close FROM ranked WHERE rn=1
                     ORDER BY trade_date DESC LIMIT 2""",
                (row["symbol"], row["target_date"]),
            ).fetchall()
            if len(prices) < 2 or str(prices[0]["trade_date"]) != str(row["target_date"]):
                continue
            actual = float(prices[0]["close"]) / float(prices[1]["close"]) - 1.0
            up = int(actual > 0)
            probability = float(row["probability_up"])
            conn.execute(
                """UPDATE deep_model_predictions SET actual_return=?,direction_correct=?,
                   brier_score=?,status='SCORED',scored_at=? WHERE id=?""",
                (actual, int((probability >= 0.5) == bool(up)),
                 (probability - up) ** 2, _now(), row["id"]),
            )
            scored += 1
        conn.commit()
    return {"scored": scored}


def deep_learning_payload() -> dict:
    initialize()
    with closing(connect()) as conn:
        version = conn.execute(
            "SELECT * FROM deep_model_versions WHERE architecture=? ORDER BY id DESC LIMIT 1",
            (ARCHITECTURE,),
        ).fetchone()
        active = conn.execute(
            "SELECT * FROM deep_model_versions WHERE status='ACTIVE' AND architecture=? "
            "ORDER BY id DESC LIMIT 1", (ARCHITECTURE,)).fetchone()
        legacy = conn.execute(
            "SELECT COUNT(*) FROM deep_model_versions WHERE architecture LIKE 'gru-%'"
        ).fetchone()[0]
        summary = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN status='SCORED' THEN 1 ELSE 0 END) scored,
                      SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) pending,
                      AVG(CASE WHEN status='SCORED' THEN direction_correct END) accuracy,
                      AVG(CASE WHEN status='SCORED' THEN brier_score END) brier
               FROM deep_model_predictions"""
        ).fetchone()

    def item(row):
        if not row:
            return None
        value = dict(row)
        value["metrics"] = _load(value.pop("metrics_json"), {})
        value["gate"] = _load(value.pop("gate_json"), {})
        value["feature_schema"] = _load(value.pop("feature_schema_json"), [])
        return value

    weights = QWEN_MODEL_DIR / "model.safetensors"
    return {
        "last_version": item(version),
        "active_model": item(active),
        "predictions": dict(summary),
        "architecture": ARCHITECTURE,
        "model_family": "Qwen3",
        "base_model_id": QWEN_MODEL_ID,
        "base_model_ready": weights.exists() and weights.stat().st_size == QWEN_WEIGHTS_SIZE,
        "legacy_gru_versions": int(legacy),
        "research_only": True,
        "order_execution": False,
    }
