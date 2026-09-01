"""Create and verify a consistent Argus SQLite backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the Argus SQLite system of record")
    parser.add_argument("--database", required=True)
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()
    if not 1 <= args.keep <= 365:
        raise ValueError("--keep must be between 1 and 365")

    database = Path(args.database).resolve(strict=True)
    output = Path(args.output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = output / f"argus-backup-{stamp}.db"
    partial = output / f".{target.name}.partial"
    if partial.exists():
        partial.unlink()

    source_uri = f"file:{database.as_posix()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=60)) as source:
        with closing(sqlite3.connect(partial)) as destination:
            source.backup(destination, pages=4096, sleep=0.05)
    with closing(sqlite3.connect(f"file:{partial.as_posix()}?mode=ro", uri=True)) as check:
        result = check.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"backup quick_check failed: {result}")
        tables = check.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    os.replace(partial, target)

    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_database": str(database),
        "backup_database": str(target),
        "bytes": target.stat().st_size,
        "sha256": sha256(target),
        "quick_check": result,
        "tables": int(tables),
        "includes": [
            "market and document indexes",
            "research mandates and runs",
            "versioned model definitions and assignments",
            "research signals, subscriptions, outbox and audit records",
        ],
        "excludes": ["raw provider files", "external model weights", "Python environment"],
    }
    metadata_path = target.with_suffix(".json")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    backups = sorted(output.glob("argus-backup-*.db"), key=lambda path: path.stat().st_mtime,
                     reverse=True)
    removed = []
    for old in backups[args.keep:]:
        old_metadata = old.with_suffix(".json")
        old.unlink()
        if old_metadata.is_file():
            old_metadata.unlink()
        removed.append(old.name)
    metadata["retention_keep"] = args.keep
    metadata["removed"] = removed
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
