"""Meeting-local, Human-controlled standing delegation; frozen rulings stay intact."""

from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path


_DIRECTORY = Path("human_private/ai_delegation_settings")
FAST_KINDS = {"fast_scope", "fast_science"}


def available_delegations(repo) -> list[str]:
    manifest = json.loads((repo.root / "public/meeting_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("meeting_type") == "scholarly_rendering":
        return ["scholarly_science"]
    private = json.loads((repo.root / "identity_private/meeting_manifest.json").read_text(encoding="utf-8"))
    if private.get("literature_writing_policy") == "fast":
        return ["fast_scope", "fast_science"]
    # Legacy fixtures/meetings can identify their procedure through frozen issues.
    from project_ensemble.orchestration.consultations import HumanConsultationService
    if any(issue.stage.startswith("FAST_") for issue in HumanConsultationService(repo).open_issues()):
        return ["fast_scope", "fast_science"]
    return []


def delegation_setting(repo, kind: str) -> tuple[bool, str | None]:
    if kind == "scholarly_science":
        from project_ensemble.orchestration.consultations import ScienceConsultationAuthorityService
        mode, source = ScienceConsultationAuthorityService(repo).current()
        return mode == "chair", source
    if kind not in FAST_KINDS:
        raise ValueError("unsupported AI delegation kind")
    for path in reversed(sorted((repo.root / _DIRECTORY).glob("*.json"))):
        record = json.loads(path.read_text(encoding="utf-8"))
        if (record.get("meeting_id") != repo.meeting_id or record.get("authorized_by") != "HUMAN"
                or record.get("scope") != "FUTURE_ELIGIBLE_CONSULTATIONS_IN_THIS_MEETING"):
            raise ValueError("invalid meeting AI delegation authorization")
        if kind in record["settings"]:
            return record["settings"][kind] is True, str(path.relative_to(repo.root))
    if kind == "fast_scope":
        legacy = Path("human_private/consultations/fast_scope_writer_standing_delegation.json")
        path = repo.root / legacy
        if path.is_file():
            record = json.loads(path.read_text(encoding="utf-8"))
            return (record.get("authority") == "HUMAN"
                    and record.get("scope") == "FUTURE_FAST_SCOPE_ITEMS_IN_THIS_MEETING"), str(legacy)
    return False, None


def set_delegation_settings(repo, settings: dict[str, bool]) -> str:
    if not settings or any(kind not in available_delegations(repo) or type(value) is not bool
                           for kind, value in settings.items()):
        raise ValueError("AI delegation settings must name supported types and boolean switches")
    if "scholarly_science" in settings:
        from project_ensemble.orchestration.consultations import ScienceConsultationAuthorityService
        return ScienceConsultationAuthorityService(repo).set_mode(
            "chair" if settings["scholarly_science"] else "human",
        )
    folder = repo.root / _DIRECTORY
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            previous = sorted(folder.glob("*.json"))
            number = int(previous[-1].stem) + 1 if previous else 1
            relative = _DIRECTORY / f"{number:06d}.json"
            payload = {
                "meeting_id": repo.meeting_id, "sequence": number,
                "authorized_by": "HUMAN", "changed_at": datetime.now(timezone.utc).isoformat(),
                "scope": "FUTURE_ELIGIBLE_CONSULTATIONS_IN_THIS_MEETING", "settings": settings,
                "effect": "EXISTING_RULINGS_UNCHANGED;ORIGINAL_RECHECK_REQUIRED",
            }
            content = json.dumps(payload, ensure_ascii=False, indent=2)
            repo.docs.write_once(relative, content)
            repo.docs.write_once(Path("public/procedural_consultations/ai_delegation_settings") / relative.name, content)
            repo.events.append("MEETING_AI_DELEGATION_SETTINGS_CHANGED", payload, actor="HUMAN")
            return str(relative)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def delegation_model(repo, kind: str) -> str:
    from project_ensemble.runtime.model_replacements import current_runtime_for
    if kind == "fast_science":
        from project_ensemble.runtime.fast_science_delegation import _science_reviewer
        participant = _science_reviewer(repo)
    elif kind == "fast_scope":
        participant = "WRITER"
    elif kind == "scholarly_science":
        participant = "CHAIR"
    else:
        raise ValueError("unsupported AI delegation kind")
    return ":".join(current_runtime_for(repo, participant))
