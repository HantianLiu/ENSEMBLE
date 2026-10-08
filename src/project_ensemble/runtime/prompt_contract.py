"""Meeting-frozen prompt/context versions; missing fields mean legacy bytes."""
from __future__ import annotations

import json
from pathlib import Path

from project_ensemble.errors import PolicyNotConfiguredError

CURRENT_PROMPT_CONTRACT_VERSION = 3
CURRENT_CONTEXT_ASSEMBLY_VERSION = 3
ADVISORY_HEADING = "## INHERITED ADVISORY DOCUMENT"


def prompt_contract_version(root: Path | None) -> int:
    if root is None:
        return 1
    path = root / "identity_private/meeting_manifest.json"
    if not path.is_file():
        return 1
    manifest = json.loads(path.read_text(encoding="utf-8"))
    version = manifest.get("prompt_contract_version", 1)
    assembly = manifest.get("context_assembly_version", 1)
    if type(version) is not int or version not in {1, 2, 3}:
        raise PolicyNotConfiguredError("UNSUPPORTED_PROMPT_CONTRACT_VERSION")
    if type(assembly) is not int or assembly != version:
        raise PolicyNotConfiguredError("UNSUPPORTED_CONTEXT_ASSEMBLY_VERSION")
    return version


def parse_json_prompt(text: str) -> tuple[dict, str] | None:
    """Parse a JSON object followed by one known, intact schema envelope.

    No heuristic brace search and no stripping arbitrary task instructions.
    The suffix is preserved byte-for-byte during lossless serialization/repair.
    """
    stripped = text.lstrip()
    try:
        payload, end = json.JSONDecoder().raw_decode(stripped)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    suffix = stripped[end:]
    if suffix.strip():
        markers = (
            "\n\nTARGET JSON SCHEMA:\n",
            "\n\n只返回一个符合以下结构的 JSON 对象：\n",
        )
        marker = next((m for m in markers if suffix.startswith(m)), None)
        if marker is None:
            return None
        try:
            schema = json.loads(suffix[len(marker):])
        except ValueError:
            return None
        if not isinstance(schema, dict):
            return None
    return payload, suffix
