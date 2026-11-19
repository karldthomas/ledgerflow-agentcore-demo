"""Cognito-protected LedgerFlow web console and investigation API."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key


REGION = os.environ.get("AWS_REGION", "us-east-1")
ENVIRONMENT_NAME = os.environ.get("ENVIRONMENT_NAME", "dev")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
USER_POOL_CLIENT_ID = os.environ.get("USER_POOL_CLIENT_ID", "")
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")
EVENTS_TABLE_NAME = os.environ.get("WORKFLOW_EVENTS_TABLE", "")
CHECKPOINTS_TABLE_NAME = os.environ.get("WORKFLOW_CHECKPOINTS_TABLE", "")
REPORTS_BUCKET = os.environ.get("REPORTS_BUCKET", "")
PROMPT_SELECTIONS_TABLE_NAME = os.environ.get("PROMPT_SELECTIONS_TABLE", "")
KMS_KEY_ARN = os.environ.get("KMS_KEY_ARN", "")
PLANNER_PROMPT_ARN = os.environ.get("PLANNER_PROMPT_ARN", "")
SYNTHESIS_PROMPT_ARN = os.environ.get("SYNTHESIS_PROMPT_ARN", "")
PLANNER_PROMPT_DEFAULT_ARN = os.environ.get("PLANNER_PROMPT_DEFAULT_ARN", "")
SYNTHESIS_PROMPT_DEFAULT_ARN = os.environ.get("SYNTHESIS_PROMPT_DEFAULT_ARN", "")
CONTEXT_COMPACTION_PROMPT_ARN = os.environ.get("CONTEXT_COMPACTION_PROMPT_ARN", "")
CONTEXT_COMPACTION_PROMPT_DEFAULT_ARN = os.environ.get("CONTEXT_COMPACTION_PROMPT_DEFAULT_ARN", "")
PROMPT_NAME_PREFIX = f"ledgerflow-{ENVIRONMENT_NAME}-"
DEFAULT_PROMPT_MODEL_ID = "amazon.nova-micro-v1:0"
DEV_TOOLS_CONTROLS_TABLE_NAME = os.environ.get("DEV_TOOLS_CONTROLS_TABLE", "")
DEV_TOOLS_ENVIRONMENTS = {"dev", "test"}
DEV_TOOL_STEPS = (
    "started",
    "planning",
    "incident_lookup",
    "invoice_lookup",
    "log_lookup",
    "deployment_lookup",
    "retrieval",
    "memory_lookup",
    "context_compaction",
    "report_synthesis",
)
WORKFLOW_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
stepfunctions = boto3.client("stepfunctions", region_name=REGION)
cognito = boto3.client("cognito-idp", region_name=REGION)
bedrock_agent = boto3.client("bedrock-agent", region_name=REGION)

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"


def _response(status: int, body: Any, content_type: str = "application/json") -> dict[str, Any]:
    if content_type == "application/json":
        encoded = json.dumps(body, default=str)
    else:
        encoded = str(body)
    return {
        "statusCode": status,
        "headers": {"Content-Type": content_type, "Cache-Control": "no-store"},
        "body": encoded,
    }


def _claims(event: dict[str, Any]) -> dict[str, Any]:
    return ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims", {}) or {}


def _groups(claims: dict[str, Any]) -> set[str]:
    value = claims.get("cognito:groups", claims.get("groups", []))
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return {str(item) for item in parsed}
        except json.JSONDecodeError:
            normalized = value.strip().strip("[]")
            return {
                item.strip(" '\"")
                for item in re.split(r"[,\s]+", normalized)
                if item.strip(" '\"")
            }
    return {str(item) for item in value} if isinstance(value, list) else set()


def _is_admin(claims: dict[str, Any]) -> bool:
    return "admins" in _groups(claims)


def _tenant(claims: dict[str, Any], requested: str | None = None) -> str:
    if _is_admin(claims) and requested:
        return str(requested)
    return str(claims.get("custom:tenant_id") or claims.get("tenant_id") or "tenant_acme")


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _event_table():
    return dynamodb.Table(EVENTS_TABLE_NAME)


def _checkpoint_table():
    return dynamodb.Table(CHECKPOINTS_TABLE_NAME)


def _prompt_selection_table():
    if not PROMPT_SELECTIONS_TABLE_NAME:
        raise ValueError("prompt selection storage is not configured")
    return dynamodb.Table(PROMPT_SELECTIONS_TABLE_NAME)


def _dev_tools_table():
    if not DEV_TOOLS_CONTROLS_TABLE_NAME:
        raise ValueError("Dev Tools storage is not configured")
    return dynamodb.Table(DEV_TOOLS_CONTROLS_TABLE_NAME)


def _require_dev_tools_access(claims: dict[str, Any]) -> None:
    if not _is_admin(claims):
        raise PermissionError("only admins can use Dev Tools")
    if ENVIRONMENT_NAME.strip().lower() not in DEV_TOOLS_ENVIRONMENTS:
        raise PermissionError("Dev Tools failure injection is only available in dev or test environments")


def _dev_tool_status(item: dict[str, Any], now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    if not item.get("Enabled", False):
        return "DISARMED"
    if item.get("Consumed", False):
        return "CONSUMED"
    try:
        if int(item.get("ExpiresAtEpoch", 0)) <= now:
            return "EXPIRED"
    except (TypeError, ValueError):
        return "EXPIRED"
    return "ACTIVE"


def _dev_tool_view(item: dict[str, Any], now: int | None = None) -> dict[str, Any]:
    now = int(time.time()) if now is None else now
    return {
        "control_id": str(item.get("ControlId") or ""),
        "environment": str(item.get("Environment") or ENVIRONMENT_NAME),
        "workflow_id": str(item.get("WorkflowId") or ""),
        "step": str(item.get("Step") or ""),
        "mode": str(item.get("Mode") or "fail_once"),
        "status": _dev_tool_status(item, now),
        "enabled": bool(item.get("Enabled", False)),
        "consumed": bool(item.get("Consumed", False)),
        "created_by": str(item.get("CreatedBy") or ""),
        "created_at": str(item.get("CreatedAt") or ""),
        "updated_at": str(item.get("UpdatedAt") or ""),
        "expires_at_epoch": int(item.get("ExpiresAtEpoch", 0) or 0),
        "consumed_at": str(item.get("ConsumedAt") or "") or None,
        "consumed_by": str(item.get("ConsumedBy") or "") or None,
        "disabled_at": str(item.get("DisabledAt") or "") or None,
        "disabled_by": str(item.get("DisabledBy") or "") or None,
    }


def _list_dev_tools(claims: dict[str, Any]) -> dict[str, Any]:
    _require_dev_tools_access(claims)
    now = int(time.time())
    response = _dev_tools_table().scan(Limit=100)
    controls = [
        _dev_tool_view(item, now)
        for item in response.get("Items", [])
        if isinstance(item, dict) and str(item.get("Environment") or ENVIRONMENT_NAME) == ENVIRONMENT_NAME
    ]
    controls.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return {
        "environment": ENVIRONMENT_NAME,
        "enabled": ENVIRONMENT_NAME.strip().lower() in DEV_TOOLS_ENVIRONMENTS,
        "steps": list(DEV_TOOL_STEPS),
        "controls": controls,
    }


def _arm_failure_plan(event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    _require_dev_tools_access(claims)
    payload = _body(event)
    workflow_id = str(payload.get("workflow_id") or "").strip()
    step = str(payload.get("step") or "").strip()
    if not WORKFLOW_ID_PATTERN.fullmatch(workflow_id):
        raise ValueError("workflow_id must be 1-128 characters using letters, numbers, ., _, :, or -")
    if step not in DEV_TOOL_STEPS:
        raise ValueError(f"step must be one of: {', '.join(DEV_TOOL_STEPS)}")
    try:
        ttl_minutes = int(payload.get("ttl_minutes", 15))
    except (TypeError, ValueError) as exc:
        raise ValueError("ttl_minutes must be an integer between 1 and 120") from exc
    if ttl_minutes < 1 or ttl_minutes > 120:
        raise ValueError("ttl_minutes must be an integer between 1 and 120")

    now_epoch = int(time.time())
    existing = _dev_tools_table().query(
        IndexName="WorkflowStepIndex",
        KeyConditionExpression=Key("WorkflowId").eq(workflow_id) & Key("Step").eq(step),
        Limit=50,
    ).get("Items", [])
    for item in existing:
        if isinstance(item, dict) and _dev_tool_status(item, now_epoch) == "ACTIVE":
            raise ValueError("an active failure plan already exists for this workflow and step")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    control_id = f"failure-{uuid4().hex}"
    item = {
        "ControlId": control_id,
        "Environment": ENVIRONMENT_NAME,
        "WorkflowId": workflow_id,
        "Step": step,
        "Mode": "fail_once",
        "Enabled": True,
        "Consumed": False,
        "CreatedBy": str(claims.get("email") or claims.get("username") or claims.get("sub") or "admin"),
        "CreatedAt": now,
        "UpdatedAt": now,
        "ExpiresAtEpoch": now_epoch + (ttl_minutes * 60),
    }
    _dev_tools_table().put_item(Item=item, ConditionExpression="attribute_not_exists(ControlId)")
    return _dev_tool_view(item, now_epoch)


def _disable_failure_plan(control_id: str, claims: dict[str, Any]) -> dict[str, Any]:
    _require_dev_tools_access(claims)
    normalized_id = str(control_id or "").strip()
    if not normalized_id:
        raise ValueError("control_id is required")
    table = _dev_tools_table()
    current = table.get_item(Key={"ControlId": normalized_id}).get("Item")
    if not current or str(current.get("Environment") or ENVIRONMENT_NAME) != ENVIRONMENT_NAME:
        raise ValueError("failure plan does not exist")
    if _dev_tool_status(current) == "CONSUMED":
        return _dev_tool_view(current)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    updated = table.update_item(
        Key={"ControlId": normalized_id},
        UpdateExpression="SET #enabled = :false, DisabledAt = :now, DisabledBy = :actor, UpdatedAt = :now",
        ExpressionAttributeNames={"#enabled": "Enabled"},
        ExpressionAttributeValues={
            ":false": False,
            ":now": now,
            ":actor": str(claims.get("email") or claims.get("username") or claims.get("sub") or "admin"),
        },
        ConditionExpression="attribute_exists(ControlId)",
        ReturnValues="ALL_NEW",
    )
    return _dev_tool_view(updated.get("Attributes", {}))


def _put_event(workflow_id: str, tenant_id: str, event_type: str, step: str, status: str, payload: dict[str, Any]) -> None:
    _event_table().put_item(
        Item={
            "WorkflowId": workflow_id,
            "EventId": f"{time.time_ns()}#{uuid4().hex}",
            "TenantId": tenant_id,
            "EventType": event_type,
            "Step": step,
            "Status": status,
            "Visibility": "operator",
            "Payload": json.dumps(payload, sort_keys=True, default=str)[:24000],
            # DynamoDB's DocumentClient rejects Python floats. Keep this as an
            # epoch-second integer so the browser can render it directly.
            "CreatedAt": int(time.time()),
            "ExpiresAtEpoch": int(time.time()) + (30 * 24 * 60 * 60),
        }
    )


def _query_events(workflow_id: str, tenant_id: str, claims: dict[str, Any]) -> list[dict[str, Any]]:
    result = _event_table().query(KeyConditionExpression=Key("WorkflowId").eq(workflow_id), Limit=250, ScanIndexForward=True)
    events: list[dict[str, Any]] = []
    for item in result.get("Items", []):
        if item.get("TenantId") != tenant_id:
            continue
        if item.get("Visibility") == "admin" and not _is_admin(claims):
            continue
        payload = item.get("Payload", "{}")
        try:
            payload = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            payload = {"value": str(payload)}
        events.append({
            "event_id": item.get("EventId"),
            "event_type": item.get("EventType"),
            "step": item.get("Step"),
            "status": item.get("Status"),
            "visibility": item.get("Visibility", "operator"),
            "created_at": item.get("CreatedAt"),
            "payload": payload,
        })
    return events


def _load_report(workflow_id: str) -> dict[str, Any] | None:
    if not REPORTS_BUCKET:
        return None
    try:
        response = boto3.client("s3", region_name=REGION).get_object(
            Bucket=REPORTS_BUCKET,
            Key=f"investigations/{workflow_id}.json",
        )
        return json.loads(response["Body"].read().decode("utf-8"))
    except Exception as exc:
        if "NoSuchKey" in str(exc) or "404" in str(exc):
            return None
        raise


def _get_investigation(workflow_id: str, claims: dict[str, Any]) -> dict[str, Any]:
    checkpoint = _checkpoint_table().get_item(Key={"WorkflowId": workflow_id}).get("Item")
    if not checkpoint:
        return {"workflow_id": workflow_id, "status": "unknown", "events": []}
    tenant_id = _tenant(claims, checkpoint.get("TenantId"))
    if not _is_admin(claims) and checkpoint.get("TenantId") != tenant_id:
        raise PermissionError("workflow is outside tenant scope")
    events = _query_events(workflow_id, tenant_id, claims)
    data = checkpoint.get("Data", "{}")
    try:
        data = json.loads(data)
    except (TypeError, json.JSONDecodeError):
        data = {"value": str(data)}
    return {
        "workflow_id": workflow_id,
        "tenant_id": tenant_id,
        "customer_id": checkpoint.get("CustomerId"),
        "step": checkpoint.get("CurrentStep"),
        "updated_at": checkpoint.get("UpdatedAt"),
        "data": data,
        "events": events,
        "report": _load_report(workflow_id) if checkpoint.get("CurrentStep") == "completed" else None,
    }


def _create_investigation(event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    payload = _body(event)
    customer_id = str(payload.get("customer_id", "")).strip()
    if not customer_id:
        raise ValueError("customer_id is required")
    requested_workflow_id = str(payload.get("workflow_id") or "").strip()
    if requested_workflow_id:
        if not _is_admin(claims):
            raise PermissionError("only admins can choose a workflow_id")
        if not WORKFLOW_ID_PATTERN.fullmatch(requested_workflow_id):
            raise ValueError("workflow_id must be 1-128 characters using letters, numbers, ., _, :, or -")
    workflow_id = requested_workflow_id or f"web-{uuid4().hex}"
    tenant_id = _tenant(claims, payload.get("tenant_id"))
    request = str(payload.get("request") or "Investigate the customer incident.")
    input_payload = {
        "customer_id": customer_id,
        "tenant_id": tenant_id,
        "incident_id": str(payload.get("incident_id") or ""),
        "workflow_id": workflow_id,
        "request": request,
    }
    started = stepfunctions.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=workflow_id,
        input=json.dumps(input_payload),
    )
    _put_event(
        workflow_id,
        tenant_id,
        "workflow",
        "investigation",
        "queued",
        {"customer_id": customer_id, "request": request, "execution_arn": started["executionArn"]},
    )
    return {"workflow_id": workflow_id, "tenant_id": tenant_id, "execution_arn": started["executionArn"], "status": "queued"}


def _create_user(event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(claims):
        raise PermissionError("only admins can create users")
    payload = _body(event)
    email = str(payload.get("email", "")).strip().lower()
    tenant_id = str(payload.get("tenant_id", "tenant_acme")).strip()
    group = str(payload.get("group", "operators")).strip().lower()
    if not email or "@" not in email:
        raise ValueError("a valid email is required")
    if group not in {"operators", "admins"}:
        raise ValueError("group must be operators or admins")
    created = cognito.admin_create_user(
        UserPoolId=USER_POOL_ID,
        Username=email,
        UserAttributes=[{"Name": "email", "Value": email}, {"Name": "custom:tenant_id", "Value": tenant_id}],
        DesiredDeliveryMediums=["EMAIL"],
    )
    cognito.admin_add_user_to_group(UserPoolId=USER_POOL_ID, Username=email, GroupName=group)
    return {"username": created["User"]["Username"], "email": email, "tenant_id": tenant_id, "group": group, "status": "INVITED"}


def _list_users(claims: dict[str, Any]) -> list[dict[str, Any]]:
    if not _is_admin(claims):
        raise PermissionError("only admins can list users")
    users = cognito.list_users(UserPoolId=USER_POOL_ID, Limit=60).get("Users", [])
    result = []
    for user in users:
        attributes = {item["Name"]: item.get("Value", "") for item in user.get("Attributes", [])}
        groups = cognito.admin_list_groups_for_user(UserPoolId=USER_POOL_ID, Username=user["Username"]).get("Groups", [])
        result.append({
            "username": user["Username"],
            "email": attributes.get("email", ""),
            "tenant_id": attributes.get("custom:tenant_id", ""),
            "status": user.get("UserStatus"),
            "groups": [group.get("GroupName") for group in groups],
        })
    return result


def _runtime_prompt_definitions() -> dict[str, dict[str, Any]]:
    return {
        "planning": {
            "label": "Investigation planner",
            "name": f"{PROMPT_NAME_PREFIX}planner",
            "arn": PLANNER_PROMPT_ARN,
            "default_arn": PLANNER_PROMPT_DEFAULT_ARN,
            "runtime": True,
        },
        "synthesis": {
            "label": "Report synthesis",
            "name": f"{PROMPT_NAME_PREFIX}synthesis",
            "arn": SYNTHESIS_PROMPT_ARN,
            "default_arn": SYNTHESIS_PROMPT_DEFAULT_ARN,
            "runtime": True,
        },
        "context_compaction": {
            "label": "Context compaction",
            "name": f"{PROMPT_NAME_PREFIX}context-compaction",
            "arn": CONTEXT_COMPACTION_PROMPT_ARN,
            "default_arn": CONTEXT_COMPACTION_PROMPT_DEFAULT_ARN,
            "runtime": True,
        },
    }


def _list_prompt_summaries() -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        request: dict[str, Any] = {"maxResults": 100}
        if next_token:
            request["nextToken"] = next_token
        response = bedrock_agent.list_prompts(**request)
        for summary in response.get("promptSummaries", []):
            version = str(summary.get("version") or "DRAFT")
            if version.upper() == "DRAFT":
                summaries.append(summary)
        next_token = response.get("nextToken")
        if not next_token:
            break
    return summaries


def _prompt_definitions() -> dict[str, dict[str, Any]]:
    definitions = _runtime_prompt_definitions()
    runtime_arns = {str(definition.get("arn")) for definition in definitions.values() if definition.get("arn")}
    for summary in _list_prompt_summaries():
        name = str(summary.get("name") or "")
        arn = str(summary.get("arn") or summary.get("promptArn") or "")
        prompt_id = str(summary.get("id") or _prompt_version_from_arn(arn))
        if not name.startswith(PROMPT_NAME_PREFIX) or not arn or arn in runtime_arns or not prompt_id:
            continue
        definitions[f"library_{prompt_id}"] = {
            "label": name,
            "name": name,
            "description": str(summary.get("description") or ""),
            "arn": arn,
            "default_arn": "",
            "runtime": False,
        }
    return definitions


def _prompt_version_from_arn(arn: str) -> str:
    return str(arn).rsplit(":", 1)[-1] if arn else ""


def _prompt_versions(prompt_arn: str) -> list[dict[str, Any]]:
    if not prompt_arn:
        raise ValueError("prompt management is not configured")
    versions: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        request: dict[str, Any] = {"promptIdentifier": prompt_arn, "maxResults": 100}
        if next_token:
            request["nextToken"] = next_token
        response = bedrock_agent.list_prompts(**request)
        for summary in response.get("promptSummaries", []):
            arn = str(summary.get("arn") or summary.get("promptArn") or "")
            version = str(summary.get("version") or _prompt_version_from_arn(arn))
            if not version or version.upper() == "DRAFT":
                continue
            versions.append({
                "version": version,
                "arn": arn or f"{prompt_arn}:{version}",
                "description": str(summary.get("description") or ""),
                "status": str(summary.get("status") or ""),
                "created_at": summary.get("createdAt"),
                "updated_at": summary.get("updatedAt"),
            })
        next_token = response.get("nextToken")
        if not next_token:
            break
    versions.sort(key=lambda item: int(item["version"]) if item["version"].isdigit() else 0, reverse=True)
    return versions


def _prompt_variable_names(*values: str) -> list[str]:
    names: list[str] = []
    for value in values:
        for name in re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", value):
            if name not in names:
                names.append(name)
    return names


def _prompt_text_blocks(blocks: list[dict[str, Any]] | None) -> str:
    return "\n\n".join(str(block.get("text", "")) for block in (blocks or []) if block.get("text"))


def _get_prompt_draft(prompt_arn: str) -> dict[str, Any]:
    return bedrock_agent.get_prompt(
        promptIdentifier=prompt_arn,
        promptVersion="DRAFT",
    )


def _prompt_draft_payload(prompt: dict[str, Any]) -> dict[str, Any]:
    variants = prompt.get("variants") or []
    variant = variants[0] if variants else {}
    configuration = (variant.get("templateConfiguration") or {}).get("chat") or {}
    user_messages = [message for message in configuration.get("messages", []) if message.get("role") == "user"]
    user_template = "\n\n".join(_prompt_text_blocks(message.get("content")) for message in user_messages)
    inference = (variant.get("inferenceConfiguration") or {}).get("text") or {}
    return {
        "name": str(prompt.get("name") or ""),
        "description": str(prompt.get("description") or ""),
        "version": str(prompt.get("version") or "DRAFT"),
        "updated_at": prompt.get("updatedAt"),
        "system": _prompt_text_blocks(configuration.get("system")),
        "user": user_template,
        "model_id": str(variant.get("modelId") or DEFAULT_PROMPT_MODEL_ID),
        "max_tokens": inference.get("maxTokens", 1000),
        "temperature": inference.get("temperature", 0),
        "input_variables": [str(item.get("name")) for item in configuration.get("inputVariables", []) if item.get("name")],
    }


def _prompt_fields(payload: dict[str, Any], draft: dict[str, Any] | None = None) -> dict[str, Any]:
    draft = draft or {}
    name = str(payload.get("name", draft.get("name", ""))).strip()
    description = str(payload.get("description", draft.get("description", ""))).strip()[:200]
    system = str(payload.get("system", draft.get("system", ""))).strip()
    user_template = str(payload.get("user", draft.get("user", "{{input}}"))).strip()
    model_id = str(payload.get("model_id", draft.get("model_id", DEFAULT_PROMPT_MODEL_ID))).strip()
    if not name:
        raise ValueError("prompt name is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", name):
        raise ValueError("prompt name must contain only letters, numbers, hyphens, and underscores")
    if not user_template:
        raise ValueError("user message template is required")
    if not model_id:
        raise ValueError("model ID is required")
    try:
        max_tokens = int(payload.get("max_tokens", draft.get("max_tokens", 1000)))
    except (TypeError, ValueError) as exc:
        raise ValueError("max tokens must be a positive integer") from exc
    if max_tokens < 1:
        raise ValueError("max tokens must be a positive integer")
    try:
        temperature = float(payload.get("temperature", draft.get("temperature", 0)))
    except (TypeError, ValueError) as exc:
        raise ValueError("temperature must be a number between 0 and 2") from exc
    if temperature < 0 or temperature > 2:
        raise ValueError("temperature must be a number between 0 and 2")
    return {
        "name": name,
        "description": description,
        "system": system,
        "user": user_template,
        "model_id": model_id,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "input_variables": _prompt_variable_names(system, user_template),
    }


def _prompt_variant(fields: dict[str, Any]) -> dict[str, Any]:
    chat: dict[str, Any] = {
        "messages": [{"role": "user", "content": [{"text": fields["user"]}]}],
        "inputVariables": [{"name": name} for name in fields["input_variables"]],
    }
    if fields["system"]:
        chat["system"] = [{"text": fields["system"]}]
    return {
        "name": "default",
        "templateType": "CHAT",
        "templateConfiguration": {"chat": chat},
        "modelId": fields["model_id"],
        "inferenceConfiguration": {
            "text": {
                "maxTokens": fields["max_tokens"],
                "temperature": fields["temperature"],
            }
        },
    }


def _active_prompt(key: str, definition: dict[str, Any]) -> dict[str, str]:
    if not definition.get("runtime"):
        return {"version": "", "arn": "", "updated_at": ""}
    item = _prompt_selection_table().get_item(Key={"PromptKey": key}).get("Item", {})
    active_arn = str(item.get("ActiveArn") or definition["default_arn"])
    return {
        "version": str(item.get("ActiveVersion") or _prompt_version_from_arn(active_arn)),
        "arn": active_arn,
        "updated_at": str(item.get("UpdatedAt") or ""),
    }


def _list_prompts(claims: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(claims):
        raise PermissionError("only admins can manage prompt versions")
    prompts: list[dict[str, Any]] = []
    for key, definition in _prompt_definitions().items():
        draft = _get_prompt_draft(definition["arn"])
        versions = _prompt_versions(definition["arn"])
        active = _active_prompt(key, definition)
        prompts.append({
            "key": key,
            "label": definition["label"],
            "arn": definition["arn"],
            "runtime": bool(definition.get("runtime")),
            "default_version": _prompt_version_from_arn(definition.get("default_arn", "")),
            "active_version": active["version"] or None,
            "active_arn": active["arn"] or None,
            "active_updated_at": active["updated_at"] or None,
            "draft": _prompt_draft_payload(draft),
            "versions": versions,
        })
    return {"prompts": prompts}


def _create_prompt(event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(claims):
        raise PermissionError("only admins can create prompts")
    payload = _body(event)
    fields = _prompt_fields(payload)
    name = fields["name"]
    if not name.startswith(PROMPT_NAME_PREFIX):
        name = f"{PROMPT_NAME_PREFIX}{name}"
        fields["name"] = name
    existing_names = {str(definition.get("name") or "") for definition in _prompt_definitions().values()}
    if name in existing_names:
        raise ValueError("a prompt with that name already exists")
    request: dict[str, Any] = {
        "name": name,
        "description": fields["description"],
        "defaultVariant": "default",
        "variants": [_prompt_variant(fields)],
    }
    if KMS_KEY_ARN:
        request["customerEncryptionKeyArn"] = KMS_KEY_ARN
    created = bedrock_agent.create_prompt(**request)
    prompt_arn = str(created.get("arn") or "")
    prompt_id = str(created.get("id") or _prompt_version_from_arn(prompt_arn))
    return {
        "prompt_key": f"library_{prompt_id}",
        "name": name,
        "arn": prompt_arn,
        "version": str(created.get("version") or "DRAFT"),
        "status": "created",
    }


def _update_prompt(key: str, event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(claims):
        raise PermissionError("only admins can edit prompts")
    definition = _prompt_definitions().get(key)
    if not definition or not definition["arn"]:
        raise ValueError("unknown prompt")
    current = _get_prompt_draft(definition["arn"])
    current_fields = _prompt_draft_payload(current)
    payload = _body(event)
    if definition.get("runtime"):
        payload["name"] = current_fields["name"]
    fields = _prompt_fields(payload, current_fields)
    if definition.get("runtime") and "input" not in fields["input_variables"]:
        raise ValueError("runtime prompts must keep the {{input}} variable in the template")
    request: dict[str, Any] = {
        "promptIdentifier": definition["arn"],
        "name": fields["name"],
        "description": fields["description"],
        "defaultVariant": "default",
        "variants": [_prompt_variant(fields)],
    }
    encryption_key = str(current.get("customerEncryptionKeyArn") or KMS_KEY_ARN or "")
    if encryption_key:
        request["customerEncryptionKeyArn"] = encryption_key
    updated = bedrock_agent.update_prompt(**request)
    return {
        "prompt_key": key,
        "name": fields["name"],
        "arn": str(updated.get("arn") or definition["arn"]),
        "version": str(updated.get("version") or "DRAFT"),
        "status": "draft_saved",
    }


def _create_prompt_version(key: str, event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(claims):
        raise PermissionError("only admins can manage prompt versions")
    definition = _prompt_definitions().get(key)
    if not definition or not definition["arn"]:
        raise ValueError("unknown prompt")
    payload = _body(event)
    description = str(payload.get("description") or "Created from the Bedrock draft.").strip()[:200]
    created = bedrock_agent.create_prompt_version(promptIdentifier=definition["arn"], description=description)
    version = str(created.get("version") or _prompt_version_from_arn(str(created.get("arn") or "")))
    return {
        "prompt_key": key,
        "version": version,
        "arn": str(created.get("arn") or f"{definition['arn']}:{version}"),
        "description": description,
        "status": "created",
    }


def _activate_prompt_version(key: str, event: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(claims):
        raise PermissionError("only admins can manage prompt versions")
    definition = _prompt_definitions().get(key)
    if not definition or not definition["arn"]:
        raise ValueError("unknown prompt")
    payload = _body(event)
    version = str(payload.get("version") or "").strip()
    if not version.isdigit() or int(version) < 1:
        raise ValueError("version must be a positive integer")
    versions = _prompt_versions(definition["arn"])
    selected = next((item for item in versions if item["version"] == version), None)
    if selected is None:
        raise ValueError("prompt version does not exist")
    updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _prompt_selection_table().put_item(
        Item={
            "PromptKey": key,
            "PromptArn": definition["arn"],
            "ActiveVersion": version,
            "ActiveArn": selected["arn"],
            "UpdatedAt": updated_at,
        }
    )
    return {"prompt_key": key, "version": version, "arn": selected["arn"], "updated_at": updated_at, "status": "active"}


def _api(event: dict[str, Any], path: str, claims: dict[str, Any]) -> dict[str, Any]:
    method = str(event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod", "GET")).upper()
    parts = [part for part in path.split("/") if part]
    if parts == ["api", "investigations"] and method == "POST":
        return _response(202, _create_investigation(event, claims))
    if len(parts) == 3 and parts[:2] == ["api", "investigations"]:
        workflow_id = parts[2]
        if method == "GET":
            return _response(200, _get_investigation(workflow_id, claims))
    if len(parts) == 4 and parts[:2] == ["api", "investigations"] and parts[3] == "events" and method == "GET":
        status = _get_investigation(parts[2], claims)
        return _response(200, {"workflow_id": parts[2], "events": status["events"]})
    if parts == ["api", "admin", "users"] and method == "GET":
        return _response(200, {"users": _list_users(claims)})
    if parts == ["api", "admin", "users"] and method == "POST":
        return _response(201, _create_user(event, claims))
    if parts == ["api", "admin", "dev-tools"] and method == "GET":
        return _response(200, _list_dev_tools(claims))
    if parts == ["api", "admin", "dev-tools", "failure-plans"] and method == "POST":
        return _response(201, {"control": _arm_failure_plan(event, claims)})
    if len(parts) == 6 and parts[:4] == ["api", "admin", "dev-tools", "failure-plans"] and parts[5] == "disable" and method == "POST":
        return _response(200, {"control": _disable_failure_plan(parts[4], claims)})
    if parts == ["api", "admin", "prompts"] and method == "POST":
        return _response(201, _create_prompt(event, claims))
    if parts == ["api", "admin", "prompts"] and method == "GET":
        return _response(200, _list_prompts(claims))
    if len(parts) == 4 and parts[:3] == ["api", "admin", "prompts"] and method in {"PUT", "PATCH"}:
        return _response(200, _update_prompt(parts[3], event, claims))
    if len(parts) == 5 and parts[:3] == ["api", "admin", "prompts"]:
        if parts[4] == "versions" and method == "POST":
            return _response(201, _create_prompt_version(parts[3], event, claims))
        if parts[4] == "active" and method == "POST":
            return _response(200, _activate_prompt_version(parts[3], event, claims))
    return _response(404, {"error": "not_found"})


def _static(path: str) -> dict[str, Any]:
    if path == "/":
        path = "/index.html"
    files = {
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/app.js": ("app.js", "application/javascript; charset=utf-8"),
        "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    }
    if path == "/config.js":
        body = (
            "window.LEDGERFLOW_CONFIG = "
            + json.dumps({"region": REGION, "environment": ENVIRONMENT_NAME, "userPoolId": USER_POOL_ID, "clientId": USER_POOL_CLIENT_ID})
            + ";"
        )
        return _response(200, body, "application/javascript; charset=utf-8")
    entry = files.get(path)
    if not entry:
        return _response(404, "not_found", "text/plain; charset=utf-8")
    file_path = FRONTEND_DIR / entry[0]
    if not file_path.exists():
        return _response(500, "frontend asset missing", "text/plain; charset=utf-8")
    return _response(200, file_path.read_text(encoding="utf-8"), entry[1])


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    path = event.get("rawPath") or event.get("path") or "/"
    method = str(event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod", "GET")).upper()
    try:
        if path.startswith("/api/"):
            return _api(event, path, _claims(event))
        if method == "GET":
            return _static(path)
        return _response(404, {"error": "not_found"})
    except PermissionError as exc:
        return _response(403, {"error": str(exc)})
    except ValueError as exc:
        return _response(400, {"error": str(exc)})
    except Exception as exc:
        print(json.dumps({"event": "web_api_error", "path": path, "error": str(exc)}))
        return _response(500, {"error": "internal_error"})
