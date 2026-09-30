from __future__ import annotations

import os
import hashlib
from pathlib import Path
from project_ensemble.errors import ImmutableWriteError


class ImmutableDocumentStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, relative: str | Path) -> Path:
        path = (self.root / relative).resolve()
        root = self.root.resolve()
        if root not in path.parents and path != root:
            raise ValueError("path escapes document root")
        return path

    def write_once(self, relative: str | Path, content: str | bytes) -> Path:
        path = self._resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise ImmutableWriteError(f"immutable document already exists: {relative}") from exc
        mode = "wb" if isinstance(content, bytes) else "w"
        kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
        with os.fdopen(fd, mode, **kwargs) as f:
            f.write(content)
        return path

    def copy_once(
        self, relative: str | Path, source: str | Path, *, chunk_size: int = 4 * 1024 * 1024
    ) -> tuple[Path, int, str]:
        """Copy an immutable artifact with bounded memory and a streaming digest."""

        if chunk_size < 1:
            raise ValueError("copy chunk size must be positive")
        source = Path(source)
        if source.is_symlink():
            raise ValueError(f"cannot inherit a symlink: {source}")
        path = self._resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ImmutableWriteError(f"immutable document already exists: {relative}") from exc
        digest = hashlib.sha256()
        byte_count = 0
        try:
            with os.fdopen(fd, "wb") as outgoing, source.open("rb") as incoming:
                while chunk := incoming.read(chunk_size):
                    outgoing.write(chunk)
                    digest.update(chunk)
                    byte_count += len(chunk)
        except BaseException:
            # The target has not been committed as a complete immutable copy.
            path.unlink(missing_ok=True)
            raise
        return path, byte_count, digest.hexdigest()

    def read_text(self, relative: str | Path) -> str:
        return self._resolve(relative).read_text(encoding="utf-8")
