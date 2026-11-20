"""HTTP entry point for an AgentCore Runtime container."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .runtime_entry import RuntimeHandler


class RuntimeRequestHandler(BaseHTTPRequestHandler):
    runtime_handler: RuntimeHandler | None = None
    max_request_bytes = 1_048_576

    def _write(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract.
        if self.path in ("/ping", "/health"):
            self._write(200, {"status": "healthy"})
        else:
            self._write(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract.
        if self.path not in ("/invocations", "/invoke"):
            self._write(404, {"error": "not_found"})
            return
        try:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._write(400, {"error": "content_length_required"})
                return
            length = int(raw_length)
            if length <= 0:
                self._write(400, {"error": "request_body_required"})
                return
            if length > self.max_request_bytes:
                self._write(413, {"error": "request_too_large"})
                return
        except ValueError:
            self._write(400, {"error": "invalid_json_request"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json_request"})
            return
        if not isinstance(payload, dict):
            self._write(400, {"error": "request_body_must_be_object"})
            return
        if self.runtime_handler is None:
            self._write(500, {"error": "internal_error"})
            return
        try:
            result = self.runtime_handler.handle_invocation(payload)
        except PermissionError:
            self._write(403, {"error": "forbidden"})
            return
        except ValueError:
            self._write(400, {"error": "invalid_request"})
            return
        except Exception:
            self._write(500, {"error": "internal_error"})
            return
        self._write(200, result)


def main() -> None:
    runtime = RuntimeHandler()
    RuntimeRequestHandler.runtime_handler = runtime
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), RuntimeRequestHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        runtime.close()


if __name__ == "__main__":
    main()
