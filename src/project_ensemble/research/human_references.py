"""Immutable, meeting-local documents supplied by the Human at initialization.

The upload is a retrieval candidate, never an automatic SOURCE_BACKED finding.
Its original bytes and extracted text remain separately auditable.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from project_ensemble.research.models import NormalizedClaim
from project_ensemble.research.pdf_warnings import capture_duplicate_pdf_length_warnings
from project_ensemble.research.retrievers import ResearchRetrievalResult
from project_ensemble.storage.meeting import MeetingRepository


SUPPORTED_REFERENCE_SUFFIXES = frozenset({".pdf", ".txt", ".md", ".html", ".htm", ".csv"})
MAX_REFERENCE_COUNT = 20
MAX_REFERENCE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_CHARACTERS = 200_000


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def validate_reference_paths(paths: tuple[str, ...] | list[str]) -> tuple[Path, ...]:
    if len(paths) > MAX_REFERENCE_COUNT:
        raise ValueError(f"at most {MAX_REFERENCE_COUNT} human reference files are allowed")
    validated: list[Path] = []
    for raw in paths:
        candidate = Path(raw).expanduser()
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"reference must be a regular, non-symlink file: {raw}")
        if candidate.suffix.lower() not in SUPPORTED_REFERENCE_SUFFIXES:
            raise ValueError(f"unsupported reference format: {candidate.suffix}")
        if candidate.stat().st_size > MAX_REFERENCE_BYTES:
            raise ValueError(f"reference exceeds {MAX_REFERENCE_BYTES} bytes: {raw}")
        resolved = candidate.resolve()
        if resolved not in validated:
            validated.append(resolved)
    return tuple(validated)


def _extract_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        try:
            with capture_duplicate_pdf_length_warnings():
                reader = PdfReader(str(path))
                parts: list[str] = []
                size = 0
                for page in reader.pages[:300]:
                    text = page.extract_text() or ""
                    parts.append(text)
                    size += len(text)
                    if size >= MAX_EXTRACTED_CHARACTERS:
                        break
            return "\n\n".join(parts)[:MAX_EXTRACTED_CHARACTERS]
        except Exception:
            # Scans and malformed PDFs remain archived, but not searchable.
            return ""
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return ""
    if path.suffix.lower() in {".html", ".htm"}:
        parser = _VisibleHTML()
        parser.feed(text)
        text = "".join(parser.parts)
    return text[:MAX_EXTRACTED_CHARACTERS]


def install_human_references(repo: MeetingRepository, paths: tuple[str, ...]) -> Path | None:
    """Freeze original files and a searchable extraction without trusting either as evidence."""
    validated = validate_reference_paths(paths)
    if not validated:
        return None
    records: list[dict] = []
    by_digest: set[str] = set()
    for source in validated:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest in by_digest:
            continue
        by_digest.add(digest)
        ref_id = f"HR-{digest[:16].upper()}"
        file_relative = Path("public/human_references/files") / f"{ref_id}{source.suffix.lower()}"
        _, size, copied_digest = repo.docs.copy_once(file_relative, source)
        if copied_digest != digest:
            raise ValueError(f"reference changed during import: {source}")
        extracted = _extract_text(repo.root / file_relative)
        text_relative = Path("public/human_references/text") / f"{ref_id}.txt"
        repo.docs.write_once(text_relative, extracted)
        records.append({
            "reference_id": ref_id,
            "original_filename": source.name,
            "file_path": str(file_relative),
            "file_sha256": digest,
            "size_bytes": size,
            "text_path": str(text_relative),
            "extracted_characters": len(extracted),
            "extraction_status": "SEARCHABLE" if extracted.strip() else "NO_SEARCHABLE_TEXT",
            "evidence_status": "CANDIDATE_NOT_VERIFIED",
        })
    relative = Path("public/human_references/manifest.json")
    repo.docs.write_once(relative, json.dumps({
        "meeting_id": repo.meeting_id,
        "scope": "THIS_MEETING_ONLY",
        "policy": "Human supply does not establish source quality or factual correctness.",
        "references": records,
    }, indent=2, ensure_ascii=False))
    return repo.root / relative


def _query_terms(query: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9]{3,}|[\u3400-\u9fff]{2,}", query.casefold())
    terms = set(words)
    for word in words:
        if len(word) >= 3 and "\u3400" <= word[0] <= "\u9fff":
            terms.update(word[index:index + 2] for index in range(len(word) - 1))
    return terms


def _relevant_excerpt(text: str, query: str, *, limit: int = 7000) -> str:
    if len(text) <= limit:
        return text
    terms = sorted(_query_terms(query), key=len, reverse=True)
    lowered = text.casefold()
    hits = sorted({lowered.find(term) for term in terms if lowered.find(term) >= 0})
    chunks = [text[: min(1400, limit)]]
    for position in hits[:5]:
        start = max(0, position - 350)
        chunks.append(text[start:start + 1100])
    return "\n[…]\n".join(chunks)[:limit]


class HumanReferenceRetriever:
    """Expose frozen uploads alongside external search without treating them as verified."""

    backend_ids = ("human_reference",)

    def __init__(self, repo: MeetingRepository):
        self.repo = repo
        manifest = json.loads(repo.docs.read_text("public/human_references/manifest.json"))
        self.references = tuple(manifest["references"])

    def retrieve(self, claim: NormalizedClaim) -> ResearchRetrievalResult:
        return self._search(claim.normalized_claim + " " + " ".join(claim.scope_terms))

    def retrieve_exploratory(self, query: str) -> ResearchRetrievalResult:
        return self._search(query)

    def _search(self, query: str) -> ResearchRetrievalResult:
        terms = _query_terms(query)
        ranked = []
        for record in self.references:
            if record["extraction_status"] != "SEARCHABLE":
                continue
            text = (self.repo.root / record["text_path"]).read_text(encoding="utf-8")
            haystack = (record["original_filename"] + " " + text).casefold()
            score = sum(1 for term in terms if term in haystack)
            ranked.append((score, record, text))
        ranked.sort(key=lambda item: (-item[0], item[1]["reference_id"]))
        candidates = []
        for score, record, text in ranked[:8]:
            candidates.append({
                "source_id": record["reference_id"],
                "title": record["original_filename"],
                "authors": [],
                "publication_year": None,
                "doi": None,
                "url": record["file_path"],
                "venue": None,
                "source_type": "human_supplied_document",
                "is_primary_source": None,
                "abstract": _relevant_excerpt(text, query),
                "relevance_score": score,
                "full_text_url": None,
                "full_text_is_public": False,
                "local_archive_path": record["file_path"],
                "local_archive_sha256": record["file_sha256"],
                "retrieval_backend_id": "human_reference",
            })
        return ResearchRetrievalResult(
            candidates=candidates,
            query_trace=[{
                "backend_id": "human_reference", "purpose": "human_supplied_candidate",
                "query": query, "returned_source_ids": [item["source_id"] for item in candidates],
            }],
            effective_backend_ids=self.backend_ids,
        )
