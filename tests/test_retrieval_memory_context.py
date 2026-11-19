from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ledgerflow.context import ContextCompactor, PrefixCache, TokenBudgetAllocator
from ledgerflow.db import Database
from ledgerflow.memory import MemoryService
from ledgerflow.models import Evidence, InvariantSet
from ledgerflow.policies import TaskType, TaskClassifier
from ledgerflow.retrieval import HybridRetriever


@pytest.fixture()
def database(tmp_path):
    db = Database(tmp_path / "ledgerflow.db")
    db.seed(reset=True)
    yield db
    db.close()


def test_hybrid_retrieval_filters_other_tenant_and_finds_current_runbook(database):
    retriever = HybridRetriever(database)
    results = retriever.search(
        "duplicate invoice timeout retry idempotency key",
        "tenant_acme",
        filters={"doc_type": "runbook"},
        top_k=6,
    )
    ids = [result.evidence.evidence_id for result in results]
    assert "RUNBOOK-014" in ids
    assert "TENANT-OTHER-014" not in ids
    assert all(result.evidence.tenant_id in (None, "tenant_acme") for result in results)
    assert all(result.rrf_score >= 0 for result in results)


def test_memory_prefers_active_record_deduplicates_and_supersedes(database):
    memory = MemoryService(database)
    records = memory.relevant("tenant_acme", "payment retry policy", subject_id="payment-retry-handler")
    assert records and records[0].memory_id == "MEM-003"
    duplicate = memory.observe(
        tenant_id="tenant_acme",
        subject_id="payment-retry-handler",
        key="retry_policy",
        value="v3 reuses the original key and reconciles timeouts",
        source_id="DEP-383",
        confidence=0.99,
    )
    assert duplicate.memory_id == "MEM-003"
    changed = memory.observe(
        tenant_id="tenant_acme",
        subject_id="payment-retry-handler",
        key="retry_policy",
        value="v4 requires provider reconciliation before retry",
        source_id="DEP-384",
        confidence=0.98,
    )
    assert changed.status == "active"
    assert database.query_one("SELECT status FROM memory_records WHERE memory_id = 'MEM-003'")["status"] == "superseded"


def test_context_compaction_preserves_invariants_and_prefix_cache():
    cache = PrefixCache()
    compactor = ContextCompactor(prefix_cache=cache)
    evidence = [
        Evidence("LOG-8821", "application_log", "retry", "timeout generated retry key pay_9934", score=1.0),
        Evidence("DEP-382", "deployment", "deployment", "3.8.2 changed retry behavior", score=0.9),
    ]
    first = compactor.compact(
        task="Investigate the duplicate invoice incident",
        workflow_state={"current_step": "retrieval"},
        evidence=evidence,
        memories=[],
        history=[f"tool call {index} returned repeated output" for index in range(40)],
        total_budget=600,
        output_reserve=100,
        invariants=InvariantSet(),
    )
    second = compactor.compact(
        task="Investigate the duplicate invoice incident",
        workflow_state={"current_step": "retrieval"},
        evidence=evidence,
        memories=[],
        history=[],
        total_budget=600,
        output_reserve=100,
        invariants=InvariantSet(),
    )
    assert first.token_count + 100 <= 600
    assert set(first.preserved_evidence_ids) == {"LOG-8821", "DEP-382"}
    assert first.preserved_invariants.no_financial_writes
    assert second.prefix_cache_hit
    assert cache.hit_rate > 0


def test_task_classifier_detects_financial_action_before_retrieval():
    classifier = TaskClassifier()
    assert classifier.classify("Why did this invoice duplicate?").task_type == TaskType.INVESTIGATION
    action = classifier.classify("Please refund the duplicate invoice")
    assert action.task_type == TaskType.FINANCIAL_ACTION
    assert action.requires_approval
