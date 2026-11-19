"""Serializable report contract shared by the AWS Runtime and console."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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
