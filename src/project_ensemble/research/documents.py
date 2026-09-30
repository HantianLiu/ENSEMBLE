from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from project_ensemble.research.models import (
    DocumentArchiveStatus,
    EvidenceSource,
    EvidenceUseClass,
)
from project_ensemble.storage.meeting import MeetingRepository


@dataclass(frozen=True)
class DownloadedDocument:
    content: bytes
    media_type: str
    final_url: str


def is_access_blocked_document(
    content: bytes, *, media_type: str, final_url: str | None = None
) -> bool:
    """Reject access-control pages masquerading as successfully fetched originals."""
    if media_type not in {"text/html", "application/xhtml+xml", "text/markdown", "text/plain"}:
        return False
    beginning = content[:16000].decode("utf-8-sig", errors="replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", beginning, flags=re.I | re.S)
    title_text = re.sub(r"\s+", " ", title.group(1)).strip() if title else ""
    blocked_heading = r"request access|access denied|attention required|just a moment|verify you are human"
    if title_text and re.search(blocked_heading, title_text, flags=re.I):
        return True
    path = (urlparse(final_url or "").path or "").lower()
    if re.search(r"/(?:unblock|access-denied|captcha|challenge)(?:/|$)", path):
        return True
    return bool(re.search(
        rf"(?im)^\s*#{{1,3}}\s*(?:{blocked_heading})(?:\s*[.!])?\s*$",
        beginning[:4000],
    ))


class DocumentFetcher(Protocol):
    def fetch(self, url: str) -> DownloadedDocument: ...


class HttpDocumentFetcher:
    """Fetch public source documents without attempting paywall or access-control bypass."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_bytes: int = 50_000_000,
        max_concurrent_requests: int = 4,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self._request_gate = threading.BoundedSemaphore(max_concurrent_requests)

    def fetch(self, url: str) -> DownloadedDocument:
        with self._request_gate:
            return self._fetch(url)

    def _fetch(self, url: str) -> DownloadedDocument:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("source document URL must be an absolute HTTP(S) URL")
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
            response = client.get(url, headers={"Accept": "application/pdf,text/*,application/xml"})
            response.raise_for_status()
        declared_length = response.headers.get("content-length")
        if declared_length and int(declared_length) > self.max_bytes:
            raise ValueError("source document exceeds configured maximum size")
        content = response.content
        if len(content) > self.max_bytes:
            raise ValueError("source document exceeds configured maximum size")
        if not content:
            raise ValueError("source document response is empty")
        media_type = response.headers.get("content-type", "application/octet-stream")
        media_type = media_type.split(";", 1)[0].strip().lower()
        if content.startswith(b"%PDF-"):
            media_type = "application/pdf"
        return DownloadedDocument(
            content=content,
            media_type=media_type,
            final_url=str(response.url),
        )


class LiteratureBundleManager:
    """Meeting-local archive and downloadable provenance bundle for cited source documents."""

    _EXTENSIONS = {
        "application/pdf": ".pdf",
        "text/html": ".html",
        "application/xhtml+xml": ".html",
        "application/xml": ".xml",
        "text/xml": ".xml",
        "text/plain": ".txt",
        "application/json": ".json",
    }

    def __init__(self, *, repo: MeetingRepository, fetcher: DocumentFetcher):
        self.repo = repo
        self.fetcher = fetcher
        self._content_write_lock = threading.Lock()

    def archive_sources(
        self,
        *,
        packet_id: str,
        sources: list[EvidenceSource],
        candidates: list[dict],
    ) -> list[EvidenceSource]:
        candidates_by_id = {str(item["source_id"]): item for item in candidates}
        archived_sources: list[EvidenceSource] = []
        for index, source in enumerate(sources, start=1):
            candidate = candidates_by_id.get(source.source_id, {})
            original_url = self._optional_text(candidate.get("full_text_url"))
            license_name = self._optional_text(candidate.get("license"))
            update: dict[str, object] = {
                "original_document_url": original_url,
                "license": license_name,
                "archive_status": DocumentArchiveStatus.NOT_AVAILABLE,
                "archived_path": None,
                "archived_sha256": None,
                "archived_media_type": None,
                "archived_size_bytes": None,
                "archived_at": None,
            }
            note = "No legally public full-text document URL was supplied by the retriever."
            resolved_url: str | None = None
            local_archive = candidate.get("local_archive_path")
            if local_archive:
                try:
                    relative_source = Path(str(local_archive))
                    unresolved_source = self.repo.root / relative_source
                    source_path = unresolved_source.resolve()
                    if (relative_source.is_absolute() or self.repo.root.resolve() not in source_path.parents
                            or not str(relative_source).startswith("public/human_references/files/")
                            or unresolved_source.is_symlink()):
                        raise ValueError("invalid meeting-local reference path")
                    content = source_path.read_bytes()
                    digest = hashlib.sha256(content).hexdigest()
                    if digest != candidate.get("local_archive_sha256"):
                        raise ValueError("human reference content hash mismatch")
                    extension = source_path.suffix.lower()
                    media_type = self._media_type_for_extension(extension)
                    if media_type is None:
                        raise ValueError("unsupported human reference media type")
                    filename = self._filename(source, digest, extension)
                    relative = Path("public/research/literature_bundle/documents") / filename
                    self._write_content_once_or_verify(relative, content, digest)
                    update.update({
                        "archive_status": DocumentArchiveStatus.ARCHIVED,
                        "archived_path": str(relative),
                        "archived_sha256": digest,
                        "archived_media_type": media_type,
                        "archived_size_bytes": len(content),
                        "archived_at": datetime.now(timezone.utc),
                    })
                    note = "Human-supplied original copy archived; upload alone does not establish validity."
                except (OSError, ValueError) as exc:
                    update["archive_status"] = DocumentArchiveStatus.DOWNLOAD_FAILED
                    note = f"Human reference archive failed: {type(exc).__name__}"
            elif original_url and bool(candidate.get("full_text_is_public")):
                try:
                    downloaded = self.fetcher.fetch(original_url)
                    resolved_url = downloaded.final_url
                    if is_access_blocked_document(
                        downloaded.content,
                        media_type=downloaded.media_type,
                        final_url=resolved_url,
                    ):
                        raise ValueError("access-control page returned instead of source document")
                    extension = self._EXTENSIONS.get(downloaded.media_type)
                    if extension is None:
                        update["archive_status"] = DocumentArchiveStatus.UNSUPPORTED_FORMAT
                        note = f"Unsupported document media type: {downloaded.media_type}"
                    else:
                        digest = hashlib.sha256(downloaded.content).hexdigest()
                        filename = self._filename(source, digest, extension)
                        relative = Path("public/research/literature_bundle/documents") / filename
                        self._write_content_once_or_verify(relative, downloaded.content, digest)
                        archived_at = datetime.now(timezone.utc)
                        update.update(
                            {
                                "archive_status": DocumentArchiveStatus.ARCHIVED,
                                "archived_path": str(relative),
                                "archived_sha256": digest,
                                "archived_media_type": downloaded.media_type,
                                "archived_size_bytes": len(downloaded.content),
                                "archived_at": archived_at,
                            }
                        )
                        note = "Public source document archived without content modification."
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    update["archive_status"] = DocumentArchiveStatus.DOWNLOAD_FAILED
                    note = f"Public document download failed: {type(exc).__name__}: {str(exc)[:180]}"
            elif original_url:
                note = "A full-text URL exists, but the retriever did not mark it as publicly accessible."

            if (
                update["archive_status"] != DocumentArchiveStatus.ARCHIVED
                and source.evidence_use_class != EvidenceUseClass.DISCOVERY_ONLY
            ):
                update["evidence_use_class"] = EvidenceUseClass.PROVISIONAL

            archived = EvidenceSource.model_validate(
                {**source.model_dump(mode="python"), **update}
            )
            archived_sources.append(archived)
            record = {
                "packet_id": packet_id,
                "source_id": archived.source_id,
                "title": archived.title,
                "authors": archived.authors,
                "publication_year": archived.publication_year,
                "doi": archived.doi,
                "citation_url": archived.url,
                "original_document_url": original_url,
                "resolved_document_url": resolved_url,
                "license": license_name,
                "archive_status": archived.archive_status.value,
                "archived_path": archived.archived_path,
                "archived_sha256": archived.archived_sha256,
                "archived_media_type": archived.archived_media_type,
                "archived_size_bytes": archived.archived_size_bytes,
                "archived_at": (
                    archived.archived_at.isoformat() if archived.archived_at is not None else None
                ),
                "note": note,
            }
            record_relative = (
                Path("public/research/literature_bundle/records")
                / f"{packet_id}-{index:03d}.json"
            )
            self.repo.docs.write_once(
                record_relative,
                json.dumps(record, indent=2, ensure_ascii=False),
            )
        return archived_sources

    def rebuild_download_bundle(self) -> Path:
        bundle_root = self.repo.root / "public/research/literature_bundle"
        records = []
        for path in sorted((bundle_root / "records").glob("*.json")):
            records.append(json.loads(path.read_text(encoding="utf-8")))
        generated_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            "meeting_id": self.repo.meeting_id,
            "generated_at": generated_at,
            "scope": "THIS_MEETING_ONLY",
            "record_count": len(records),
            "archived_document_count": sum(
                record["archive_status"] == DocumentArchiveStatus.ARCHIVED.value
                for record in records
            ),
            "records": records,
        }
        readme = (
            "# Meeting literature bundle\n\n"
            "本目录仅属于当前会议。`manifest.json` 将每份来源关联到 evidence packet、原始 URL、"
            "访问时间、本地文件和 SHA-256。`documents/` 保存依法公开取得的原始文件；未能归档"
            "的来源保留链接和状态，不表示该来源不存在。\n\n"
            "Files are stored without content modification. Verify each file against the SHA-256 recorded "
            "in `manifest.json`.\n"
        )
        self._atomic_write(bundle_root / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False).encode())
        self._atomic_write(bundle_root / "README.md", readme.encode("utf-8"))

        zip_path = self.repo.root / "public/research/literature_bundle.zip"
        temporary = zip_path.with_name(f".{zip_path.name}.{secrets.token_hex(4)}.tmp")
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(bundle_root.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(bundle_root))
            os.replace(temporary, zip_path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return zip_path.relative_to(self.repo.root)

    def _write_content_once_or_verify(self, relative: Path, content: bytes, digest: str) -> None:
        with self._content_write_lock:
            path = self.repo.root / relative
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError("existing archived document does not match its content hash")
                return
            self.repo.docs.write_once(relative, content)

    @staticmethod
    def _filename(source: EvidenceSource, digest: str, extension: str) -> str:
        title = unicodedata.normalize("NFKD", source.title).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:80] or "source"
        year = str(source.publication_year) if source.publication_year is not None else "undated"
        return f"{year}-{slug}-{digest[:12]}{extension}"

    @staticmethod
    def _media_type_for_extension(extension: str) -> str | None:
        return {
            ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/plain",
            ".csv": "text/plain", ".html": "text/html", ".htm": "text/html",
        }.get(extension)

    @staticmethod
    def _optional_text(value: object) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
