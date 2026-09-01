"""Isolated benchmark entry point used by the code-evolution gate."""

from __future__ import annotations

import argparse
import json

from .intraday_strategy import benchmark_candidate_library


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--max-drawdown", type=float, default=0.15)
    args = parser.parse_args()
    result = benchmark_candidate_library(args.symbol, args.interval, args.max_drawdown)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
