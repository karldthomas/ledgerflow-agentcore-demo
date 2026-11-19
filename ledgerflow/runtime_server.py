"""HTTP entry point for an AgentCore Runtime container."""

from __future__ import annotations

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .runtime_entry import RuntimeHandler


class RuntimeRequestHandler(BaseHTTPRequestHandler):
    runtime_handler: RuntimeHandler | None = None

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
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.runtime_handler is None:
                raise RuntimeError("runtime handler is not initialized")
            self._write(200, self.runtime_handler.handle_invocation(payload))
        except Exception as exc:
            traceback.print_exc()
            self._write(500, {"error": str(exc)})


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
