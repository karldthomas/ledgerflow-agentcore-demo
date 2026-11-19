"""Deterministic deployment fixtures staged through an in-memory SQLite store."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    plan TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    description TEXT NOT NULL,
    deployment_version TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    subscription_id TEXT NOT NULL,
    renewal_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    issued_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_logs (
    log_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    service TEXT NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    version TEXT NOT NULL,
    deployed_at TEXT NOT NULL,
    service TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    tenant_id TEXT,
    doc_type TEXT NOT NULL,
    title TEXT NOT NULL,
    version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    authority INTEGER NOT NULL DEFAULT 1,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_records (
    memory_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source_id TEXT NOT NULL,
    confidence REAL NOT NULL,
    observed_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    authority INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS checkpoints (
    workflow_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    status TEXT NOT NULL,
    state_json TEXT NOT NULL,
    checkpoint_version INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_ledger (
    operation_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incidents_customer ON incidents(customer_id);
CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_id);
CREATE INDEX IF NOT EXISTS idx_logs_customer_time ON application_logs(customer_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_documents_tenant_type ON documents(tenant_id, doc_type);
CREATE INDEX IF NOT EXISTS idx_memory_active ON memory_records(tenant_id, subject_id, key, status);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Extra tenant_acme cases keep the deployed demo useful after the original
# ACME-482 walkthrough.  Each case has matching invoices and logs generated
# below, so the evidence graph is coherent instead of being a collection of
# unrelated incident rows.
EXTRA_ACME_CASES: tuple[dict[str, str], ...] = (
    {
        "customer_id": "ACME-484",
        "incident_id": "INC-1047",
        "category": "payment_timeout",
        "severity": "medium",
        "description": "Renewal timed out and the provider outcome needs reconciliation.",
        "deployment_version": "3.8.3",
        "status": "open",
        "scenario": "timeout_unresolved",
    },
    {
        "customer_id": "ACME-485",
        "incident_id": "INC-1048",
        "category": "configuration_error",
        "severity": "high",
        "description": "Payment failed because the merchant profile is invalid.",
        "deployment_version": "3.8.1",
        "status": "open",
        "scenario": "configuration_error",
    },
    {
        "customer_id": "ACME-486",
        "incident_id": "INC-1049",
        "category": "payment_failure",
        "severity": "medium",
        "description": "The provider declined the renewal payment.",
        "deployment_version": "3.8.1",
        "status": "open",
        "scenario": "payment_failure",
    },
    {
        "customer_id": "ACME-487",
        "incident_id": "INC-1050",
        "category": "duplicate_invoice",
        "severity": "high",
        "description": "A retry generated a second invoice for the same renewal.",
        "deployment_version": "3.8.2",
        "status": "open",
        "scenario": "duplicate_invoice",
    },
    {
        "customer_id": "ACME-488",
        "incident_id": "INC-1051",
        "category": "payment_timeout",
        "severity": "low",
        "description": "A timeout was reconciled and the provider reported success.",
        "deployment_version": "3.8.2",
        "status": "closed",
        "scenario": "timeout_reconciled",
    },
    {
        "customer_id": "ACME-489",
        "incident_id": "INC-1052",
        "category": "duplicate_invoice",
        "severity": "high",
        "description": "Two invoices were issued after a payment retry.",
        "deployment_version": "3.8.2",
        "status": "open",
        "scenario": "duplicate_invoice",
    },
    {
        "customer_id": "ACME-490",
        "incident_id": "INC-1053",
        "category": "payment_failure",
        "severity": "medium",
        "description": "A payment failed after the provider rejected the request.",
        "deployment_version": "3.8.3",
        "status": "open",
        "scenario": "payment_failure",
    },
    {
        "customer_id": "ACME-491",
        "incident_id": "INC-1054",
        "category": "configuration_error",
        "severity": "medium",
        "description": "A merchant configuration mismatch blocked payment processing.",
        "deployment_version": "3.8.1",
        "status": "closed",
        "scenario": "configuration_error",
    },
    {
        "customer_id": "ACME-492",
        "incident_id": "INC-1055",
        "category": "payment_timeout",
        "severity": "medium",
        "description": "The provider timed out and no final settlement status is recorded.",
        "deployment_version": "3.8.3",
        "status": "open",
        "scenario": "timeout_unresolved",
    },
    {
        "customer_id": "ACME-493",
        "incident_id": "INC-1056",
        "category": "duplicate_invoice",
        "severity": "high",
        "description": "A regenerated retry key resulted in duplicate billing records.",
        "deployment_version": "3.8.2",
        "status": "open",
        "scenario": "duplicate_invoice",
    },
    {
        "customer_id": "ACME-494",
        "incident_id": "INC-1057",
        "category": "payment_failure",
        "severity": "low",
        "description": "The provider rejected the payment for insufficient funds.",
        "deployment_version": "3.8.1",
        "status": "closed",
        "scenario": "payment_failure",
    },
    {
        "customer_id": "ACME-495",
        "incident_id": "INC-1058",
        "category": "payment_timeout",
        "severity": "low",
        "description": "A timeout was followed by a safe retry using the original key.",
        "deployment_version": "3.8.2",
        "status": "closed",
        "scenario": "timeout_reconciled",
    },
)


class Database:
    """A fixture staging store with an in-memory default."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.initialize()

    def initialize(self) -> None:
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, params)

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    def log_event(self, event_type: str, detail: dict[str, Any], workflow_id: str | None = None) -> None:
        self.execute(
            "INSERT INTO audit_events(workflow_id, event_type, detail_json, created_at) VALUES (?, ?, ?, ?)",
            (workflow_id, event_type, json.dumps(detail, sort_keys=True), utc_now().isoformat()),
        )
        self.connection.commit()

    def seed(self, reset: bool = False) -> dict[str, int]:
        """Populate the deterministic synthetic dataset.

        The generated volume is intentionally close to the case study's target
        sizes while keeping the meaningful incident records easy to inspect.
        """

        if reset:
            for table in (
                "audit_events",
                "operation_ledger",
                "checkpoints",
                "memory_records",
                "documents",
                "deployments",
                "application_logs",
                "invoices",
                "incidents",
                "customers",
            ):
                self.execute(f"DELETE FROM {table}")
            self.connection.commit()
        elif self.query_one("SELECT 1 FROM incidents LIMIT 1") is not None:
            return self.counts()

        self._seed_customers()
        self._seed_incidents()
        self._seed_invoices()
        self._seed_logs()
        self._seed_deployments()
        self._seed_documents()
        self._seed_memories()
        self.connection.commit()
        return self.counts()

    def counts(self) -> dict[str, int]:
        tables = (
            "customers",
            "incidents",
            "invoices",
            "application_logs",
            "deployments",
            "documents",
            "memory_records",
        )
        return {table: int(self.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]) for table in tables}

    def _seed_customers(self) -> None:
        rows: list[tuple[Any, ...]] = [
            ("ACME-482", "tenant_acme", "ACME Corporation", "enterprise", 1),
            ("ACME-483", "tenant_acme", "ACME Labs", "enterprise", 1),
            ("BETA-101", "tenant_beta", "Beta Systems", "growth", 1),
            ("CONFIG-77", "tenant_config", "Config Works", "starter", 1),
            ("OMEGA-009", "tenant_omega", "Omega Retail", "growth", 1),
        ]
        rows.extend(
            (
                case["customer_id"],
                "tenant_acme",
                f"Synthetic ACME Customer {case['customer_id'].split('-', 1)[1]}",
                "enterprise",
                1,
            )
            for case in EXTRA_ACME_CASES
        )
        for index in range(1, 196):
            customer_id = f"CUSTOMER-{index:03d}"
            rows.append((customer_id, f"tenant_{index:03d}", f"Synthetic Customer {index:03d}", "growth", 1))
        self.connection.executemany(
            "INSERT INTO customers(customer_id, tenant_id, name, plan, active) VALUES (?, ?, ?, ?, ?)", rows
        )

    def _seed_incidents(self) -> None:
        base = "2026-11-15T10:00:00+00:00"
        rows: list[tuple[Any, ...]] = [
            (
                "INC-1042",
                "ACME-482",
                "tenant_acme",
                "duplicate_invoice",
                "high",
                "Customer received two invoices for one renewal.",
                "3.8.2",
                "open",
                base,
            ),
            (
                "INC-1043",
                "ACME-483",
                "tenant_acme",
                "duplicate_invoice",
                "medium",
                "Duplicate invoice reported after a provider timeout.",
                "3.8.2",
                "open",
                "2026-11-15T11:00:00+00:00",
            ),
            (
                "INC-1044",
                "BETA-101",
                "tenant_beta",
                "payment_timeout",
                "medium",
                "Renewal timed out; payment outcome needs reconciliation.",
                "3.8.1",
                "open",
                "2026-11-15T12:00:00+00:00",
            ),
            (
                "INC-1045",
                "CONFIG-77",
                "tenant_config",
                "configuration_error",
                "high",
                "Payment failed because the merchant configuration is invalid.",
                "3.8.1",
                "open",
                "2026-11-15T13:00:00+00:00",
            ),
            (
                "INC-1046",
                "OMEGA-009",
                "tenant_omega",
                "payment_timeout",
                "low",
                "Timeout observed but provider status is successful.",
                "3.8.2",
                "closed",
                "2026-11-15T14:00:00+00:00",
            ),
        ]
        for offset, case in enumerate(EXTRA_ACME_CASES):
            rows.append(
                (
                    case["incident_id"],
                    case["customer_id"],
                    "tenant_acme",
                    case["category"],
                    case["severity"],
                    case["description"],
                    case["deployment_version"],
                    case["status"],
                    f"2026-11-16T{(8 + offset):02d}:00:00+00:00",
                )
            )
        categories = ("payment_timeout", "configuration_error", "duplicate_invoice", "payment_failure")
        for index in range(1, 96):
            customer_number = ((index - 1) % 195) + 1
            customer_id = f"CUSTOMER-{customer_number:03d}"
            rows.append(
                (
                    f"INC-{2000 + index}",
                    customer_id,
                    f"tenant_{customer_number:03d}",
                    categories[index % len(categories)],
                    "low" if index % 3 else "medium",
                    f"Synthetic payment investigation record {index}.",
                    "3.8.1" if index % 2 else "3.8.2",
                    "closed" if index % 4 else "open",
                    f"2026-11-{(index % 14) + 1:02d}T09:00:00+00:00",
                )
            )
        self.connection.executemany(
            """INSERT INTO incidents(
                incident_id, customer_id, tenant_id, category, severity, description,
                deployment_version, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def _seed_invoices(self) -> None:
        rows: list[tuple[Any, ...]] = [
            ("INV-8821", "ACME-482", "tenant_acme", "SUB-ACME-482", "REN-2026-11", 12900, "issued", "2026-11-15T10:42:03+00:00"),
            ("INV-8822", "ACME-482", "tenant_acme", "SUB-ACME-482", "REN-2026-11", 12900, "issued", "2026-11-15T10:42:09+00:00"),
            ("INV-8830", "ACME-483", "tenant_acme", "SUB-ACME-483", "REN-2026-11", 9900, "issued", "2026-11-15T11:42:03+00:00"),
            ("INV-8840", "BETA-101", "tenant_beta", "SUB-BETA-101", "REN-2026-11", 4900, "pending", "2026-11-15T12:42:03+00:00"),
            ("INV-8850", "CONFIG-77", "tenant_config", "SUB-CONFIG-77", "REN-2026-11", 2900, "failed", "2026-11-15T13:42:03+00:00"),
        ]
        for offset, case in enumerate(EXTRA_ACME_CASES):
            renewal_id = f"REN-2026-{12 + (offset % 3):02d}"
            amount_cents = 7900 + (offset * 300)
            issued_at = f"2026-11-16T{(8 + offset):02d}:42:03+00:00"
            status = {
                "timeout_unresolved": "pending",
                "timeout_reconciled": "paid",
                "configuration_error": "failed",
                "payment_failure": "failed",
                "duplicate_invoice": "issued",
            }[case["scenario"]]
            invoice_base = 8841 + (offset * 10)
            invoice_values = [
                (
                    f"INV-{invoice_base}",
                    case["customer_id"],
                    "tenant_acme",
                    f"SUB-{case['customer_id']}",
                    renewal_id,
                    amount_cents,
                    status,
                    issued_at,
                )
            ]
            if case["scenario"] == "duplicate_invoice":
                invoice_values.append(
                    (
                        f"INV-{invoice_base + 1}",
                        case["customer_id"],
                        "tenant_acme",
                        f"SUB-{case['customer_id']}",
                        renewal_id,
                        amount_cents,
                        status,
                        f"2026-11-16T{(8 + offset):02d}:42:09+00:00",
                    )
                )
            rows.extend(invoice_values)
        for index in range(1, 196):
            customer_number = ((index - 1) % 195) + 1
            customer_id = f"CUSTOMER-{customer_number:03d}"
            rows.append(
                (
                    f"INV-{9000 + index}",
                    customer_id,
                    f"tenant_{customer_number:03d}",
                    f"SUB-{customer_id}",
                    f"REN-2026-{(index % 3) + 9:02d}",
                    1000 + (index % 20) * 500,
                    "paid" if index % 5 else "pending",
                    f"2026-11-{(index % 14) + 1:02d}T08:00:00+00:00",
                )
            )
        self.connection.executemany(
            """INSERT INTO invoices(
                invoice_id, customer_id, tenant_id, subscription_id, renewal_id,
                amount_cents, status, issued_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def _seed_logs(self) -> None:
        rows: list[tuple[Any, ...]] = [
            (
                "LOG-8820",
                "ACME-482",
                "tenant_acme",
                "2026-11-15T10:41:58+00:00",
                "payment-service",
                "payment_attempt",
                json.dumps({"payment_id": "pay_7721", "idempotency_key": "pay_7721", "amount_cents": 12900}),
            ),
            (
                "LOG-8821",
                "ACME-482",
                "tenant_acme",
                "2026-11-15T10:42:00+00:00",
                "payment-retry-handler",
                "retry_attempt",
                json.dumps(
                    {
                        "original_idempotency_key": "pay_7721",
                        "retry_idempotency_key": "pay_9934",
                        "error": "PaymentTimeoutException",
                        "attempt": 2,
                    }
                ),
            ),
            (
                "LOG-8822",
                "ACME-482",
                "tenant_acme",
                "2026-11-15T10:42:03+00:00",
                "invoice-service",
                "invoice_created",
                json.dumps({"invoice_id": "INV-8821", "renewal_id": "REN-2026-11", "payment_id": "pay_7721"}),
            ),
            (
                "LOG-8823",
                "ACME-482",
                "tenant_acme",
                "2026-11-15T10:42:09+00:00",
                "invoice-service",
                "invoice_created",
                json.dumps({"invoice_id": "INV-8822", "renewal_id": "REN-2026-11", "payment_id": "pay_9934"}),
            ),
            (
                "LOG-8830",
                "ACME-483",
                "tenant_acme",
                "2026-11-15T11:42:00+00:00",
                "payment-retry-handler",
                "retry_attempt",
                json.dumps({"original_idempotency_key": "pay_7831", "retry_idempotency_key": "pay_7831", "error": "PaymentTimeoutException"}),
            ),
            (
                "LOG-8840",
                "BETA-101",
                "tenant_beta",
                "2026-11-15T12:42:00+00:00",
                "payment-service",
                "payment_timeout",
                json.dumps({"provider_status": "unknown", "error": "PaymentTimeoutException", "payment_id": "pay_beta_1"}),
            ),
            (
                "LOG-8850",
                "CONFIG-77",
                "tenant_config",
                "2026-11-15T13:42:00+00:00",
                "payment-service",
                "payment_failed",
                json.dumps({"error": "InvalidMerchantConfiguration", "merchant_profile": "cfg-77"}),
            ),
        ]
        for offset, case in enumerate(EXTRA_ACME_CASES):
            customer_id = case["customer_id"]
            base_time = datetime(2026, 11, 16, 8 + offset, 42, 0, tzinfo=timezone.utc)
            original_key = f"pay_{customer_id.lower().replace('-', '_')}"
            rows.append(
                (
                    f"LOG-{customer_id}-01",
                    customer_id,
                    "tenant_acme",
                    base_time.isoformat(),
                    "payment-service",
                    "payment_attempt",
                    json.dumps({"payment_id": original_key, "idempotency_key": original_key, "amount_cents": 7900 + (offset * 300)}),
                )
            )
            scenario = case["scenario"]
            if scenario in {"timeout_unresolved", "timeout_reconciled", "duplicate_invoice"}:
                retry_key = original_key if scenario == "timeout_reconciled" else f"{original_key}_retry"
                payload: dict[str, Any] = {
                    "original_idempotency_key": original_key,
                    "retry_idempotency_key": retry_key,
                    "error": "PaymentTimeoutException",
                    "attempt": 2,
                }
                if scenario == "timeout_reconciled":
                    payload["provider_status"] = "successful"
                rows.append(
                    (
                        f"LOG-{customer_id}-02",
                        customer_id,
                        "tenant_acme",
                        (base_time + timedelta(seconds=2)).isoformat(),
                        "payment-retry-handler",
                        "retry_attempt",
                        json.dumps(payload),
                    )
                )
                if scenario == "duplicate_invoice":
                    invoice_base = 8841 + (offset * 10)
                    for sequence, invoice_id in enumerate((f"INV-{invoice_base}", f"INV-{invoice_base + 1}"), start=3):
                        payment_id = original_key if sequence == 3 else retry_key
                        rows.append(
                            (
                                f"LOG-{customer_id}-{sequence:02d}",
                                customer_id,
                                "tenant_acme",
                                (base_time + timedelta(seconds=sequence + 1)).isoformat(),
                                "invoice-service",
                                "invoice_created",
                                json.dumps(
                                    {
                                        "invoice_id": invoice_id,
                                        "payment_id": payment_id,
                                        "renewal_id": f"REN-2026-{12 + (offset % 3):02d}",
                                    }
                                ),
                            )
                        )
            elif scenario == "configuration_error":
                rows.append(
                    (
                        f"LOG-{customer_id}-02",
                        customer_id,
                        "tenant_acme",
                        (base_time + timedelta(seconds=2)).isoformat(),
                        "payment-service",
                        "payment_failed",
                        json.dumps({"error": "InvalidMerchantConfiguration", "provider_status": "rejected"}),
                    )
                )
            else:
                rows.append(
                    (
                        f"LOG-{customer_id}-02",
                        customer_id,
                        "tenant_acme",
                        (base_time + timedelta(seconds=2)).isoformat(),
                        "payment-service",
                        "payment_failed",
                        json.dumps({"error": "ProviderDeclined" if offset % 2 == 0 else "InsufficientFunds", "provider_status": "declined"}),
                    )
                )
        base_time = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
        for index in range(1, 4994):
            customer_number = ((index - 1) % 195) + 1
            customer_id = f"CUSTOMER-{customer_number:03d}"
            timestamp = (base_time + timedelta(minutes=index)).isoformat()
            event = ("payment_attempt", "payment_succeeded", "payment_timeout", "retry_attempt")[index % 4]
            payload: dict[str, Any] = {"payment_id": f"pay-synthetic-{index:05d}"}
            if event == "retry_attempt":
                payload.update(
                    {
                        "original_idempotency_key": f"pay-synthetic-{index:05d}",
                        "retry_idempotency_key": f"pay-synthetic-{index:05d}",
                    }
                )
            rows.append(
                (
                    f"LOG-{100000 + index}",
                    customer_id,
                    f"tenant_{customer_number:03d}",
                    timestamp,
                    "payment-service" if index % 2 else "payment-retry-handler",
                    event,
                    json.dumps(payload),
                )
            )
        self.connection.executemany(
            """INSERT INTO application_logs(
                log_id, customer_id, tenant_id, timestamp, service, event, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def _seed_deployments(self) -> None:
        rows: list[tuple[Any, ...]] = [
            ("DEP-381", "tenant_acme", "3.8.1", "2026-11-01T09:00:00+00:00", "payment-retry-handler", "Retry reused the original idempotency key.", "stable"),
            ("DEP-382", "tenant_acme", "3.8.2", "2026-11-14T18:00:00+00:00", "payment-retry-handler", "Refactored retry handler; retry key is regenerated after a timeout.", "active"),
            ("DEP-383", "tenant_acme", "3.8.3", "2026-11-16T18:00:00+00:00", "payment-retry-handler", "Restores original idempotency key reuse and adds timeout reconciliation.", "staged"),
        ]
        for offset, case in enumerate(EXTRA_ACME_CASES):
            if case["scenario"] == "configuration_error":
                service = "merchant-config"
                summary = "Updated merchant profile validation for payment requests."
            elif case["scenario"] == "payment_failure":
                service = "payment-service"
                summary = "Updated provider response handling for declined payments."
            elif case["scenario"] == "timeout_reconciled":
                service = "payment-retry-handler"
                summary = "Added provider reconciliation before retry completion."
            else:
                service = "payment-retry-handler"
                summary = "Updated timeout and retry handling for synthetic case coverage."
            rows.append(
                (
                    f"DEP-{case['customer_id']}",
                    "tenant_acme",
                    case["deployment_version"],
                    f"2026-11-16T{(7 + offset):02d}:00:00+00:00",
                    service,
                    summary,
                    "active" if case["status"] == "open" else "stable",
                )
            )
        for index in range(4, 31):
            tenant_number = ((index - 4) % 10) + 1
            rows.append(
                (
                    f"DEP-{400 + index}",
                    f"tenant_{tenant_number:03d}",
                    f"3.{index // 10}.{index % 10}",
                    f"2026-10-{(index % 20) + 1:02d}T18:00:00+00:00",
                    "payment-service",
                    "Routine synthetic deployment.",
                    "stable",
                )
            )
        self.connection.executemany(
            """INSERT INTO deployments(
                deployment_id, tenant_id, version, deployed_at, service,
                change_summary, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def _seed_documents(self) -> None:
        docs: list[tuple[Any, ...]] = [
            (
                "RUNBOOK-014",
                None,
                "runbook",
                "Idempotent Retry Handling",
                "3",
                "2026-11-16T08:00:00+00:00",
                5,
                """Payment retries must reuse the original idempotency key to prevent duplicate financial operations.

If the payment provider returns a timeout, verify the original operation status before retrying.
Do not create a new payment operation until the previous outcome has been reconciled.""",
            ),
            (
                "RUNBOOK-008",
                None,
                "runbook",
                "Payment Timeout Reconciliation",
                "4",
                "2026-11-15T08:00:00+00:00",
                5,
                "A timeout is not proof of failure. Query the provider using the original payment identifier and reconcile the outcome before retrying.",
            ),
            (
                "RUNBOOK-002",
                None,
                "runbook",
                "Merchant Configuration Failures",
                "2",
                "2026-10-30T08:00:00+00:00",
                4,
                "Invalid merchant configuration errors must be corrected in the tenant profile and verified with a read-only payment attempt.",
            ),
            (
                "RUNBOOK-013",
                "tenant_acme",
                "runbook",
                "Legacy Retry Guidance",
                "1",
                "2026-08-01T08:00:00+00:00",
                2,
                "Older guidance allowed a retry to use a new request key. This document is obsolete and must not override the current retry runbook.",
            ),
            (
                "TENANT-OTHER-014",
                "tenant_other",
                "runbook",
                "Idempotent Retry Handling - Other Tenant",
                "9",
                "2026-11-17T08:00:00+00:00",
                5,
                "Highly relevant private guidance for another tenant. It must never be returned to an ACME investigation.",
            ),
            (
                "POLICY-APPROVAL-001",
                None,
                "approval_policy",
                "Financial Modification Approval Policy",
                "2",
                "2026-11-16T08:00:00+00:00",
                5,
                "Invoice cancellations, refunds, and other financial modifications require a current explicit approval. Investigation tools are read-only.",
            ),
            (
                "RUNBOOK-021",
                None,
                "runbook",
                "Provider Timeout Decision Tree",
                "1",
                "2026-11-17T08:00:00+00:00",
                4,
                "A provider timeout requires reconciliation using the original payment identifier. A timeout alone is not proof of success or failure.",
            ),
            (
                "RUNBOOK-022",
                None,
                "runbook",
                "Merchant Configuration Verification",
                "1",
                "2026-11-17T08:00:00+00:00",
                4,
                "Invalid merchant configuration errors should be corrected through the approved change process and verified with a read-only payment attempt.",
            ),
            (
                "RUNBOOK-023",
                None,
                "runbook",
                "Payment Failure Triage",
                "1",
                "2026-11-17T08:00:00+00:00",
                4,
                "For a declined payment, preserve the provider response, distinguish a configuration error from a customer funding issue, and avoid retrying without an approved policy.",
            ),
            (
                "RUNBOOK-024",
                None,
                "runbook",
                "Duplicate Invoice Detection",
                "1",
                "2026-11-17T08:00:00+00:00",
                4,
                "Group invoices by renewal or subscription and compare payment identifiers and idempotency keys before concluding that a duplicate operation occurred.",
            ),
        ]
        for index in range(7, 21):
            if index in (8, 13, 14):
                continue
            docs.append(
                (
                    f"RUNBOOK-{index:03d}",
                    None,
                    "runbook",
                    f"Synthetic Operations Guide {index}",
                    "1",
                    f"2026-10-{(index % 20) + 1:02d}T08:00:00+00:00",
                    1,
                    f"Synthetic operational guidance for service {index}; no customer-specific financial action is authorized.",
                )
            )
        self.connection.executemany(
            """INSERT INTO documents(
                document_id, tenant_id, doc_type, title, version, updated_at,
                authority, body
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            docs,
        )

    def _seed_memories(self) -> None:
        rows: list[tuple[Any, ...]] = [
            (
                "MEM-001",
                "tenant_acme",
                "payment-retry-handler",
                "retry_policy",
                "v1 reused the original idempotency key",
                "DEP-381",
                0.99,
                "2026-11-01T09:00:00+00:00",
                None,
                "superseded",
                json.dumps(["DEP-381"]),
                5,
            ),
            (
                "MEM-002",
                "tenant_acme",
                "payment-retry-handler",
                "retry_policy",
                "v2 generated a new idempotency key after timeout",
                "DEP-382",
                0.98,
                "2026-11-14T18:00:00+00:00",
                None,
                "superseded",
                json.dumps(["DEP-382"]),
                5,
            ),
            (
                "MEM-003",
                "tenant_acme",
                "payment-retry-handler",
                "retry_policy",
                "v3 reuses the original key and reconciles timeouts",
                "DEP-383",
                0.99,
                "2026-11-16T18:00:00+00:00",
                None,
                "active",
                json.dumps(["DEP-383"]),
                5,
            ),
        ]
        for index in range(4, 101):
            rows.append(
                (
                    f"MEM-{index:03d}",
                    f"tenant_{((index - 4) % 10) + 1:03d}",
                    f"service-{index % 7}",
                    "health_observation",
                    "service healthy during synthetic observation",
                    f"LOG-{100000 + index}",
                    0.7,
                    f"2026-11-{(index % 17) + 1:02d}T08:00:00+00:00",
                    "2026-12-01T00:00:00+00:00",
                    "active",
                    json.dumps([f"LOG-{100000 + index}"]),
                    1,
                )
            )
        self.connection.executemany(
            """INSERT INTO memory_records(
                memory_id, tenant_id, subject_id, key, value, source_id,
                confidence, observed_at, expires_at, status, provenance_json,
                authority
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
