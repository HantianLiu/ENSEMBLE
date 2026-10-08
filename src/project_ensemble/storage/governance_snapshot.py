"""Meeting-local immutable governance packages, independent of live updates."""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from project_ensemble.storage.documents import ImmutableDocumentStore

GOVERNANCE_SNAPSHOT = Path("human_private/governance_snapshot")


def freeze_governance_docs(source: str | Path, destination: str | Path, *, expected_digest: str | None = None) -> Path:
    """Copy an exact package; never overwrite an existing snapshot."""
    source = Path(source).expanduser().resolve()
    destination = Path(destination).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"governance directory is missing: {source}")
    # Capture everything before creating the destination so a source that
    # contains the workspace cannot recursively capture its own snapshot.
    documents = {
        str(path.relative_to(source)).replace("\\", "/"): path.read_bytes()
        for path in sorted(source.rglob("*")) if path.is_file()
    }
    digest = hashlib.sha256()
    for relative, data in documents.items():
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    captured_digest = digest.hexdigest()
    if expected_digest is not None and captured_digest != expected_digest:
        raise ValueError("governance recovery package does not match the frozen digest")
    from project_ensemble.storage.meeting import directory_digest

    if destination.exists():
        if not destination.is_dir() or directory_digest(destination) != captured_digest:
            raise ValueError("existing governance snapshot differs; refusing to overwrite")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".governance-snapshot-", dir=destination.parent))
    try:
        store = ImmutableDocumentStore(stage)
        for relative, data in documents.items():
            store.write_once(relative, data)
        if directory_digest(stage) != captured_digest:
            raise ValueError("governance snapshot verification failed")
        stage.rename(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return destination
