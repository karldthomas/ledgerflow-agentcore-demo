#!/usr/bin/env python3
"""Package Lambda code and optionally upload it to the foundation bucket."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def build_lambda_zip(output: Path, *, vendor_dependencies: bool = False, requirements: Path | None = None) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).parent.parent / "lambda" / "tool_handler.py"
    enhanced_source = Path(__file__).parent / "enhanced_index_handler.py"
    requirements = requirements or source.parent / "requirements.txt"
    with tempfile.TemporaryDirectory(prefix="ledgerflow-lambda-") as vendor_dir:
        if vendor_dependencies:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--target", vendor_dir, "-r", str(requirements)],
                check=True,
            )
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(source, "tool_handler.py")
            archive.write(enhanced_source, "enhanced_index_handler.py")
            if vendor_dependencies:
                vendor_root = Path(vendor_dir)
                for path in sorted(vendor_root.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(vendor_root))
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region")
    parser.add_argument("--key", default="lambda/tool_handler.zip")
    parser.add_argument("--output", type=Path, default=Path("cloudformation/build/tool_handler.zip"))
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--vendor-dependencies", action="store_true", help="bundle lambda/requirements.txt for a deterministic SDK version")
    parser.add_argument("--requirements", type=Path, default=Path("lambda/requirements.txt"))
    args = parser.parse_args(argv)
    digest = build_lambda_zip(args.output, vendor_dependencies=args.vendor_dependencies, requirements=args.requirements)
    if args.upload:
        try:
            import boto3
        except ImportError as exc:
            raise SystemExit("boto3 is required for --upload") from exc
        client = boto3.client("s3", region_name=args.region)
        client.upload_file(str(args.output), args.bucket, args.key, ExtraArgs={"Metadata": {"sha256": digest}})
    print(f"{args.output} sha256={digest} s3://{args.bucket}/{args.key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
