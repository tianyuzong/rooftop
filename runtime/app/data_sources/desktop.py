"""Read-only probes for optional Windows desktop data bridges.

No account, order or trading method is imported.  A source becomes eligible for
failover only after its local client/path and quote entitlement are verified.
"""

import importlib.util
import os
import socket
from pathlib import Path


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=.15):
            return True
    except OSError:
        return False


def probe_desktop_sources() -> list[dict]:
    qmt_home = Path(os.environ["ARGUS_QMT_HOME"]) if os.environ.get("ARGUS_QMT_HOME") else None
    tdx_home = Path(os.environ["ARGUS_TDX_HOME"]) if os.environ.get("ARGUS_TDX_HOME") else None
    qmt_module = bool(importlib.util.find_spec("xtquant"))
    if not qmt_module and qmt_home:
        qmt_module = any(qmt_home.glob("**/xtquant/__init__.py"))
    tdx_files = 0
    if tdx_home and tdx_home.exists():
        tdx_files = sum(1 for _ in tdx_home.glob("vipdoc/*/lday/*.day"))
    return [
        {"code": "qmt", "ready": qmt_module, "mode": "xtquant read-only",
         "detail": "已发现 xtquant" if qmt_module else "设置 ARGUS_QMT_HOME 并启动券商 QMT/miniQMT"},
        {"code": "futu", "ready": _tcp_open("127.0.0.1", int(os.environ.get("ARGUS_FUTU_PORT", "11111"))),
         "mode": "OpenD local gateway", "detail": "检测本机 OpenD 端口；仍需核验对应市场行情权限"},
        {"code": "tdx_local", "ready": tdx_files > 0, "mode": "local .day files",
         "detail": f"发现 {tdx_files} 个日线文件" if tdx_files else "设置 ARGUS_TDX_HOME，并在通达信执行盘后数据下载"},
        {"code": "baostock", "ready": bool(importlib.util.find_spec("baostock")),
         "mode": "public API", "detail": "仅在实际覆盖与质量测试通过后启用"},
    ]
