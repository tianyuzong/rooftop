"""Isolated, tested and reversible source evolution for pure strategy rules."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import pprint
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import db
from .db import connect, initialize

ROOT = Path(__file__).resolve().parents[2]
EVOLVABLE_PATH = Path("runtime/app/evolvable/intraday_recipes.py")
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "input",
                   "os", "sys", "subprocess", "socket", "requests", "urllib", "pathlib"}
FORBIDDEN_TEXT = {"broker", "qmt", "futu", "place_order", "send_order",
                  "api_key", "bearer_token", "password", "secret"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_recipe_source(content: str) -> list[dict]:
    if len(content.encode("utf-8")) > 64_000:
        raise ValueError("evolvable source exceeds 64 KB")
    lowered = content.lower()
    found = sorted(term for term in FORBIDDEN_TEXT if term in lowered)
    if found:
        raise ValueError(f"forbidden source terms: {', '.join(found)}")
    tree = ast.parse(content)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if len(functions) != 1 or functions[0].name != "strategy_candidates":
        raise ValueError("recipe source must define only strategy_candidates")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Attribute, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda, ast.With, ast.AsyncWith, ast.Try)):
            raise ValueError(f"forbidden AST node: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise ValueError(f"forbidden name: {node.id}")
        if isinstance(node, ast.Call):
            raise ValueError("function calls are forbidden in evolvable recipes")
    returns = [node for node in ast.walk(functions[0]) if isinstance(node, ast.Return)]
    if len(returns) != 1:
        raise ValueError("strategy_candidates must contain one literal return")
    candidates = ast.literal_eval(returns[0].value)
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 40:
        raise ValueError("strategy candidate list size must be between 2 and 40")
    keys = set()
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("each strategy candidate must be a dictionary")
        required = {"strategy_key", "family", "stop_loss", "take_profit", "trailing_stop"}
        if not required.issubset(item):
            raise ValueError(f"missing required strategy fields: {sorted(required - set(item))}")
        if item["strategy_key"] in keys:
            raise ValueError("strategy_key must be unique")
        keys.add(item["strategy_key"])
        if item["family"] not in {"momentum", "mean_reversion", "breakout"}:
            raise ValueError(f"unsupported strategy family: {item['family']}")
        for field in ("stop_loss", "take_profit", "trailing_stop"):
            value = float(item[field])
            if not 0.001 <= value <= 0.30:
                raise ValueError(f"{field} is outside safety range")
    return candidates


def _mutated_library(base: list[dict], seed: dict | None = None,
                     generation: str = "next") -> list[dict]:
    result = [dict(item) for item in base]
    source = dict(seed or base[0])
    existing = {item["strategy_key"] for item in result}
    for suffix, factor in (("tight", 0.82), ("wide", 1.18)):
        item = source
        family = item["family"]
        variant = dict(item)
        variant["strategy_key"] = f"{item['strategy_key']}_evo_{generation}_{suffix}"
        if variant["strategy_key"] in existing:
            continue
        variant["trailing_stop"] = round(max(0.008, min(0.08,
                                                    item["trailing_stop"] * factor)), 6)
        if family in {"momentum", "breakout"}:
            variant["entry"] = round(max(0.0001, float(item["entry"]) * factor), 7)
        else:
            variant["entry_z"] = round(float(item["entry_z"]) * factor, 5)
        result.append(variant)
        existing.add(variant["strategy_key"])
    return result


def _render_recipes(candidates: list[dict]) -> str:
    literal = pprint.pformat(candidates, width=100, sort_dicts=False)
    return ('"""Versioned intraday candidate recipes; generated through gated evaluation."""\n\n\n'
            "def strategy_candidates():\n"
            f"    return {literal.replace(chr(10), chr(10) + '    ')}\n")


def create_automatic_candidate() -> dict:
    initialize()
    target = ROOT / EVOLVABLE_PATH
    before_bytes = target.read_bytes()
    before = before_bytes.decode("utf-8")
    from .evolvable.intraday_recipes import strategy_candidates
    base = strategy_candidates()
    with closing(connect()) as conn:
        latest = conn.execute(
            """SELECT c.params_json,r.data_end FROM intraday_strategy_candidates c
               JOIN intraday_strategy_runs r ON r.id=c.run_id
               WHERE c.selected=1 AND r.status='SUCCESS' ORDER BY r.id DESC LIMIT 1"""
        ).fetchone()
    seed = _load(latest["params_json"], {}) if latest else base[0]
    generation = str(latest["data_end"]).replace("-", "") if latest else datetime.now().strftime("%Y%m%d")
    proposed = _mutated_library(base, seed, generation)
    after = _render_recipes(proposed)
    _validate_recipe_source(after)
    patch_text = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=str(EVOLVABLE_PATH).replace("\\", "/"),
        tofile=str(EVOLVABLE_PATH).replace("\\", "/"),
    ))
    before_hash = _hash_bytes(before_bytes)
    patch_hash = _hash_bytes(patch_text.encode("utf-8"))
    candidate_key = f"code-{datetime.now().strftime('%Y%m%d')}-{patch_hash[:16]}"
    workspace = db.DATA_LAKE / "research" / "code_evolution" / candidate_key
    workspace.mkdir(parents=True, exist_ok=True)
    replacement = workspace / "replacement.py"
    replacement.write_text(after, encoding="utf-8", newline="\n")
    manifest = {"paths": [str(EVOLVABLE_PATH).replace("\\", "/")],
                "before_hash": before_hash, "replacement_path": str(replacement),
                "replacement_hash": _hash_bytes(replacement.read_bytes())}
    with closing(connect()) as conn:
        active = conn.execute(
            "SELECT version_key FROM code_evolution_versions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.execute(
            """INSERT INTO code_evolution_candidates
               (candidate_key,parent_version,status,title,rationale,patch_text,
                allowed_paths_json,workspace_path,patch_hash,created_by,created_at)
               VALUES(?,?,'DRAFT',?,?,?,?,?,?,?,?)
               ON CONFLICT(candidate_key) DO UPDATE SET status='DRAFT',patch_text=excluded.patch_text,
                 allowed_paths_json=excluded.allowed_paths_json,
                 workspace_path=excluded.workspace_path""",
            (candidate_key, str(active[0]) if active else None,
             "自动扩展分钟策略候选参数邻域",
             "依据当前纯策略库生成紧/宽两侧邻域；只以隔离回测和样本外门禁决定是否发布。",
             patch_text, _dump(manifest), str(workspace), patch_hash,
             "deterministic_recipe_search", _now()),
        )
        conn.commit()
    return {"candidate_key": candidate_key, "status": "DRAFT",
            "path": str(EVOLVABLE_PATH).replace("\\", "/"),
            "before_hash": before_hash, "replacement_hash": manifest["replacement_hash"],
            "candidate_count": len(proposed), "workspace": str(workspace)}


def _copy_database(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source = connect()
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def _run(command: list[str], cwd: Path, environment: dict, timeout: int) -> dict:
    started = time.perf_counter()
    completed = subprocess.run(
        command, cwd=str(cwd), env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return {"command": command, "returncode": completed.returncode,
            "stdout": completed.stdout[-12000:], "stderr": completed.stderr[-12000:],
            "duration_seconds": time.perf_counter() - started}


def _benchmark(runtime_dir: Path, environment: dict, symbols: Iterable[str],
               max_drawdown: float) -> tuple[dict, dict]:
    command = [sys.executable, "-m", "app.code_evolution_worker",
               "--max-drawdown", str(max_drawdown)]
    for symbol in symbols:
        command.extend(("--symbol", str(symbol)))
    check = _run(command, runtime_dir, environment, 300)
    if check["returncode"]:
        raise RuntimeError(f"isolated benchmark failed: {check['stderr'][-1000:]}")
    lines = [line for line in check["stdout"].splitlines() if line.strip()]
    try:
        metrics = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("isolated benchmark produced invalid JSON") from exc
    return check, metrics


def _prepare_sandbox(candidate: dict, manifest: dict) -> tuple[Path, Path, dict]:
    workspace = Path(str(candidate["workspace_path"]))
    sandbox = workspace / f"sandbox-{uuid.uuid4().hex[:8]}"
    runtime_dir = sandbox / "runtime"
    shutil.copytree(ROOT / "runtime" / "app", runtime_dir / "app",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "runtime" / "tests", runtime_dir / "tests",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "scripts", sandbox / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # The full regression suite checks published documentation and both plugin manifests.
    # Preserve these fixtures in the isolated repository; otherwise every candidate fails
    # due to missing files rather than its strategy behavior.
    for relative in ("runtime/docs", ".codex-plugin", ".zcode-plugin", "commands", "skills"):
        source = ROOT / relative
        if source.is_dir():
            shutil.copytree(source, sandbox / relative,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for relative in ("README.md", ".env.example", "LICENSE"):
        source = ROOT / relative
        if source.is_file():
            shutil.copy2(source, sandbox / relative)
    sandbox_lake = sandbox / "data_lake"
    _copy_database(sandbox_lake / "db" / "market_intelligence.db")
    environment = os.environ.copy()
    environment["ARGUS_DATA_LAKE"] = str(sandbox_lake)
    environment["PYTHONPATH"] = str(runtime_dir)
    environment["PYTHONHASHSEED"] = "0"
    replacement = Path(str(manifest["replacement_path"]))
    if _hash_bytes(replacement.read_bytes()) != manifest["replacement_hash"]:
        raise ValueError("replacement artifact hash mismatch")
    return sandbox, runtime_dir, environment


def _cleanup_sandbox(sandbox: Path) -> dict:
    """Remove an evaluation sandbox only when it is inside the managed root."""
    managed_root = (db.DATA_LAKE / "research" / "code_evolution").resolve()
    resolved = sandbox.resolve()
    result = {"path": str(resolved), "removed": False}
    try:
        relative = resolved.relative_to(managed_root)
        if len(relative.parts) < 2 or not resolved.name.startswith("sandbox-"):
            raise ValueError("sandbox path is outside the managed layout")
        if resolved.exists():
            shutil.rmtree(resolved)
        result["removed"] = True
    except Exception as exc:
        result["error"] = repr(exc)
    return result


def _promote(candidate: dict, manifest: dict, gate: dict, metrics: dict) -> dict:
    target = ROOT / manifest["paths"][0]
    if _hash_bytes(target.read_bytes()) != manifest["before_hash"]:
        raise RuntimeError("production source changed after candidate creation")
    replacement = Path(manifest["replacement_path"])
    version_key = f"source-{datetime.now().strftime('%Y%m%d%H%M%S')}-{candidate['patch_hash'][:10]}"
    backup_dir = db.DATA_LAKE / "backups" / "code_evolution" / version_key
    backup = backup_dir / manifest["paths"][0]
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, backup)
    temporary = target.with_name(f".{target.name}.{candidate['candidate_key']}.tmp")
    shutil.copy2(replacement, temporary)
    os.replace(temporary, target)
    if _hash_bytes(target.read_bytes()) != manifest["replacement_hash"]:
        shutil.copy2(backup, target)
        raise RuntimeError("promoted source hash mismatch; backup restored")
    version_manifest = {"files": [{"path": manifest["paths"][0],
                                     "before_hash": manifest["before_hash"],
                                     "after_hash": manifest["replacement_hash"],
                                     "backup": str(backup)}],
                        "gate": gate, "metrics": metrics}
    with closing(connect()) as conn:
        conn.execute("UPDATE code_evolution_versions SET status='ARCHIVED' WHERE status='ACTIVE'")
        conn.execute(
            """INSERT INTO code_evolution_versions
               (version_key,parent_version,candidate_id,status,manifest_json,backup_path,
                reason,created_at,activated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (version_key, candidate["parent_version"], candidate["id"], "ACTIVE",
             _dump(version_manifest), str(backup_dir),
             "all isolated checks and chronological holdout gates passed", _now(), _now()),
        )
        conn.execute(
            "UPDATE code_evolution_candidates SET status='PROMOTED',promoted_at=? WHERE id=?",
            (_now(), candidate["id"]),
        )
        conn.commit()
    return {"status": "PROMOTED", "version_key": version_key,
            "backup_path": str(backup_dir), "manifest": version_manifest}


def evaluate_candidate(candidate_key: str, symbols: Iterable[str],
                       max_drawdown: float = 0.15, auto_promote: bool = True) -> dict:
    initialize()
    symbols = list(dict.fromkeys(str(symbol) for symbol in symbols))
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM code_evolution_candidates WHERE candidate_key=?", (candidate_key,)
        ).fetchone()
    if not row:
        raise ValueError(f"unknown code candidate: {candidate_key}")
    candidate = dict(row)
    manifest = _load(candidate["allowed_paths_json"], {})
    if manifest.get("paths") != [str(EVOLVABLE_PATH).replace("\\", "/")]:
        raise ValueError("candidate path is outside the evolvable allowlist")
    replacement = Path(str(manifest.get("replacement_path", "")))
    _validate_recipe_source(replacement.read_text(encoding="utf-8"))
    sandbox, runtime_dir, environment = _prepare_sandbox(candidate, manifest)
    checks, started = [], _now()
    result = None
    try:
        baseline_check, baseline = _benchmark(runtime_dir, environment, symbols, max_drawdown)
        baseline_check["name"] = "baseline_backtest"
        checks.append(baseline_check)
        sandbox_target = sandbox / manifest["paths"][0]
        shutil.copy2(replacement, sandbox_target)
        compile_check = _run([sys.executable, "-m", "compileall", "-q", "app"],
                             runtime_dir, environment, 120)
        compile_check["name"] = "compileall"
        checks.append(compile_check)
        tests_check = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                           runtime_dir, environment, 900)
        tests_check["name"] = "full_unit_tests"
        checks.append(tests_check)
        candidate_check, candidate_metrics = _benchmark(
            runtime_dir, environment, symbols, max_drawdown)
        candidate_check["name"] = "candidate_backtest"
        checks.append(candidate_check)
        tests_passed = sum(line.rstrip().endswith("... ok") for line in tests_check["stderr"].splitlines())
        tests_failed = sum(marker in tests_check["stderr"] for marker in ("FAILED (", "ERROR"))
        gate = {
            "ast_safety_pass": True,
            "compile_pass": compile_check["returncode"] == 0,
            "full_tests_pass": tests_check["returncode"] == 0,
            "validation_improved": (candidate_metrics["validation_score"]
                                    > baseline["validation_score"] + 0.0001),
            "candidate_feasible": bool(candidate_metrics["feasible"]),
            "holdout_return_no_regression": (candidate_metrics["holdout"]["total_return"]
                                             >= baseline["holdout"]["total_return"] - 0.005),
            "holdout_risk_pass": (candidate_metrics["holdout"]["max_drawdown"] <= max_drawdown
                                  and candidate_metrics["holdout"]["max_drawdown"]
                                  <= baseline["holdout"]["max_drawdown"] + 0.01),
            "broker_execution_absent": True,
        }
        gate["passed"] = all(gate.values())
        status = "APPROVED" if gate["passed"] else "REJECTED"
        with closing(connect()) as conn:
            conn.execute(
                """INSERT INTO code_evolution_evaluations
                   (candidate_id,status,checks_json,tests_passed,tests_failed,regression_count,
                    baseline_metrics_json,candidate_metrics_json,gate_json,started_at,finished_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate["id"], status, _dump(checks), tests_passed, tests_failed,
                 int(not gate["holdout_return_no_regression"]), _dump(baseline),
                 _dump(candidate_metrics), _dump(gate), started, _now()),
            )
            conn.execute(
                "UPDATE code_evolution_candidates SET status=?,evaluated_at=? WHERE id=?",
                (status, _now(), candidate["id"]),
            )
            conn.commit()
        promotion = (_promote(candidate, manifest, gate,
                              {"baseline": baseline, "candidate": candidate_metrics})
                     if gate["passed"] and auto_promote else None)
        result = {"candidate_key": candidate_key,
                  "status": promotion["status"] if promotion else status,
                  "gate": gate, "baseline": baseline, "candidate": candidate_metrics,
                  "checks": [{"name": item["name"], "returncode": item["returncode"],
                              "duration_seconds": item["duration_seconds"]} for item in checks],
                  "promotion": promotion}
    except Exception as exc:
        with closing(connect()) as conn:
            conn.execute(
                """INSERT INTO code_evolution_evaluations
                   (candidate_id,status,checks_json,started_at,finished_at,error)
                   VALUES(?,'FAILED',?,?,?,?)""",
                (candidate["id"], _dump(checks), started, _now(), repr(exc)),
            )
            conn.execute("UPDATE code_evolution_candidates SET status='FAILED',evaluated_at=? WHERE id=?",
                         (_now(), candidate["id"]))
            conn.commit()
        raise
    finally:
        cleanup = _cleanup_sandbox(sandbox)
        if result is not None:
            result["sandbox_cleanup"] = cleanup
    return result


def run_automatic_code_evolution(symbols: Iterable[str], max_drawdown: float = 0.15,
                                 auto_promote: bool = True) -> dict:
    candidate = create_automatic_candidate()
    return evaluate_candidate(candidate["candidate_key"], symbols, max_drawdown, auto_promote)


def rollback_active_version(reason: str = "manual rollback") -> dict:
    initialize()
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM code_evolution_versions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {"status": "NO_ACTIVE_VERSION"}
    version = dict(row)
    manifest = _load(version["manifest_json"], {})
    restored = []
    for item in manifest.get("files", []):
        target = ROOT / item["path"]
        backup = Path(item["backup"])
        if not backup.exists() or _hash_bytes(backup.read_bytes()) != item["before_hash"]:
            raise RuntimeError(f"rollback backup invalid: {backup}")
        temporary = target.with_name(f".{target.name}.rollback.tmp")
        shutil.copy2(backup, temporary)
        os.replace(temporary, target)
        restored.append(item["path"])
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE code_evolution_versions SET status='ROLLED_BACK',rolled_back_at=?,reason=? WHERE id=?",
            (_now(), reason, version["id"]),
        )
        if version.get("parent_version"):
            conn.execute(
                "UPDATE code_evolution_versions SET status='ACTIVE',activated_at=? WHERE version_key=?",
                (_now(), version["parent_version"]),
            )
        conn.commit()
    return {"status": "ROLLED_BACK", "version_key": version["version_key"],
            "restored": restored, "reason": reason}


def code_evolution_payload() -> dict:
    initialize()
    with closing(connect()) as conn:
        candidate = conn.execute(
            "SELECT * FROM code_evolution_candidates ORDER BY id DESC LIMIT 1"
        ).fetchone()
        evaluation = conn.execute(
            "SELECT * FROM code_evolution_evaluations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        version = conn.execute(
            "SELECT * FROM code_evolution_versions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    candidate_item = dict(candidate) if candidate else None
    if candidate_item:
        candidate_item["allowed_paths"] = _load(candidate_item.pop("allowed_paths_json"), {})
        candidate_item.pop("patch_text", None)
    evaluation_item = dict(evaluation) if evaluation else None
    if evaluation_item:
        evaluation_item["checks"] = _load(evaluation_item.pop("checks_json"), [])
        evaluation_item["gate"] = _load(evaluation_item.pop("gate_json"), {})
        evaluation_item["baseline_metrics"] = _load(
            evaluation_item.pop("baseline_metrics_json"), {})
        evaluation_item["candidate_metrics"] = _load(
            evaluation_item.pop("candidate_metrics_json"), {})
    version_item = dict(version) if version else None
    if version_item:
        version_item["manifest"] = _load(version_item.pop("manifest_json"), {})
    return {"last_candidate": candidate_item, "last_evaluation": evaluation_item,
            "active_version": version_item,
            "automatic_code_changes": True, "automatic_activation": True,
            "isolation": "code copy + SQLite snapshot", "rollback_available": bool(version),
            "allowed_paths": [str(EVOLVABLE_PATH).replace("\\", "/")],
            "order_execution": False}
