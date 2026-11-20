from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_tool_handler(monkeypatch):
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    spec = importlib.util.spec_from_file_location("ledgerflow_tool_handler_test", Path("lambda/tool_handler.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Table:
    def __init__(self, *, pages=None, item=None):
        self.pages = list(pages or [])
        self.item = item
        self.calls: list[dict[str, object]] = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages.pop(0) if self.pages else {"Items": []}

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages.pop(0) if self.pages else {"Items": []}

    def get_item(self, **_kwargs):
        return {"Item": self.item} if self.item else {}


class _Resource:
    def __init__(self, tables):
        self.tables = tables

    def Table(self, name):
        return self.tables[name]


def test_tool_arguments_require_a_tenant_and_ignore_untrusted_headers(monkeypatch):
    module = _load_tool_handler(monkeypatch)

    with pytest.raises(PermissionError, match="tenant_id is required"):
        module._arguments({"arguments": {}, "headers": {"x-tenant-id": "tenant_other"}})

    with pytest.raises(PermissionError, match="authenticated tenant"):
        monkeypatch.setenv("REQUIRE_TRUSTED_TENANT_CONTEXT", "ENABLED")
        module._arguments({"arguments": {"tenant_id": "tenant_acme"}})

    assert module._arguments(
        {
            "arguments": {"tenant_id": "tenant_acme"},
            "requestContext": {"authorizer": {"tenant_id": "tenant_acme"}},
        }
    )["tenant_id"] == "tenant_acme"


def test_read_only_tools_paginate_and_filter_each_page(monkeypatch):
    module = _load_tool_handler(monkeypatch)
    monkeypatch.setenv("INCIDENTS_TABLE", "incidents")
    monkeypatch.setenv("APPLICATION_LOGS_TABLE", "logs")
    monkeypatch.setenv("CUSTOMERS_TABLE", "customers")
    incidents = _Table(
        pages=[
            {"Items": [{"IncidentId": "OTHER", "TenantId": "tenant_other"}], "LastEvaluatedKey": {"IncidentId": "OTHER"}},
            {"Items": [{"IncidentId": "INC-1", "TenantId": "tenant_acme"}]},
        ]
    )
    logs = _Table(
        pages=[
            {"Items": [{"LogId": "OTHER-LOG", "TenantId": "tenant_other"}], "LastEvaluatedKey": {"LogId": "OTHER-LOG"}},
            {"Items": [{"LogId": "LOG-1", "TenantId": "tenant_acme"}]},
        ]
    )
    customers = _Table(item={"CustomerId": "ACME-482", "TenantId": "tenant_acme"})
    module.dynamodb = _Resource({"incidents": incidents, "logs": logs, "customers": customers})

    incident_result = module.search_incidents({"tenant_id": "tenant_acme"})
    log_result = module.get_application_logs({"tenant_id": "tenant_acme", "customer_id": "ACME-482", "limit": 1})

    assert incident_result["items"] == [{"IncidentId": "INC-1", "TenantId": "tenant_acme"}]
    assert log_result["items"] == [{"LogId": "LOG-1", "TenantId": "tenant_acme"}]
    assert len(incidents.calls) == 2
    assert len(logs.calls) == 2
    assert logs.calls[1]["ExclusiveStartKey"] == {"LogId": "OTHER-LOG"}


def test_vector_results_require_allowed_tenant_metadata(monkeypatch):
    module = _load_tool_handler(monkeypatch)
    monkeypatch.setenv("VECTOR_BUCKET_NAME", "vectors")
    monkeypatch.setenv("VECTOR_INDEX_NAME", "index")
    module.s3vectors = type(
        "_Vectors",
        (),
        {
            "query_vectors": lambda _self, **_kwargs: {
                "vectors": [
                    {"key": "private", "metadata": {"tenant_id": "tenant_other", "source_type": "runbook"}},
                    {"key": "public", "metadata": {"tenant_id": "public", "source_type": "runbook"}},
                    {"key": "mine", "metadata": {"tenant_id": "tenant_acme", "source_type": "runbook"}},
                ]
            }
        },
    )()

    result = module.search_evidence(
        {"tenant_id": "tenant_acme", "query": "retry", "query_vector": [0.1], "source_types": ["runbook"]}
    )

    assert {item["key"] for item in result["items"]} == {"public", "mine"}
