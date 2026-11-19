"""Small adapter boundary for an AgentCore Runtime container.

The checked-in Runtime image uses the dependency-light LedgerFlow orchestrator.
It can optionally synthesize the final report with Bedrock Converse while the
Gateway, authorization, retrieval, and checkpoint boundaries remain explicit.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .agent import InvestigationAgent
from .db import Database


class RuntimeHandler:
    def __init__(self, database_path: str | None = None) -> None:
        self.cloud = os.environ.get("LEDGERFLOW_BACKEND", "sqlite").lower() == "aws"
        if self.cloud:
            from .cloud_agent import CloudInvestigationHandler

            self.cloud_handler = CloudInvestigationHandler()
            self.db = None
            return
        path = database_path or os.environ.get("LEDGERFLOW_DB_PATH", ".ledgerflow/runtime.db")
        self.db = Database(path)
        self.db.seed()

    def handle_invocation(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.cloud:
            return self.cloud_handler.handle(payload)
        customer_id = str(payload["customer_id"])
        tenant_id = str(payload.get("tenant_id", "tenant_acme"))
        report = InvestigationAgent(self.db, tenant_id=tenant_id).investigate(
            customer_id,
            workflow_id=payload.get("workflow_id"),
            request=payload.get("request"),
        )
        return report.to_dict()

    def close(self) -> None:
        if self.db is not None:
            self.db.close()


def main() -> None:
    handler = RuntimeHandler()
    try:
        for line in __import__("sys").stdin:
            if line.strip():
                print(json.dumps(handler.handle_invocation(json.loads(line))))
    finally:
        handler.close()


if __name__ == "__main__":
    main()
