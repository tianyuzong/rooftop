"""Start the plugin's bundled local Web application."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not (root / "app" / "server.py").is_file():
        raise SystemExit(f"plugin runtime is incomplete: {root}")
    os.chdir(root)
    sys.path.insert(0, str(root))
    from app.server import run

    run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
