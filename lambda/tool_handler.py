"""Read-only Lambda target used by AgentCore Gateway.

The handler repeats tenant authorization inside the target. Gateway Policy is
an additional control plane, not a substitute for backend authorization.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key


dynamodb = boto3.resource("dynamodb")
s3vectors = boto3.client("s3vectors")
bedrock_runtime = boto3.client("bedrock-runtime")


def _table(name: str):
    return dynamodb.Table(os.environ[name])


def _arguments(event: dict[str, Any]) -> dict[str, Any]:
    arguments = event.get("arguments") or event.get("parameters") or event.get("input") or event
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if isinstance(event.get("body"), str):
        try:
            body = json.loads(event["body"])
            arguments = body.get("arguments", body)
        except json.JSONDecodeError:
            pass
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    headers = event.get("headers", {}) or {}
    tenant_id = arguments.get("tenant_id") or headers.get("x-tenant-id") or headers.get("X-Tenant-Id")
    if not tenant_id:
        raise PermissionError("tenant_id is required")
    result = dict(arguments)
    result["tenant_id"] = str(tenant_id)
    return result


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _customer_tenant(customer_id: str) -> str | None:
    table = _table("CUSTOMERS_TABLE") if os.environ.get("CUSTOMERS_TABLE") else None
    if table is None:
        return None
    item = table.get_item(Key={"CustomerId": customer_id}).get("Item")
    return item.get("TenantId") if item else None


def _authorize_customer(customer_id: str, tenant_id: str) -> None:
    owner = _customer_tenant(customer_id)
    if owner is not None and owner != tenant_id:
        raise PermissionError("customer is outside tenant scope")


def _business_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": [_json_safe(item) for item in items], "count": len(items), "read_only": True}


def search_incidents(arguments: dict[str, Any]) -> dict[str, Any]:
    tenant_id = arguments["tenant_id"]
    query = str(arguments.get("query", "")).lower()
    response = _table("INCIDENTS_TABLE").scan()
    items = []
    for item in response.get("Items", []):
        if item.get("TenantId") != tenant_id:
            continue
        haystack = json.dumps(item, default=str).lower()
        if query and query not in haystack:
            continue
        items.append(item)
    return _business_response(items)


def get_customer_invoices(arguments: dict[str, Any]) -> dict[str, Any]:
    customer_id = str(arguments["customer_id"])
    _authorize_customer(customer_id, arguments["tenant_id"])
    response = _table("INVOICES_TABLE").query(
        IndexName="CustomerRenewalIndex",
        KeyConditionExpression=Key("CustomerId").eq(customer_id),
    )
    return _business_response([item for item in response.get("Items", []) if item.get("TenantId") == arguments["tenant_id"]])


def get_application_logs(arguments: dict[str, Any]) -> dict[str, Any]:
    customer_id = str(arguments["customer_id"])
    _authorize_customer(customer_id, arguments["tenant_id"])
    response = _table("APPLICATION_LOGS_TABLE").query(
        IndexName="CustomerTimestampIndex",
        KeyConditionExpression=Key("CustomerId").eq(customer_id),
        ScanIndexForward=True,
        Limit=min(int(arguments.get("limit", 100)), 500),
    )
    return _business_response([item for item in response.get("Items", []) if item.get("TenantId") == arguments["tenant_id"]])


def get_deployments(arguments: dict[str, Any]) -> dict[str, Any]:
    tenant_id = arguments["tenant_id"]
    response = _table("DEPLOYMENTS_TABLE").scan()
    items = [item for item in response.get("Items", []) if item.get("TenantId") == tenant_id]
    if arguments.get("version"):
        items = [item for item in items if item.get("Version") == arguments["version"]]
    return _business_response(items)


def get_current_invoice_state(arguments: dict[str, Any]) -> dict[str, Any]:
    item = _table("INVOICES_TABLE").get_item(Key={"InvoiceId": str(arguments["invoice_id"])}).get("Item")
    if not item:
        return _business_response([])
    if item.get("TenantId") != arguments["tenant_id"]:
        raise PermissionError("invoice is outside tenant scope")
    return _business_response([item])


def _embedding(text: str) -> list[float]:
    dimensions = int(os.environ.get("VECTOR_DIMENSION", "1024"))
    response = bedrock_runtime.invoke_model(
        modelId=os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"),
        body=json.dumps({"inputText": text, "dimensions": dimensions, "normalize": True}).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())["embedding"]


def _rerank_evidence(vectors: list[dict[str, Any]], query: str, strategy: str = "hybrid") -> list[dict[str, Any]]:
    """Apply a small deterministic authority/freshness/identifier rerank.

    S3 Vectors remains responsible for BM25/vector fusion and native RRF. This
    final pass only applies application-owned business preferences after the
    tenant filter has already been enforced by the service.
    """

    query_terms = {term.lower() for term in query.split() if len(term) > 2}
    strategy = strategy if strategy in {"hybrid", "semantic", "authoritative"} else "hybrid"
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for position, original in enumerate(vectors):
        item = dict(original)
        metadata = item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {}
        scores = item.get("scores", {}) if isinstance(item.get("scores"), dict) else {}
        fused = float(scores.get("fused", 0.0) or 0.0)
        authority = float(metadata.get("authority", 0.0) or 0.0)
        freshness = 0.001 if metadata.get("status") == "active" else 0.0
        searchable = f"{metadata.get('title', '')} {metadata.get('text', '')}".lower()
        identifier_hits = sum(1 for term in query_terms if term in searchable)
        vector_score = float(scores.get("vector", 0.0) or 0.0)
        if strategy == "semantic":
            # Semantic explanations favor conceptual similarity while still
            # retaining the native hybrid result as a tie-breaker.
            rerank_score = vector_score + (fused * 0.1) + (authority * 0.0001)
        elif strategy == "authoritative":
            # Verification favors current, authoritative documents and exact
            # identifiers over a merely similar passage.
            rerank_score = fused + (authority * 0.001) + (freshness * 10) + (identifier_hits * 0.0001)
        else:
            rerank_score = fused + (authority * 0.0001) + freshness + (identifier_hits * 0.00001)
        item["application_rerank_score"] = rerank_score
        scored.append((rerank_score, -position, item))
    scored.sort(key=lambda value: (value[0], value[1]), reverse=True)
    return [item for _, _, item in scored]


def search_evidence(arguments: dict[str, Any]) -> dict[str, Any]:
    tenant_id = arguments["tenant_id"]
    query = str(arguments.get("query", ""))
    strategy = str(arguments.get("strategy", "hybrid")).strip().lower()
    if strategy not in {"hybrid", "semantic", "authoritative"}:
        strategy = "hybrid"
    top_k = min(int(arguments.get("top_k", 8)), 100)
    source_types = {
        str(value).strip().lower()
        for value in arguments.get("source_types", [])
        if str(value).strip()
    }
    vector = arguments.get("query_vector") or _embedding(query)
    query_args: dict[str, Any] = {
        "vectorBucketName": os.environ["VECTOR_BUCKET_NAME"],
        "indexName": os.environ["VECTOR_INDEX_NAME"],
        "queryVector": {"float32": [float(value) for value in vector]},
        "queryTexts": [{"metadataKey": "text", "text": query}, {"metadataKey": "title", "text": query}],
        "queryMode": "ENHANCED",
        # Fetch extra candidates when the selected policy narrows the
        # document source types after the tenant filter.
        "topK": min(max(top_k, top_k * 4 if source_types else top_k), 100),
        "returnMetadata": True,
        "returnScores": True,
    }
    # Public documents are marked with tenant_id=public at ingestion. Enhanced
    # filters are applied before search, so another tenant cannot be returned.
    query_args["filter"] = {"$or": [{"tenant_id": tenant_id}, {"tenant_id": "public"}]}
    response = s3vectors.query_vectors(**query_args)
    vectors = _rerank_evidence(response.get("vectors", []), query, strategy)
    if source_types:
        vectors = [
            item
            for item in vectors
            if str((item.get("metadata") or {}).get("source_type", "")).strip().lower() in source_types
        ]
    vectors = vectors[:top_k]
    return {
        "items": _json_safe(vectors),
        "count": len(vectors),
        "read_only": True,
        "ranking": f"{strategy}:native_rrf_then_application_rerank",
        "source_types": sorted(source_types),
    }


HANDLERS = {
    "search_incidents": search_incidents,
    "get_customer_invoices": get_customer_invoices,
    "get_application_logs": get_application_logs,
    "get_deployments": get_deployments,
    "get_current_invoice_state": get_current_invoice_state,
    "search_evidence": search_evidence,
}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    tool_name = os.environ.get("TOOL_NAME") or event.get("tool_name")
    if tool_name not in HANDLERS:
        raise ValueError(f"unregistered read-only tool: {tool_name}")
    if any(word in str(tool_name).lower() for word in ("cancel", "refund", "update", "delete", "write")):
        raise PermissionError("financial mutations are not exposed")
    arguments = _arguments(event)
    return HANDLERS[tool_name](arguments)
