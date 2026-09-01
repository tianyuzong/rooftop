from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def runtime_revision(runtime_root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in (runtime_root / "app").rglob("*")
        if path.is_file() and path.suffix.lower() in {".py", ".js", ".css", ".html"}
    )
    for path in files:
        digest.update(path.relative_to(runtime_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: runtime_revision.py <runtime-root>")
    print(runtime_revision(Path(sys.argv[1])))
