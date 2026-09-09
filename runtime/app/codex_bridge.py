"""Structured, tool-free Codex calls using the host's existing CLI login."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.request import getproxies

_probe_lock = threading.Lock()
_probe_cache: dict = {}


def validate_output(value, schema: dict, path: str = "result") -> None:
    """Validate the closed JSON-schema subset used by this bridge."""
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    matches = {"null": value is None, "object": isinstance(value, dict),
               "array": isinstance(value, list), "string": isinstance(value, str),
               "boolean": isinstance(value, bool),
               "number": type(value) in (int, float) and math.isfinite(value),
               "integer": type(value) is int}
    if not any(matches.get(k, False) for k in kinds):
        raise ValueError(f"Codex 返回字段类型错误：{path}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"Codex 返回字段不在允许范围：{path}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(k not in value for k in schema.get("required", [])):
            raise ValueError(f"Codex 返回缺少必填字段：{path}")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError(f"Codex 返回未知字段：{path}")
        for key, item in value.items():
            if key in properties:
                validate_output(item, properties[key], f"{path}.{key}")
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_output(item, schema["items"], f"{path}[{index}]")


def executable() -> str:
    configured = os.environ.get("ARGUS_CODEX_EXECUTABLE")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found:
        return found
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "OpenAI Codex CLI"
    candidates = sorted(root.glob("*/bin/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return str(candidates[0])
    raise RuntimeError("未找到 Codex CLI；请安装并运行 codex login。")


def status(force: bool = False) -> dict:
    with _probe_lock:
        if not force and time.monotonic() - _probe_cache.get("checked", -999) < 60:
            return dict(_probe_cache["value"])
        try:
            binary = executable()
            result = subprocess.run([binary, "login", "status"], capture_output=True,
                                    timeout=12, text=True, encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            logged_in = result.returncode == 0
            value = {"provider": "codex-cli", "ready": logged_in,
                     "status": "READY" if logged_in else "LOGIN_REQUIRED",
                     "model": os.environ.get("ARGUS_CODEX_MODEL") or "Codex 默认模型",
                     "authentication": "本机 Codex 登录", "shell_tools": False,
                     "detail": "已登录" if logged_in else "请在本机运行 codex login"}
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            value = {"provider": "codex-cli", "ready": False, "status": "UNAVAILABLE",
                     "detail": str(exc)[:300], "shell_tools": False}
        _probe_cache.update(checked=time.monotonic(), value=value)
        return dict(value)


def structured_call(prompt: str, schema: dict, directory: Path,
                    cancel: threading.Event | None = None, timeout: int = 240) -> dict:
    """The process receives only the supplied research context, never service tokens."""
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-", dir=directory) as temporary:
        folder = Path(temporary)
        schema_path, result_path = folder / "schema.json", folder / "result.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        command = [executable(), "exec", "--ignore-user-config", "--skip-git-repo-check",
                   "--ephemeral", "--sandbox", "read-only", "--disable", "shell_tool",
                   "--disable", "apps", "--disable", "multi_agent", "--disable", "skill_search",
                   "--disable", "shell_snapshot", "-c", "features.skip_host_skill_discovery=true",
                   "-c", "suppress_unstable_features_warning=true", "-c", 'approval_policy="never"',
                   "-c", 'web_search="disabled"', "-c", 'model_reasoning_effort="medium"',
                   "--output-schema", str(schema_path), "-o", str(result_path), "--json", "-"]
        if os.environ.get("ARGUS_CODEX_MODEL"):
            command[2:2] = ["--model", os.environ["ARGUS_CODEX_MODEL"]]
        # Remove Rooftop credentials from the child environment. Codex owns its login.
        child_env = {k: v for k, v in os.environ.items() if not
                     (k.startswith("ARGUS_") and any(s in k for s in ("TOKEN", "SECRET", "PASSWORD")))}
        # Rust's HTTP client does not read the Windows system proxy itself.
        for scheme, proxy in getproxies().items():
            if scheme in {"http", "https"} and proxy:
                child_env.setdefault(scheme.upper() + "_PROXY", proxy)
        with (folder / "events.jsonl").open("w", encoding="utf-8") as out, \
                (folder / "stderr.log").open("w", encoding="utf-8") as err:
            process = subprocess.Popen(command, cwd=folder, env=child_env, stdin=subprocess.PIPE,
                                       stdout=out, stderr=err,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            deadline = time.monotonic() + timeout
            try:
                process.stdin.write(prompt.encode("utf-8"))
                process.stdin.close()
                while process.poll() is None:
                    if cancel and cancel.wait(.25):
                        raise RuntimeError("研究已取消")
                    if not cancel:
                        time.sleep(.25)
                    if time.monotonic() > deadline:
                        raise TimeoutError("Codex 响应超时，可重试；已保存此前研究步骤。")
                if process.returncode != 0 or not result_path.exists():
                    events = (folder / "events.jsonl").read_text(encoding="utf-8", errors="replace")
                    errors = []
                    for line in events.splitlines():
                        try:
                            event = json.loads(line)
                            if event.get("type") in {"error", "turn.failed"}:
                                errors.append(str(event.get("message") or event.get("error")))
                        except ValueError:
                            pass
                    detail = "; ".join(errors)[-400:] or f"退出码 {process.returncode}，请检查 Codex 登录和用量。"
                    raise RuntimeError("Codex 调用失败：" + detail)
                result = json.loads(result_path.read_text(encoding="utf-8-sig"))
                validate_output(result, schema)
                usage = {}
                for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                        if event.get("type") == "turn.completed":
                            usage = event.get("usage", {})
                    except ValueError:
                        pass
                return {"data": result, "usage": usage, "provider": "codex-cli"}
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
