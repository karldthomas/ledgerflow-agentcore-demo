"""Command-line entry point for the deterministic LedgerFlow POC."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import InvestigationAgent, SimulatedCrash
from .db import Database
from .retrieval import HybridRetriever


def _database(args: argparse.Namespace) -> Database:
    db = Database(args.db)
    if getattr(args, "seed", True):
        db.seed(reset=getattr(args, "reset", False))
    return db


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledgerflow", description="LedgerFlow incident-response POC")
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed = subparsers.add_parser("seed", help="create or reset the deterministic dataset")
    seed.add_argument("--db", default=".ledgerflow/ledgerflow.db")
    seed.add_argument("--reset", action="store_true")

    investigate = subparsers.add_parser("investigate", help="run an evidence-grounded investigation")
    investigate.add_argument("customer_id")
    investigate.add_argument("--db", default=".ledgerflow/ledgerflow.db")
    investigate.add_argument("--tenant-id", default="tenant_acme")
    investigate.add_argument("--workflow-id")
    investigate.add_argument("--reset", action="store_true", help="reset the synthetic database before running")
    investigate.add_argument("--fail-after-step")

    demo = subparsers.add_parser("demo", help="seed and investigate in one command")
    demo.add_argument("--db", default=".ledgerflow/demo.db")
    demo.add_argument("--customer-id", default="ACME-482")
    demo.add_argument("--tenant-id", default="tenant_acme")
    demo.add_argument("--workflow-id", default="demo")

    status = subparsers.add_parser("status", help="inspect a persisted workflow")
    status.add_argument("workflow_id")
    status.add_argument("--db", default=".ledgerflow/ledgerflow.db")
    status.add_argument("--no-seed", dest="seed", action="store_false")

    retrieve = subparsers.add_parser("retrieve", help="run local hybrid document retrieval")
    retrieve.add_argument("query")
    retrieve.add_argument("--db", default=".ledgerflow/ledgerflow.db")
    retrieve.add_argument("--tenant-id", default="tenant_acme")
    retrieve.add_argument("--top-k", type=int, default=5)
    retrieve.add_argument("--strategy", choices=("keyword", "vector", "hybrid"), default="hybrid")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "seed":
        db = Database(args.db)
        try:
            _print(db.seed(reset=args.reset))
        finally:
            db.close()
        return 0
    if args.command == "demo":
        db = Database(args.db)
        try:
            counts = db.seed(reset=True)
            report = InvestigationAgent(db, tenant_id=args.tenant_id).investigate(
                args.customer_id, workflow_id=args.workflow_id
            )
            _print({"seed": counts, "report": report.to_dict()})
        finally:
            db.close()
        return 0
    if args.command == "status":
        db = _database(args)
        try:
            from .state import CheckpointStore

            state = CheckpointStore(db).load(args.workflow_id)
            _print(state.to_dict() if state else {"status": "not_found", "workflow_id": args.workflow_id})
        finally:
            db.close()
        return 0
    if args.command == "retrieve":
        db = _database(args)
        try:
            results = HybridRetriever(db).search(
                args.query, args.tenant_id, top_k=args.top_k, strategy=args.strategy, filters={"doc_type": "runbook"}
            )
            _print([result.to_dict() for result in results])
        finally:
            db.close()
        return 0
    if args.command == "investigate":
        db = _database(args)
        try:
            report = InvestigationAgent(db, tenant_id=args.tenant_id).investigate(
                args.customer_id, workflow_id=args.workflow_id, fail_after_step=args.fail_after_step
            )
            _print(report.to_dict())
            return 0
        except SimulatedCrash as exc:
            print(str(exc), file=sys.stderr)
            return 2
        finally:
            db.close()
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
