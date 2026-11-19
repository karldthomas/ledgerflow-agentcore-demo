#!/usr/bin/env python3
"""Package the Lambda-served LedgerFlow web console."""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path


def build(output: Path) -> str:
    root = Path(__file__).parent.parent
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(root / "lambda" / "web_app.py", "web_app.py")
        archive.write(root / "lambda" / "bootstrap_admin.py", "bootstrap_admin.py")
        for path in sorted((root / "frontend").iterdir()):
            if path.is_file():
                archive.write(path, f"frontend/{path.name}")
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("cloudformation/build/web_app.zip"))
    args = parser.parse_args()
    digest = build(args.output)
    print(f"{args.output} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
