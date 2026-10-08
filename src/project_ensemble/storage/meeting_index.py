from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from project_ensemble.user_settings import settings_dir

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class MeetingIndexEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    meeting_id: str
    title: str
    path: str
    meeting_type: str
    deliverable_type: str
    created_at: str
    last_seen_at: str


def meeting_index_path(config_path: str | Path) -> Path:
    """Return one user-global index, independent of checkout or workspace."""

    return settings_dir() / "meetings.json"


def _legacy_index_path(config_path: str | Path) -> Path:
    return Path(config_path).expanduser().resolve().parent / ".ensemble" / "meetings.json"


def _lock(handle, acquire: bool) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if acquire else fcntl.LOCK_UN)


def meeting_is_complete(root: str | Path) -> bool:
    root = Path(root)
    archived = root / "public/archive_manifest.json"
    if archived.is_file():
        try:
            record = json.loads(archived.read_text(encoding="utf-8"))
            document = record.get("retained_document_path")
            if record.get("status") != "ARCHIVED" or not isinstance(document, str):
                return False
            relative = Path(document)
            return (
                not relative.is_absolute()
                and ".." not in relative.parts
                and (root / relative).is_file()
            )
        except (OSError, ValueError, TypeError):
            return False
    rendering = root / "public/scholarly_rendering/execution_result.json"
    if rendering.is_file():
        try:
            return json.loads(rendering.read_text(encoding="utf-8")).get("status") == "HANDOFF_READY"
        except (OSError, ValueError, TypeError):
            return False
    literature = root / "public/literature_report/execution_result.json"
    if literature.is_file():
        try:
            return json.loads(literature.read_text(encoding="utf-8")).get("status") == "HANDOFF_READY"
        except (OSError, ValueError, TypeError):
            return False
    research = root / "public/research/research_only_result.json"
    if research.is_file():
        try:
            return json.loads(research.read_text(encoding="utf-8")).get("status") in {
                "RESEARCH_COMPLETE",
                "RESEARCH_QC_FAILED",
            }
        except (OSError, ValueError, TypeError):
            return False
    return (root / "MEETING_RESULTS.md").is_file() or (
        (root / "public/final/final_publication_manifest.json").is_file()
        and (root / "human_private/final/chair_accountability_report.md").is_file()
    )


def _archived_document_title(root: Path) -> str | None:
    """Use the retained report's own title for an archived meeting listing."""
    archive_path = root / "public/archive_manifest.json"
    if not archive_path.is_file():
        return None
    try:
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        relative = Path(str(archive["retained_document_path"]))
        if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".md":
            return None
        with (root / relative).open(encoding="utf-8") as document:
            for number, line in enumerate(document):
                if number >= 100:
                    break
                if line.startswith("# "):
                    title = " ".join(line[2:].strip().split())
                    return title or None
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        pass
    return None


def inspect_meeting(root: str | Path) -> MeetingIndexEntry:
    root = Path(root).expanduser().resolve()
    manifest_path = root / "public/meeting_manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"not an ENSEMBLE meeting directory: {root}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task_path = root / "public/task.json"
    task = json.loads(task_path.read_text(encoding="utf-8")) if task_path.is_file() else {}
    document_title = _archived_document_title(root)
    title = str(
        document_title
        or manifest.get("title") or task.get("title") or task.get("description") or manifest["meeting_id"]
    )
    title = " ".join(title.split())
    if document_title is None and len(title) > 72:
        title = title[:71].rstrip("，,：: ") + "…"
    now = datetime.now(timezone.utc).isoformat()
    return MeetingIndexEntry(
        meeting_id=str(manifest["meeting_id"]),
        title=title,
        path=str(root),
        meeting_type=str(manifest.get("meeting_type", "deliberation")),
        deliverable_type=str(manifest.get("deliverable_type", "normative_instrument")),
        created_at=str(manifest.get("created_at", now)),
        last_seen_at=now,
    )


def register_meeting(root: str | Path, config_path: str | Path) -> MeetingIndexEntry:
    entry = inspect_meeting(root)
    index_path = meeting_index_path(config_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = index_path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        if os.name == "nt" and lock.tell() == 0:
            lock.write("0")
            lock.flush()
        _lock(lock, True)
        # Preserve entries whose directories were moved: they are exactly the
        # records needed to recognize later relocation attempts.
        entries = _read_entries(_legacy_index_path(config_path)) + _read_entries(index_path)
        by_id = {item.meeting_id: item for item in entries}
        by_id[entry.meeting_id] = entry
        payload = {
            "version": 1,
            "meetings": [
                item.model_dump(mode="json")
                for item in sorted(
                    by_id.values(), key=lambda value: value.created_at, reverse=True
                )
            ],
        }
        temporary = index_path.with_name(f".{index_path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, index_path)
        _lock(lock, False)
    return entry


def unregister_meeting(root: str | Path, config_path: str | Path) -> None:
    """Remove one exact workspace from the user-global meeting list."""
    target = Path(root).expanduser().resolve()
    index_path = meeting_index_path(config_path)
    if not index_path.is_file():
        return
    lock_path = index_path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        if os.name == "nt" and lock.tell() == 0:
            lock.write("0")
            lock.flush()
        _lock(lock, True)
        entries = _read_entries(index_path)
        remaining = [item for item in entries if Path(item.path).expanduser().resolve() != target]
        if len(remaining) != len(entries):
            payload = {"version": 1, "meetings": [item.model_dump(mode="json") for item in remaining]}
            temporary = index_path.with_name(f".{index_path.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, index_path)
        _lock(lock, False)


def indexed_meetings(config_path: str | Path) -> list[MeetingIndexEntry]:
    entries = _read_entries(_legacy_index_path(config_path)) + _read_entries(meeting_index_path(config_path))
    by_id = {entry.meeting_id: entry for entry in entries}
    return [entry for entry in by_id.values() if (Path(entry.path) / "public/meeting_manifest.json").is_file()]


def registered_meeting(meeting_id: str, config_path: str | Path) -> MeetingIndexEntry | None:
    """Look up a saved ID even when its old directory no longer exists."""

    entries = _read_entries(_legacy_index_path(config_path)) + _read_entries(meeting_index_path(config_path))
    return next((entry for entry in reversed(entries) if entry.meeting_id == meeting_id), None)


def resolve_indexed_meeting(selector: str, config_path: str | Path) -> Path | None:
    candidate = Path(selector).expanduser()
    direct_candidates = [candidate, Path.cwd() / candidate]
    for direct in direct_candidates:
        if (direct / "public/meeting_manifest.json").is_file():
            return direct.resolve()
    entries = indexed_meetings(config_path)
    for entry in entries:
        if entry.meeting_id == selector:
            return Path(entry.path)
    title_matches = [entry for entry in entries if entry.title.casefold() == selector.casefold()]
    if len(title_matches) == 1:
        return Path(title_matches[0].path)
    current_child = Path.cwd() / selector
    if (current_child / "public/meeting_manifest.json").is_file():
        return current_child.resolve()
    return None


def discover_local_meetings(directory: str | Path) -> list[Path]:
    directory = Path(directory).expanduser().resolve()
    found: list[Path] = []
    if (directory / "public/meeting_manifest.json").is_file():
        found.append(directory)
    if directory.is_dir():
        # A relocated meeting need not retain its original directory name.
        found.extend(
            path for path in sorted(directory.iterdir())
            if path.is_dir() and (path / "public/meeting_manifest.json").is_file()
        )
    return list(dict.fromkeys(path.resolve() for path in found))


def _read_entries(path: Path) -> list[MeetingIndexEntry]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [MeetingIndexEntry.model_validate(item) for item in payload.get("meetings", [])]
    except (OSError, ValueError, TypeError):
        return []
