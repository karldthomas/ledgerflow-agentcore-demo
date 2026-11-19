"""Read-only data tools and authorization boundaries for the local agent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .db import Database
from .models import Evidence


class ToolError(RuntimeError):
    """Base class for tool boundary failures."""


class AuthorizationError(ToolError):
    """Raised when a request attempts to cross a tenant boundary."""


class ReadOnlyViolation(ToolError):
    """Raised when a caller requests a financial mutation."""


@dataclass(frozen=True)
class ToolTrace:
    name: str
    arguments: dict[str, Any]
    result_count: int


class ReadOnlyTools:
    """The only business-data interface exposed to the investigation agent.

    Every method requires a tenant scope for customer data and writes an audit
    event.  The production version is represented by Lambda targets behind
    AgentCore Gateway; this local version keeps the same authorization shape.
    """

    READ_ONLY_TOOL_NAMES = frozenset(
        {
            "search_incidents",
            "get_customer_invoices",
            "get_application_logs",
            "get_deployments",
            "get_current_invoice_state",
        }
    )

    def __init__(self, db: Database) -> None:
        self.db = db
        self.traces: list[ToolTrace] = []

    def _require_tenant(self, tenant_id: str | None) -> str:
        if not tenant_id:
            raise AuthorizationError("tenant_id is required for customer-scoped data")
        return tenant_id

    def _authorize_customer(self, customer_id: str, tenant_id: str) -> None:
        row = self.db.query_one("SELECT tenant_id FROM customers WHERE customer_id = ?", (customer_id,))
        if row is None:
            raise ToolError(f"unknown customer: {customer_id}")
        if row["tenant_id"] != tenant_id:
            raise AuthorizationError(f"customer {customer_id} is outside tenant scope")

    def _record(self, name: str, arguments: dict[str, Any], results: list[Evidence]) -> list[Evidence]:
        self.traces.append(ToolTrace(name=name, arguments=arguments, result_count=len(results)))
        self.db.log_event("tool_call", {"tool": name, "arguments": arguments, "result_count": len(results)})
        return results

    def invoke(self, name: str, **arguments: Any) -> Any:
        """Dispatch only explicitly registered read-only tools.

        Unknown names are rejected rather than being dynamically resolved.  In
        particular, names such as ``cancel_invoice`` never reach a backend.
        """

        if name not in self.READ_ONLY_TOOL_NAMES:
            if any(word in name.lower() for word in ("cancel", "refund", "write", "update", "delete")):
                raise ReadOnlyViolation(f"financial mutation is not available: {name}")
            raise ToolError(f"tool is not registered: {name}")
        handler = getattr(self, name)
        return handler(**arguments)

    def search_incidents(self, query: str, tenant_id: str) -> list[Evidence]:
        tenant_id = self._require_tenant(tenant_id)
        terms = [term for term in query.lower().split() if term]
        rows = self.db.query_all(
            """SELECT * FROM incidents
               WHERE tenant_id = ?
               ORDER BY created_at DESC""",
            (tenant_id,),
        )
        results: list[Evidence] = []
        for row in rows:
            haystack = " ".join(str(row[key]) for key in ("incident_id", "customer_id", "category", "description", "deployment_version"))
            normalized = haystack.lower()
            if terms and not all(term in normalized for term in terms):
                continue
            results.append(
                Evidence(
                    evidence_id=row["incident_id"],
                    source_type="incident",
                    title=f"{row['incident_id']} — {row['category']}",
                    content=(
                        f"Incident {row['incident_id']} for customer {row['customer_id']}: {row['description']} "
                        f"severity={row['severity']}, deployment={row['deployment_version']}, status={row['status']}"
                    ),
                    tenant_id=row["tenant_id"],
                    observed_at=row["created_at"],
                    metadata={
                        "customer_id": row["customer_id"],
                        "category": row["category"],
                        "description": row["description"],
                        "severity": row["severity"],
                        "status": row["status"],
                        "deployment_version": row["deployment_version"],
                    },
                    score=1.0,
                )
            )
        return self._record("search_incidents", {"query": query, "tenant_id": tenant_id}, results)

    def get_customer_invoices(self, customer_id: str, tenant_id: str) -> list[Evidence]:
        tenant_id = self._require_tenant(tenant_id)
        self._authorize_customer(customer_id, tenant_id)
        rows = self.db.query_all(
            "SELECT * FROM invoices WHERE customer_id = ? AND tenant_id = ? ORDER BY issued_at",
            (customer_id, tenant_id),
        )
        results = [
            Evidence(
                evidence_id=row["invoice_id"],
                source_type="invoice",
                title=f"Invoice {row['invoice_id']}",
                content=(
                    f"Invoice {row['invoice_id']} for {row['customer_id']} renewal {row['renewal_id']} "
                    f"subscription {row['subscription_id']} amount_cents={row['amount_cents']} status={row['status']}"
                ),
                tenant_id=row["tenant_id"],
                observed_at=row["issued_at"],
                metadata={
                    "customer_id": row["customer_id"],
                    "renewal_id": row["renewal_id"],
                    "subscription_id": row["subscription_id"],
                    "amount_cents": row["amount_cents"],
                    "status": row["status"],
                },
                score=1.0,
            )
            for row in rows
        ]
        return self._record("get_customer_invoices", {"customer_id": customer_id, "tenant_id": tenant_id}, results)

    def get_application_logs(self, customer_id: str, tenant_id: str, limit: int = 100) -> list[Evidence]:
        tenant_id = self._require_tenant(tenant_id)
        self._authorize_customer(customer_id, tenant_id)
        rows = self.db.query_all(
            """SELECT * FROM application_logs
               WHERE customer_id = ? AND tenant_id = ?
               ORDER BY timestamp LIMIT ?""",
            (customer_id, tenant_id, max(1, min(limit, 500))),
        )
        results: list[Evidence] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            results.append(
                Evidence(
                    evidence_id=row["log_id"],
                    source_type="application_log",
                    title=f"{row['log_id']} — {row['service']} {row['event']}",
                    content=(
                        f"Log {row['log_id']} at {row['timestamp']} service={row['service']} event={row['event']} "
                        f"payload={json.dumps(payload, sort_keys=True)}"
                    ),
                    tenant_id=row["tenant_id"],
                    observed_at=row["timestamp"],
                    metadata={"customer_id": row["customer_id"], "service": row["service"], "event": row["event"], **payload},
                    score=1.0,
                )
            )
        return self._record("get_application_logs", {"customer_id": customer_id, "tenant_id": tenant_id, "limit": limit}, results)

    def get_deployments(self, tenant_id: str, version: str | None = None) -> list[Evidence]:
        tenant_id = self._require_tenant(tenant_id)
        if version:
            rows = self.db.query_all(
                "SELECT * FROM deployments WHERE tenant_id = ? AND version = ? ORDER BY deployed_at",
                (tenant_id, version),
            )
        else:
            rows = self.db.query_all("SELECT * FROM deployments WHERE tenant_id = ? ORDER BY deployed_at", (tenant_id,))
        results = [
            Evidence(
                evidence_id=row["deployment_id"],
                source_type="deployment",
                title=f"Deployment {row['deployment_id']} version {row['version']}",
                content=(
                    f"Deployment {row['deployment_id']} version {row['version']} service={row['service']} "
                    f"status={row['status']} summary={row['change_summary']}"
                ),
                tenant_id=row["tenant_id"],
                observed_at=row["deployed_at"],
                metadata={
                    "version": row["version"],
                    "service": row["service"],
                    "change_summary": row["change_summary"],
                    "status": row["status"],
                },
                score=1.0,
            )
            for row in rows
        ]
        return self._record("get_deployments", {"tenant_id": tenant_id, "version": version}, results)

    def get_current_invoice_state(self, invoice_id: str, tenant_id: str) -> list[Evidence]:
        tenant_id = self._require_tenant(tenant_id)
        row = self.db.query_one("SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,))
        if row is None:
            raise ToolError(f"unknown invoice: {invoice_id}")
        if row["tenant_id"] != tenant_id:
            raise AuthorizationError(f"invoice {invoice_id} is outside tenant scope")
        result = self.get_customer_invoices(row["customer_id"], tenant_id)
        filtered = [item for item in result if item.evidence_id == invoice_id]
        self.traces.append(ToolTrace(name="get_current_invoice_state", arguments={"invoice_id": invoice_id, "tenant_id": tenant_id}, result_count=len(filtered)))
        self.db.log_event("tool_call", {"tool": "get_current_invoice_state", "arguments": {"invoice_id": invoice_id, "tenant_id": tenant_id}, "result_count": len(filtered)})
        return filtered


def tool_callable_map(tools: ReadOnlyTools) -> dict[str, Callable[..., Any]]:
    """Return an allow-listed map suitable for Gateway/Lambda adapters."""

    return {name: getattr(tools, name) for name in sorted(tools.READ_ONLY_TOOL_NAMES)}
