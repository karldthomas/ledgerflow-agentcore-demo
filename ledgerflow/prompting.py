"""Native Bedrock Prompt Management version selection for the runtime."""

from __future__ import annotations

from typing import Any


class PromptVersionResolver:
    """Resolve the immutable Bedrock prompt version active for a stage.

    Prompt bodies and immutable versions live in Bedrock Prompt Management.
    The small DynamoDB record only stores the operator-selected version, so a
    prompt can be changed without rebuilding the Runtime container.
    """

    def __init__(
        self,
        *,
        prompt_arn: str | None,
        default_version_arn: str | None,
        selection_table: Any | None = None,
        prompt_key: str,
    ) -> None:
        self.prompt_arn = (prompt_arn or "").strip()
        self.default_version_arn = (default_version_arn or "").strip()
        self.selection_table = selection_table
        self.prompt_key = prompt_key

    def resolve(self) -> str:
        """Return the selected version ARN, falling back to the initial version."""

        if self.selection_table is not None:
            try:
                item = self.selection_table.get_item(Key={"PromptKey": self.prompt_key}).get("Item", {})
            except Exception as exc:  # pragma: no cover - exercised by deployed AWS permissions
                print({"event": "prompt_selection_degraded", "prompt_key": self.prompt_key, "error": str(exc)})
                item = {}
            selected = str(item.get("ActiveArn", "")).strip()
            if selected:
                return selected

        if self.default_version_arn:
            return self.default_version_arn
        if self.prompt_arn:
            raise RuntimeError(f"no active version configured for prompt {self.prompt_key}")
        raise RuntimeError(f"prompt {self.prompt_key} is not configured")
