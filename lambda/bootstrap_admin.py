"""CloudFormation custom resource for an optional Cognito bootstrap admin."""

from __future__ import annotations

import json
import urllib.request
from typing import Any

import boto3


def _reply(event: dict[str, Any], context: Any, status: str, reason: str = "") -> None:
    body = json.dumps({
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": event.get("PhysicalResourceId") or event.get("LogicalResourceId", "LedgerFlowBootstrapAdmin"),
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": {},
    }).encode("utf-8")
    request = urllib.request.Request(event["ResponseURL"], data=body, method="PUT", headers={"Content-Type": "", "Content-Length": str(len(body))})
    urllib.request.urlopen(request, timeout=10).read()


def handler(event: dict[str, Any], context: Any) -> None:
    try:
        if event.get("RequestType") == "Delete":
            _reply(event, context, "SUCCESS")
            return
        props = event.get("ResourceProperties", {})
        email = str(props.get("Email", "")).strip().lower()
        user_pool_id = str(props["UserPoolId"])
        if not email:
            _reply(event, context, "SUCCESS", "No bootstrap email supplied")
            return
        client = boto3.client("cognito-idp")
        try:
            client.admin_create_user(
                UserPoolId=user_pool_id,
                Username=email,
                UserAttributes=[{"Name": "email", "Value": email}, {"Name": "custom:tenant_id", "Value": str(props.get("TenantId", "tenant_acme"))}],
                DesiredDeliveryMediums=["EMAIL"],
            )
        except client.exceptions.UsernameExistsException:
            pass
        client.admin_add_user_to_group(UserPoolId=user_pool_id, Username=email, GroupName="admins")
        _reply(event, context, "SUCCESS")
    except Exception as exc:
        _reply(event, context, "FAILED", str(exc))
