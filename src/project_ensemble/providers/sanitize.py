from __future__ import annotations

from typing import Any


_HIDDEN_REASONING_KEYS = {
    "reasoning_content",
    "reasoning_details",
    "thinking_content",
    "thought_signature",
}


def without_hidden_reasoning(value: Any) -> Any:
    """Remove provider-specific hidden-reasoning fields before persistence or return."""
    if isinstance(value, list):
        return [without_hidden_reasoning(item) for item in value]
    if isinstance(value, dict):
        if value.get("thought") is True:
            return {"thought": True, "redacted": True}
        return {
            key: without_hidden_reasoning(item)
            for key, item in value.items()
            if key not in _HIDDEN_REASONING_KEYS
        }
    return value
