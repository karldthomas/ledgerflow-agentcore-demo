"""Evidence-grounded incident investigation workflow."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from uuid import uuid4

from .context import ContextCompactor
from .db import Database
from .evidence_reasoning import build_evidence_query, build_evidence_report
from .memory import MemoryService
from .models import Evidence, IncidentReport, InvariantSet, WorkflowState
from .policies import RetrievalRouter, TaskType
from .retrieval import HybridRetriever
from .state import CheckpointStore
from .tools import ReadOnlyTools


class SimulatedCrash(RuntimeError):
    """Intentional failure used to prove checkpoint recovery."""


class InvestigationAgent:
    """Deterministic local implementation of the planned Runtime workflow."""

    def __init__(
        self,
        db: Database,
        *,
        tenant_id: str,
        tools: ReadOnlyTools | None = None,
        retriever: HybridRetriever | None = None,
        memory: MemoryService | None = None,
        checkpoints: CheckpointStore | None = None,
        compactor: ContextCompactor | None = None,
    ) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.tools = tools or ReadOnlyTools(db)
        self.retriever = retriever or HybridRetriever(db)
        self.memory = memory or MemoryService(db)
        self.checkpoints = checkpoints or CheckpointStore(db)
        self.compactor = compactor or ContextCompactor()
        self.router = RetrievalRouter()

    def _checkpointed(
        self,
        state: WorkflowState,
        step: str,
        *,
        data: dict[str, Any] | None = None,
        fail_after_step: str | None = None,
    ) -> WorkflowState:
        if step not in state.completed_steps:
            state = self.checkpoints.checkpoint(state, step=step, data=data)
        if fail_after_step == step:
            raise SimulatedCrash(f"simulated worker failure after checkpoint {step}")
        return state

    @staticmethod
    def _evidence_dict(items: list[Evidence]) -> list[dict[str, Any]]:
        return [item.to_dict() for item in items]

    @staticmethod
    def _evidence_from_dict(items: list[dict[str, Any]]) -> list[Evidence]:
        return [Evidence(**item) for item in items]

    def investigate(
        self,
        customer_id: str,
        *,
        workflow_id: str | None = None,
        fail_after_step: str | None = None,
        request: str | None = None,
    ) -> IncidentReport:
        workflow_id = workflow_id or f"wf-{uuid4().hex[:12]}"
        state = self.checkpoints.load(workflow_id)
        if state is None:
            state = self.checkpoints.create(workflow_id, customer_id)
        elif state.customer_id != customer_id:
            raise ValueError("workflow is bound to a different customer")
        if state.status == "completed" and state.data.get("report"):
            return IncidentReport(**state.data["report"])

        owner = f"agent-{uuid4().hex[:8]}"
        self.checkpoints.acquire_lease(workflow_id, owner)
        try:
            incident_items: list[Evidence]
            if "incident_lookup" in state.completed_steps:
                incident_items = self._evidence_from_dict(state.data["incident_items"])
            else:
                incident_items = self.tools.search_incidents(customer_id, self.tenant_id)
                state = self._checkpointed(
                    state,
                    "incident_lookup",
                    data={"incident_items": self._evidence_dict(incident_items)},
                    fail_after_step=fail_after_step,
                )
            if not incident_items:
                raise ValueError(f"no incident found for customer {customer_id}")
            incident = incident_items[0]

            if "invoice_lookup" in state.completed_steps:
                invoice_items = self._evidence_from_dict(state.data["invoice_items"])
            else:
                invoice_items = self.tools.get_customer_invoices(customer_id, self.tenant_id)
                state = self._checkpointed(
                    state,
                    "invoice_lookup",
                    data={"invoice_items": self._evidence_dict(invoice_items)},
                    fail_after_step=fail_after_step,
                )

            if "log_lookup" in state.completed_steps:
                log_items = self._evidence_from_dict(state.data["log_items"])
            else:
                log_items = self.tools.get_application_logs(customer_id, self.tenant_id)
                state = self._checkpointed(
                    state,
                    "log_lookup",
                    data={"log_items": self._evidence_dict(log_items)},
                    fail_after_step=fail_after_step,
                )

            deployment_version_value = incident.metadata.get("deployment_version")
            deployment_version = str(deployment_version_value) if deployment_version_value else None
            if "deployment_lookup" in state.completed_steps:
                deployment_items = self._evidence_from_dict(state.data["deployment_items"])
            else:
                deployment_items = self.tools.get_deployments(self.tenant_id, version=deployment_version)
                state = self._checkpointed(
                    state,
                    "deployment_lookup",
                    data={"deployment_items": self._evidence_dict(deployment_items)},
                    fail_after_step=fail_after_step,
                )

            task_request = request or f"Investigate the root cause of incident {incident.evidence_id} for {customer_id}"
            task, policy = self.router.route(task_request)
            if task.task_type != TaskType.INVESTIGATION:
                raise ValueError("investigation workflow received a non-investigation task")
            if "retrieval" in state.completed_steps:
                retrieved_items = self._evidence_from_dict(state.data["retrieved_items"])
            else:
                retrieval_query = f"{task_request} {build_evidence_query(incident, log_items[:5])}"
                retrieved_results = self.retriever.search(
                    retrieval_query,
                    self.tenant_id,
                    filters={"doc_type": "runbook", "min_authority": 1},
                    top_k=policy.top_k,
                    strategy=policy.strategy,
                )
                retrieved_items = [result.evidence for result in retrieved_results]
                state = self._checkpointed(
                    state,
                    "retrieval",
                    data={"retrieved_items": self._evidence_dict(retrieved_items), "retrieval_policy": asdict(policy)},
                    fail_after_step=fail_after_step,
                )

            if "memory_lookup" in state.completed_steps:
                memory_items = self.memory.relevant(self.tenant_id, "payment retry policy", subject_id="payment-retry-handler")
            else:
                memory_items = self.memory.relevant(self.tenant_id, "payment retry policy", subject_id="payment-retry-handler")
                state = self._checkpointed(
                    state,
                    "memory_lookup",
                    data={"memory_items": [item.to_dict() for item in memory_items]},
                    fail_after_step=fail_after_step,
                )

            all_evidence = incident_items + invoice_items + log_items + deployment_items + retrieved_items
            context = self.compactor.compact(
                task=task_request,
                workflow_state=state.to_dict(),
                evidence=all_evidence,
                memories=memory_items,
                history=[f"completed:{step}" for step in state.completed_steps],
                total_budget=1800,
                output_reserve=400,
                invariants=InvariantSet(),
                previous_compactions=int(state.data.get("compaction_count", 0)),
            )
            state = self._checkpointed(
                state,
                "context_compaction",
                data={"context": context.to_dict(), "compaction_count": context.compaction_count},
                fail_after_step=fail_after_step,
            )

            report = self._build_report(
                incident_items=incident_items,
                invoice_items=invoice_items,
                log_items=log_items,
                deployment_items=deployment_items,
                retrieved_items=retrieved_items,
                memory_items=memory_items,
                state=state,
                task_type=task.task_type.value,
            )
            operation_id = f"report:{workflow_id}"
            operation = self.checkpoints.begin_operation(operation_id, workflow_id, "publish_investigation_report")
            if operation.status == "succeeded" and operation.result:
                report = IncidentReport(**operation.result)
            else:
                self.checkpoints.complete_operation(operation_id, report.to_dict())
            state = self.checkpoints.checkpoint(
                state,
                step="report_complete",
                data={"report": report.to_dict()},
                status="completed",
            )
            self.db.log_event("investigation_completed", {"customer_id": customer_id, "evidence_ids": report.evidence_ids}, workflow_id)
            return report
        finally:
            self.checkpoints.release_lease(workflow_id, owner)

    def _build_report(
        self,
        *,
        incident_items: list[Evidence],
        invoice_items: list[Evidence],
        log_items: list[Evidence],
        deployment_items: list[Evidence],
        retrieved_items: list[Evidence],
        memory_items: list[Any],
        state: WorkflowState,
        task_type: str,
    ) -> IncidentReport:
        return build_evidence_report(
            incident=incident_items[0],
            invoices=invoice_items,
            logs=log_items,
            deployments=deployment_items,
            vectors=retrieved_items,
            memories=[item.to_dict() if hasattr(item, "to_dict") else item for item in memory_items],
            tenant_id=self.tenant_id,
            task_type=task_type,
            recovery_history=state.recovery_history,
        )
