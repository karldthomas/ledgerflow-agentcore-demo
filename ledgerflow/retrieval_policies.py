"""Deterministic retrieval policies selected from the planner task type.

The model may classify a request, but this module owns the bounded retrieval
recipe that follows.  Keeping the registry in application code makes source
scope, limits, and fallback behavior auditable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TASK_TYPES = ("investigation", "explanation", "verification")


@dataclass(frozen=True)
class RetrievalPolicy:
    """The read-only evidence recipe for one supported task type."""

    task_type: str
    policy_id: str
    strategy: str
    include_invoices: bool
    include_logs: bool
    include_deployments: bool
    include_memory: bool
    top_k: int
    log_limit: int
    vector_source_types: tuple[str, ...] = ()
    memory_query: str = "payment retry idempotency policy"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "policy_id": self.policy_id,
            "strategy": self.strategy,
            "sources": [
                source
                for source, enabled in (
                    ("invoices", self.include_invoices),
                    ("application_logs", self.include_logs),
                    ("deployments", self.include_deployments),
                    ("verified_memory", self.include_memory),
                    ("vector_evidence", True),
                )
                if enabled
            ],
            "top_k": self.top_k,
            "log_limit": self.log_limit,
            "vector_source_types": list(self.vector_source_types),
        }


POLICIES: dict[str, RetrievalPolicy] = {
    "investigation": RetrievalPolicy(
        task_type="investigation",
        policy_id="investigation-v1",
        strategy="hybrid",
        include_invoices=True,
        include_logs=True,
        include_deployments=True,
        include_memory=True,
        top_k=8,
        log_limit=500,
    ),
    "explanation": RetrievalPolicy(
        task_type="explanation",
        policy_id="explanation-v1",
        strategy="semantic",
        include_invoices=False,
        include_logs=False,
        include_deployments=True,
        include_memory=False,
        top_k=5,
        log_limit=100,
        vector_source_types=("runbook",),
        memory_query="payment retry explanation",
    ),
    "verification": RetrievalPolicy(
        task_type="verification",
        policy_id="verification-v1",
        strategy="authoritative",
        include_invoices=True,
        include_logs=True,
        include_deployments=True,
        include_memory=False,
        top_k=8,
        log_limit=250,
        vector_source_types=("runbook",),
        memory_query="payment retry verification",
    ),
}


def normalize_task_type(value: Any, *, default: str = "investigation") -> str:
    """Return a supported task type, falling back safely for unknown input."""

    candidate = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "investigate": "investigation",
        "investigation": "investigation",
        "explain": "explanation",
        "explanation": "explanation",
        "verify": "verification",
        "verification": "verification",
        "check": "verification",
        "audit": "verification",
    }
    normalized_default = aliases.get(str(default).strip().lower(), "investigation")
    return aliases.get(candidate, normalized_default if normalized_default in TASK_TYPES else "investigation")


def classify_task(request: Any, requested_task_type: Any = None) -> str:
    """Classify a request without granting it any permission or tool access.

    An explicit supported task type wins.  The keyword fallback exists for
    model-disabled or invalid-model-response runs and keeps the workflow safe
    and repeatable.
    """

    explicit = str(requested_task_type or "").strip().lower()
    if explicit and explicit not in {"auto", "automatic"}:
        return normalize_task_type(explicit)
    text = str(request or "").strip().lower()
    if any(term in text for term in ("explain", "what does", "how does", "why is", "why are")):
        return "explanation"
    if any(term in text for term in ("verify", "verification", "confirm", "validate", "check whether", "is it true")):
        return "verification"
    return "investigation"


def policy_for(task_type: Any) -> RetrievalPolicy:
    """Return the bounded policy for a task, using investigation as fallback."""

    return POLICIES[normalize_task_type(task_type)]
