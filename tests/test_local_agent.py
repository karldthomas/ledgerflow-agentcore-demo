from __future__ import annotations

import json

import pytest

from ledgerflow.agent import InvestigationAgent, SimulatedCrash
from ledgerflow.db import Database
from ledgerflow.tools import AuthorizationError, ReadOnlyTools, ReadOnlyViolation


@pytest.fixture()
def database(tmp_path):
    db = Database(tmp_path / "ledgerflow.db")
    db.seed(reset=True)
    yield db
    db.close()


def test_acme_investigation_is_evidence_grounded_and_read_only(database):
    invoices_before = database.query_one("SELECT COUNT(*) AS n FROM invoices")["n"]
    report = InvestigationAgent(database, tenant_id="tenant_acme").investigate("ACME-482", workflow_id="wf-acme")

    assert report.incident_id == "INC-1042"
    assert report.confidence > 0.9
    assert "idempotency key" in report.root_cause
    assert {"INV-8821", "INV-8822", "LOG-8821", "DEP-382", "RUNBOOK-014"}.issubset(report.evidence_ids)
    assert database.query_one("SELECT COUNT(*) AS n FROM invoices")["n"] == invoices_before

    tools = ReadOnlyTools(database)
    with pytest.raises(ReadOnlyViolation):
        tools.invoke("cancel_invoice", invoice_id="INV-8821", tenant_id="tenant_acme")
    with pytest.raises(AuthorizationError):
        tools.get_customer_invoices("ACME-482", "tenant_beta")


def test_checkpoint_recovery_resumes_after_simulated_crash(database):
    agent = InvestigationAgent(database, tenant_id="tenant_acme")
    with pytest.raises(SimulatedCrash):
        agent.investigate("ACME-482", workflow_id="wf-recovery", fail_after_step="log_lookup")

    state = agent.checkpoints.load("wf-recovery")
    assert state is not None
    assert "log_lookup" in state.completed_steps
    assert state.status == "running"

    report = agent.investigate("ACME-482", workflow_id="wf-recovery")
    assert report.incident_id == "INC-1042"
    assert report.root_cause and "3.8.2" in report.root_cause
    assert agent.checkpoints.load("wf-recovery").status == "completed"


def test_report_is_idempotent_on_repeated_workflow_call(database):
    agent = InvestigationAgent(database, tenant_id="tenant_acme")
    first = agent.investigate("ACME-482", workflow_id="wf-idempotent")
    second = agent.investigate("ACME-482", workflow_id="wf-idempotent")
    assert first.to_dict() == second.to_dict()
    assert database.query_one("SELECT COUNT(*) AS n FROM operation_ledger WHERE operation_id = ?", ("report:wf-idempotent",))["n"] == 1


def test_schema_driven_reasoning_handles_additional_synthetic_cases(database):
    agent = InvestigationAgent(database, tenant_id="tenant_acme")

    duplicate = agent.investigate("ACME-487", workflow_id="wf-schema-duplicate")
    assert duplicate.incident_id == "INC-1050"
    assert "REN-2026-12" in duplicate.root_cause
    assert any(claim["type"] == "hypothesis" for claim in duplicate.evidence_claims)
    assert {"INV-8871", "INV-8872", "LOG-ACME-487-02"}.issubset(duplicate.evidence_ids)
    assert "DEP-ACME-487" in duplicate.evidence_ids
    assert not {"DEP-ACME-488", "DEP-ACME-489", "DEP-ACME-493", "DEP-ACME-495"}.intersection(duplicate.evidence_ids)

    reconciled = agent.investigate("ACME-488", workflow_id="wf-schema-reconciled")
    assert "safe retry handling" in reconciled.root_cause
    assert not any("duplicate operation" in question.lower() for question in reconciled.unresolved_questions)
