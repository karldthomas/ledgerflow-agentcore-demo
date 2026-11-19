"""Small, serializable domain models used throughout the POC.

The production case study uses Pydantic. The first local slice uses standard
library dataclasses instead so it can run in a clean Python installation while
still validating the important boundaries explicitly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source_type: str
    title: str
    content: str
    tenant_id: str | None = None
    observed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IncidentReport:
    """Evidence-grounded report returned by an investigation."""

    incident_id: str
    customer_id: str
    root_cause: str | None
    evidence_ids: list[str]
    confidence: float
    recommended_actions: list[str]
    unresolved_questions: list[str]
    executive_summary: str = ""
    affected_scope: list[str] = field(default_factory=list)
    incident_timeline: list[str] = field(default_factory=list)
    verified_findings: list[str] = field(default_factory=list)
    recovery_history: list[str] = field(default_factory=list)
    remaining_risks: list[str] = field(default_factory=list)
    task_type: str = "investigation"
    evidence_schema_version: str = "ledgerflow.evidence.v2"
    evidence_claims: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if not self.incident_id:
            raise ValueError("incident_id is required")
        if not self.customer_id:
            raise ValueError("customer_id is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryRecord:
    memory_id: str
    tenant_id: str
    subject_id: str
    key: str
    value: str
    source_id: str
    confidence: float
    observed_at: str
    expires_at: str | None = None
    status: str = "active"
    provenance: list[str] = field(default_factory=list)
    authority: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowState:
    workflow_id: str
    customer_id: str
    status: str = "running"
    current_step: str = "created"
    completed_steps: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    checkpoint_version: int = 0
    schema_version: int = 1
    recovery_history: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetrievalPolicy:
    name: str
    sources: tuple[str, ...]
    strategy: str
    top_k: int
    freshness: str = "normal"
    require_approval: bool = False


@dataclass(frozen=True)
class InvariantSet:
    no_financial_writes: bool = True
    require_evidence_for_findings: bool = True
    require_approval_for_remediation: bool = True

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


def parse_iso(value: str | None) -> datetime | None:
    """Parse the ISO timestamps used by the synthetic dataset."""

    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
