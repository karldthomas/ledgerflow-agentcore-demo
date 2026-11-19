"""Task classification and retrieval-policy routing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import RetrievalPolicy


class TaskType(StrEnum):
    EXPLANATION = "explanation"
    INVESTIGATION = "investigation"
    FINANCIAL_ACTION = "financial_action"


@dataclass(frozen=True)
class ClassifiedTask:
    task_type: TaskType
    reason: str
    requires_approval: bool


class TaskClassifier:
    """Classify intent conservatively before retrieval or tool selection."""

    FINANCIAL_WORDS = ("refund", "cancel", "credit", "reissue", "charge", "modify", "write off", "change invoice")
    INVESTIGATION_WORDS = ("investigate", "incident", "root cause", "why", "duplicate", "failure", "timeline", "what happened")

    def classify(self, request: str) -> ClassifiedTask:
        normalized = request.lower()
        if any(term in normalized for term in self.FINANCIAL_WORDS):
            return ClassifiedTask(TaskType.FINANCIAL_ACTION, "request contains a financial mutation signal", True)
        if any(term in normalized for term in self.INVESTIGATION_WORDS):
            return ClassifiedTask(TaskType.INVESTIGATION, "request asks for causal or incident analysis", False)
        return ClassifiedTask(TaskType.EXPLANATION, "request is informational without an explicit mutation", False)


class RetrievalPolicyRegistry:
    """Allow-listed task policies owned by application code, not the model."""

    def __init__(self) -> None:
        self._policies = {
            TaskType.EXPLANATION: RetrievalPolicy(
                name="explanation",
                sources=("runbook", "approval_policy"),
                strategy="hybrid",
                top_k=4,
                freshness="normal",
            ),
            TaskType.INVESTIGATION: RetrievalPolicy(
                name="investigation",
                sources=("runbook", "incident", "invoice", "application_log", "deployment", "memory"),
                strategy="hybrid",
                top_k=6,
                freshness="recent_and_authoritative",
            ),
            TaskType.FINANCIAL_ACTION: RetrievalPolicy(
                name="financial_action",
                sources=("approval_policy", "runbook"),
                strategy="hybrid",
                top_k=5,
                freshness="current_only",
                require_approval=True,
            ),
        }

    def get(self, task_type: TaskType | str) -> RetrievalPolicy:
        key = TaskType(task_type)
        return self._policies[key]


class RetrievalRouter:
    def __init__(self, classifier: TaskClassifier | None = None, registry: RetrievalPolicyRegistry | None = None) -> None:
        self.classifier = classifier or TaskClassifier()
        self.registry = registry or RetrievalPolicyRegistry()

    def route(self, request: str) -> tuple[ClassifiedTask, RetrievalPolicy]:
        task = self.classifier.classify(request)
        return task, self.registry.get(task.task_type)
