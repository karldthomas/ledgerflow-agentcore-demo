"""AWS-only entry point for the AgentCore Runtime container."""

from __future__ import annotations

from typing import Any

from .cloud_agent import CloudInvestigationHandler


class RuntimeHandler:
    def __init__(self) -> None:
        self.cloud_handler = CloudInvestigationHandler()

    def handle_invocation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.cloud_handler.handle(payload)

    def close(self) -> None:
        return None


def main() -> None:
    handler = RuntimeHandler()
    try:
        for line in __import__("sys").stdin:
            if line.strip():
                print(json.dumps(handler.handle_invocation(json.loads(line))))
    finally:
        handler.close()


if __name__ == "__main__":
    main()
