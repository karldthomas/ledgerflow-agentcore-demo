"""Minimal SigV4-authenticated MCP client for an AgentCore Gateway."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from uuid import uuid4


class GatewayInvocationError(RuntimeError):
    """Raised when an MCP Gateway call cannot be completed."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AgentCoreGatewayClient:
    """Invoke Gateway tools over streamable HTTP MCP with session recovery."""

    def __init__(self, gateway_url: str, *, region: str | None = None, protocol_version: str = "2025-11-25") -> None:
        self.gateway_url = gateway_url.rstrip("/") + ("" if gateway_url.rstrip("/").endswith("/mcp") else "/mcp")
        self.region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        self.protocol_version = protocol_version
        self._session_id: str | None = None
        self._initialized = False

    def _signed_headers(self, body: bytes, *, session_id: str | None = None) -> dict[str, str]:
        try:
            import boto3
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest
        except ImportError as exc:
            raise GatewayInvocationError("boto3/botocore is required for Gateway invocation") from exc
        session = boto3.Session()
        credentials = session.get_credentials()
        if credentials is None:
            raise GatewayInvocationError("no AWS credentials available for SigV4 Gateway invocation")
        frozen = credentials.get_frozen_credentials()
        request = AWSRequest(
            method="POST",
            url=self.gateway_url,
            data=body,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": self.protocol_version,
                **({"Mcp-Session-Id": session_id} if session_id else {}),
            },
        )
        SigV4Auth(frozen, "bedrock-agentcore", self.region).add_auth(request)
        return {str(key): str(value) for key, value in request.headers.items()}

    @staticmethod
    def _extract(response: dict[str, Any]) -> Any:
        if response.get("error"):
            raise GatewayInvocationError(json.dumps(response["error"], sort_keys=True))
        result = response.get("result", response)
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            for item in content:
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    try:
                        return json.loads(item["text"])
                    except json.JSONDecodeError:
                        return item["text"]
        return result

    def _reset_session(self) -> None:
        self._session_id = None
        self._initialized = False

    def _request(self, payload: dict[str, Any], *, allow_empty: bool = False) -> tuple[Any, dict[str, str]]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.gateway_url,
            data=body,
            method="POST",
            headers=self._signed_headers(body, session_id=self._session_id),
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=max(1, int(os.environ.get("GATEWAY_TIMEOUT_SECONDS", "120"))),
            ) as response:
                raw = response.read().decode("utf-8")
                response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            detail = exc.read().decode("utf-8", errors="replace") if isinstance(exc, urllib.error.HTTPError) else str(exc)
            status = f" status={exc.code}" if isinstance(exc, urllib.error.HTTPError) else ""
            raise GatewayInvocationError(
                f"Gateway call failed{status}: {detail}",
                status=exc.code if isinstance(exc, urllib.error.HTTPError) else None,
            ) from exc
        if response_headers.get("mcp-session-id"):
            self._session_id = response_headers["mcp-session-id"]
        if not raw and allow_empty:
            return {}, response_headers
        frames: list[str] = []
        if raw.lstrip().startswith("data:"):
            current: list[str] = []
            for line in raw.splitlines():
                if line.startswith("data:"):
                    current.append(line[5:].lstrip())
                elif not line.strip() and current:
                    frames.append("\n".join(current))
                    current = []
            if current:
                frames.append("\n".join(current))
        else:
            frames = [raw]
        decoded: list[dict[str, Any]] = []
        for frame in frames:
            if not frame.strip():
                continue
            try:
                value = json.loads(frame)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                decoded.append(value)
        if not decoded:
            if allow_empty and not raw:
                return {}, response_headers
            raise GatewayInvocationError(f"Gateway returned non-JSON response: {raw[:500]}")
        request_id = payload.get("id")
        selected = next((value for value in decoded if request_id and value.get("id") == request_id), decoded[-1])
        return self._extract(selected), response_headers

    def _initialize(self) -> None:
        if self._initialized or self.protocol_version == "2026-07-28":
            self._initialized = True
            return
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "ledgerflow-runtime", "version": "0.1.0"},
            },
        }
        try:
            self._request(payload)
        except GatewayInvocationError as exc:
            # A stateless Gateway may accept tools/call without creating a
            # session. Only fall back for an unsupported initialize method;
            # authentication and transport failures remain fatal.
            message = str(exc).lower()
            if "status=400" not in message and "status=404" not in message and "method not found" not in message:
                raise
        self._initialized = True

        # MCP requires the initialized notification after a successful
        # handshake. Gateways that are stateless simply return no body here.
        if self._session_id:
            self._request(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                allow_empty=True,
            )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid4()),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        for attempt in range(2):
            try:
                self._initialize()
                result, _ = self._request(payload)
                return result
            except GatewayInvocationError as exc:
                if attempt == 0 and self._session_id and exc.status in {401, 404}:
                    self._reset_session()
                    continue
                raise
        raise GatewayInvocationError("Gateway call failed after session recovery")
