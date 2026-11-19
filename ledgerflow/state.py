"""Durable workflow checkpoints, leases, and idempotency records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database, utc_now
from .models import WorkflowState


class CheckpointConflict(RuntimeError):
    """Raised when an older worker attempts to overwrite a newer checkpoint."""


class LeaseUnavailable(RuntimeError):
    """Raised when another worker owns a live workflow lease."""


@dataclass(frozen=True)
class OperationStatus:
    operation_id: str
    status: str
    result: dict[str, Any] | None = None


class CheckpointStore:
    def __init__(self, db: Database, lease_seconds: int = 120) -> None:
        self.db = db
        self.lease_seconds = lease_seconds

    @staticmethod
    def _state_from_row(row: Any) -> WorkflowState:
        payload = json.loads(row["state_json"])
        return WorkflowState(
            workflow_id=payload["workflow_id"],
            customer_id=payload["customer_id"],
            status=payload.get("status", row["status"]),
            current_step=payload.get("current_step", "created"),
            completed_steps=payload.get("completed_steps", []),
            data=payload.get("data", {}),
            checkpoint_version=int(row["checkpoint_version"]),
            schema_version=int(row["schema_version"]),
            recovery_history=payload.get("recovery_history", []),
        )

    def load(self, workflow_id: str) -> WorkflowState | None:
        row = self.db.query_one("SELECT * FROM checkpoints WHERE workflow_id = ?", (workflow_id,))
        return self._state_from_row(row) if row else None

    def create(self, workflow_id: str, customer_id: str) -> WorkflowState:
        existing = self.load(workflow_id)
        if existing:
            return existing
        state = WorkflowState(workflow_id=workflow_id, customer_id=customer_id)
        self._write(state, expected_version=-1)
        return state

    def _write(self, state: WorkflowState, expected_version: int | None = None) -> WorkflowState:
        current = self.db.query_one("SELECT checkpoint_version FROM checkpoints WHERE workflow_id = ?", (state.workflow_id,))
        if expected_version is not None:
            actual = int(current["checkpoint_version"]) if current else -1
            if actual != expected_version:
                raise CheckpointConflict(
                    f"workflow {state.workflow_id} expected version {expected_version}, current version {actual}"
                )
        next_version = (int(current["checkpoint_version"]) + 1) if current else 0
        payload = state.to_dict()
        payload["checkpoint_version"] = next_version
        state.checkpoint_version = next_version
        self.db.execute(
            """INSERT INTO checkpoints(
                workflow_id, customer_id, status, state_json, checkpoint_version,
                schema_version, lease_owner, lease_expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            ON CONFLICT(workflow_id) DO UPDATE SET
                customer_id = excluded.customer_id,
                status = excluded.status,
                state_json = excluded.state_json,
                checkpoint_version = excluded.checkpoint_version,
                schema_version = excluded.schema_version,
                updated_at = excluded.updated_at""",
            (
                state.workflow_id,
                state.customer_id,
                state.status,
                json.dumps(payload, sort_keys=True),
                next_version,
                state.schema_version,
                utc_now().isoformat(),
            ),
        )
        self.db.connection.commit()
        self.db.log_event("checkpoint_saved", {"workflow_id": state.workflow_id, "step": state.current_step, "version": next_version}, state.workflow_id)
        return state

    def checkpoint(
        self,
        state: WorkflowState,
        *,
        step: str,
        data: dict[str, Any] | None = None,
        status: str | None = None,
        recovery_note: str | None = None,
    ) -> WorkflowState:
        state.current_step = step
        if step not in state.completed_steps:
            state.completed_steps.append(step)
        if data:
            state.data.update(data)
        if status:
            state.status = status
        if recovery_note:
            state.recovery_history.append(recovery_note)
        return self._write(state, expected_version=state.checkpoint_version)

    def acquire_lease(self, workflow_id: str, owner: str, *, now: datetime | None = None) -> bool:
        effective_now = now or utc_now()
        row = self.db.query_one("SELECT lease_owner, lease_expires_at FROM checkpoints WHERE workflow_id = ?", (workflow_id,))
        if row is None:
            raise KeyError(workflow_id)
        current_expiry = row["lease_expires_at"]
        if row["lease_owner"] and row["lease_owner"] != owner and current_expiry:
            expiry = datetime.fromisoformat(current_expiry)
            if expiry > effective_now:
                raise LeaseUnavailable(f"workflow {workflow_id} is leased by {row['lease_owner']}")
        expiry = effective_now + timedelta(seconds=self.lease_seconds)
        self.db.execute(
            "UPDATE checkpoints SET lease_owner = ?, lease_expires_at = ? WHERE workflow_id = ?",
            (owner, expiry.isoformat(), workflow_id),
        )
        self.db.connection.commit()
        return True

    def release_lease(self, workflow_id: str, owner: str) -> None:
        self.db.execute(
            "UPDATE checkpoints SET lease_owner = NULL, lease_expires_at = NULL WHERE workflow_id = ? AND lease_owner = ?",
            (workflow_id, owner),
        )
        self.db.connection.commit()

    def begin_operation(self, operation_id: str, workflow_id: str, operation_type: str) -> OperationStatus:
        existing = self.db.query_one("SELECT * FROM operation_ledger WHERE operation_id = ?", (operation_id,))
        if existing:
            result = json.loads(existing["result_json"]) if existing["result_json"] else None
            return OperationStatus(operation_id, existing["status"], result)
        self.db.execute(
            "INSERT INTO operation_ledger(operation_id, workflow_id, operation_type, status, result_json, created_at) VALUES (?, ?, ?, 'started', NULL, ?)",
            (operation_id, workflow_id, operation_type, utc_now().isoformat()),
        )
        self.db.connection.commit()
        self.db.log_event("operation_started", {"operation_id": operation_id, "operation_type": operation_type}, workflow_id)
        return OperationStatus(operation_id, "started")

    def complete_operation(self, operation_id: str, result: dict[str, Any]) -> OperationStatus:
        self.db.execute(
            "UPDATE operation_ledger SET status = 'succeeded', result_json = ? WHERE operation_id = ?",
            (json.dumps(result, sort_keys=True), operation_id),
        )
        self.db.connection.commit()
        row = self.db.query_one("SELECT * FROM operation_ledger WHERE operation_id = ?", (operation_id,))
        if row is None:
            raise KeyError(operation_id)
        return OperationStatus(operation_id, row["status"], json.loads(row["result_json"]))

    def mark_operation_uncertain(self, operation_id: str) -> None:
        self.db.execute("UPDATE operation_ledger SET status = 'uncertain' WHERE operation_id = ?", (operation_id,))
        self.db.connection.commit()
