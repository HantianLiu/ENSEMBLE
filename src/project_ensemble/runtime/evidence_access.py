"""Read-only ID lookup of already released meeting evidence; no network access."""
from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

FINDING_GROUPS = (
    "supporting_evidence", "contradictory_evidence", "scope_limitations", "canonical_alternatives",
)
PUBLIC_ORIGINAL_ROOTS = (
    Path("public/research/literature_bundle/documents"),
    Path("public/human_references/files"),
)
MATH_SPANS = re.compile(
    r"\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|(?<!\\)\$(?!\$)[^\n$]+(?<!\\)\$"
)


class EvidenceReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source_id: str = Field(min_length=1, max_length=500)
    view: str = Field(default="findings", pattern=r"^(findings|original)$")
    cursor: int = Field(default=0, ge=0, strict=True)
    page: int = Field(default=0, ge=0, strict=True)
    max_characters: int = Field(default=12000, ge=500, le=12000, strict=True)


class EvidenceReadBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_read_requests: list[EvidenceReadRequest] = Field(min_length=1, max_length=2)


def public_file(root: Path, relative: Path, *, prefixes=(Path("public"),)) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid public location")
    if not any(relative.is_relative_to(prefix) for prefix in prefixes):
        raise ValueError("location outside authorized public compartment")
    path = root
    for component in relative.parts:
        path = path / component
        if path.is_symlink():
            raise ValueError("symlink in public location")
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("not an ordinary meeting-local public file")
    return path


def _safe_end(text: str, start: int, size: int) -> int:
    """Prefer text boundaries and do not cut an existing display-math block."""
    end = min(len(text), start + size)
    if end == len(text):
        return end
    newline = text.rfind("\n", start + size // 2, end)
    if newline >= 0:
        end = newline + 1
    for span in MATH_SPANS.finditer(text):
        if span.start() < end < span.end():
            end = span.end()
            break
    if text[:end].count("$$") % 2:
        end = len(text)  # Incomplete source math is not silently cut further.
    return end


class PublicEvidenceReader:
    """Registry is built only from a current public dossier or released snapshots.

    Never scan research audit/staging directories or accept model-supplied paths.
    Public source records are projected; query trails, scores, and private archive
    paths never enter the returned data.
    """

    def __init__(self, root: Path, *, module_ids=(), writer=False, before_writer_version=None):
        self.root, self.writer = root, writer
        self.sources, self.findings, self.aliases = {}, {}, {}
        self.ambiguous_aliases = set()
        self.provenance = {}
        self.original_cache = {}
        self.errors = []
        packets = {}
        catalogs = []
        for module_id in sorted(set(module_ids)):
            if not re.fullmatch(r"RM-\d+", module_id):
                continue
            base = Path("public/literature_report/modules") / module_id
            dossier = self._json(base / "research/evidence_dossier.json")
            if dossier is None:
                continue
            for packet in dossier.get("packets", []):
                if isinstance(packet, dict) and packet.get("packet_id"):
                    packets.setdefault(packet["packet_id"], packet)
            # The dossier is itself a published module record. Resolve only
            # its explicitly listed packet IDs, including omitted records;
            # a directory-wide packet scan could expose staging material.
            approved_ids = [
                *[item.get("packet_id") for item in dossier.get("packets", []) if isinstance(item, dict)],
                *dossier.get("omitted_packet_ids", []),
            ]
            for packet_id in approved_ids:
                if re.fullmatch(r"RP-[A-Z0-9]+", str(packet_id)):
                    full = self._json(Path("public/research/evidence_packets") / f"{packet_id}.json")
                    if full is not None and full.get("packet_id") == packet_id:
                        packets[packet_id] = full
            catalog_relative = base / "research/chapter_citation_catalog.json"
            supplements = []
            directory = root / base / "writing_v071"
            if not directory.is_symlink():
                for path in directory.glob("writer_v*_citation_rechecked_catalog.json"):
                    match = re.fullmatch(r"writer_v(\d+)_citation_rechecked_catalog\.json", path.name)
                    if match and (before_writer_version is None or int(match[1]) < before_writer_version):
                        supplements.append((int(match.group(1)), path.relative_to(root)))
            if supplements:
                catalog_relative = max(supplements)[1]
            catalog = self._json(catalog_relative)
            if catalog is not None:
                catalogs.extend(catalog.get("sources", []))
                for entry in catalog.get("sources", []):
                    if not isinstance(entry, dict):
                        continue
                    for packet_id in entry.get("packet_ids", []):
                        if not re.fullmatch(r"RP-[A-Z0-9]+", str(packet_id)):
                            continue
                        full = self._json(Path("public/research/evidence_packets") / f"{packet_id}.json")
                        if full is not None and full.get("packet_id") == packet_id:
                            packets[packet_id] = full
        if not module_ids:
            # Presence of a finished snapshot is the release gate; copied packets
            # or PDF files alone are not publication authorization.
            snapshot_root = root / "public/research/rounds"
            if not snapshot_root.is_symlink():
                for path in sorted(snapshot_root.glob("*/evidence_snapshot.json")):
                    snapshot = self._json(path.relative_to(root))
                    if snapshot is None or snapshot.get("status") != "RELEASED":
                        continue
                    for packet in snapshot.get("packets", []):
                        if isinstance(packet, dict) and packet.get("packet_id"):
                            packets.setdefault(packet["packet_id"], packet)
                    for packet_id in snapshot.get("packet_ids", []):
                        if not re.fullmatch(r"RP-[A-Z0-9]+", str(packet_id)):
                            continue
                        packet = self._json(Path("public/research/evidence_packets") / f"{packet_id}.json")
                        if packet is not None:
                            packets.setdefault(packet_id, packet)
        for packet in packets.values():
            for source in packet.get("sources", []):
                if not isinstance(source, dict) or not isinstance(source.get("source_id"), str):
                    continue
                sid = source["source_id"]
                if sid not in self.sources or (not self.sources[sid].get("archived_path") and source.get("archived_path")):
                    self.sources[sid] = source
                self._add_alias(sid, sid)
                entries = self.findings.setdefault(sid, [])
                for group in FINDING_GROUPS:
                    for finding in packet.get(group, []):
                        if not isinstance(finding, dict) or finding.get("source_id") != sid:
                            continue
                        # Keep the complete finding, its direction and claim context.
                        item = {key: finding.get(key) for key in (
                            "evidence_summary", "applicability", "limitations")}
                        item.update(
                            direction=finding.get("direction"), category=group,
                            claim=packet.get("normalized_claim"),
                            knowledge_status=packet.get("knowledge_status"),
                            consensus=packet.get("consensus"),
                            unresolved_questions=packet.get("unresolved_questions", []),
                        )
                        if not self.writer:
                            item["packet_id"] = packet["packet_id"]
                        if item not in entries:
                            entries.append(item)
        for source in catalogs:
            if not isinstance(source, dict):
                continue
            sid, citation = source.get("source_id"), source.get("citation_id")
            if sid not in self.sources or not isinstance(citation, str):
                continue
            self._add_alias(citation, sid)
            match = re.fullmatch(r"C(\d+)-(\d+)", citation)
            if match:
                self._add_alias(f"C{int(match[1])}-{int(match[2])}", sid)
        self.digest = hashlib.sha256(json.dumps(
            self.provenance, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()

    def _add_alias(self, alias, sid):
        if alias in self.ambiguous_aliases:
            return
        if alias in self.aliases and self.aliases[alias] != sid:
            self.aliases.pop(alias)
            self.ambiguous_aliases.add(alias)
        else:
            self.aliases[alias] = sid

    def _json(self, relative):
        try:
            path = public_file(self.root, relative)
            content = path.read_bytes()
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("not a public object")
            self.provenance[str(relative)] = hashlib.sha256(content).hexdigest()
            return data
        except (OSError, ValueError):
            self.errors.append(str(relative))
            return None

    def index(self) -> dict:
        entries = []
        for sid, source in sorted(self.sources.items()):
            citations = sorted(alias for alias, target in self.aliases.items()
                               if target == sid and re.fullmatch(r"C\d+-\d+", alias))
            # Prefer the frozen zero-padded ID; numeric aliases remain resolvable.
            handle = max(citations, key=len) if citations else sid
            if self.writer and not citations:
                continue
            entries.append({
                "source_id": handle, "title": str(source.get("title") or "")[:100],
                "original_archived": (
                    any(Path(str(source.get("archived_path") or "")).is_relative_to(prefix)
                        for prefix in PUBLIC_ORIGINAL_ROOTS)
                    and bool(source.get("archived_sha256"))
                ),
            })
        return {
            "sources": entries, "source_count": len(entries),
            "scope": "CURRENT_PUBLIC_DOSSIER_OR_RELEASED_SNAPSHOTS_ONLY",
            "not_shown_does_not_mean_absent": True,
        }

    def read(self, request: EvidenceReadRequest) -> dict:
        sid = self.aliases.get(request.source_id)
        if sid is None or (self.writer and not re.fullmatch(r"C\d+-\d+", request.source_id)):
            return {"source_id": request.source_id, "status": "UNKNOWN_OR_UNAUTHORIZED_SOURCE",
                    "note": "No lookup outside this public source registry; not proof that the source does not exist."}
        source = self.sources[sid]
        metadata = {key: source.get(key) for key in (
            "title", "authors", "publication_year", "doi", "url", "evidence_use_class",
            "archive_status", "access_basis",
        )}
        result = {"source_id": request.source_id, "view": request.view, "source": metadata}
        if request.view == "findings":
            findings = self.findings.get(sid, [])
            selected, cursor, used = [], request.cursor, 0
            while cursor < len(findings):
                item = findings[cursor]
                size = len(json.dumps(item, ensure_ascii=False))
                if selected and used + size > request.max_characters:
                    break
                selected.append(item)
                used += size
                cursor += 1
            result.update(
                status="READABLE_FINDINGS" if selected or cursor == len(findings) else "FINDING_TOO_LARGE_TO_INLINE",
                findings=selected, start_cursor=request.cursor, next_cursor=cursor,
                finding_count=len(findings), has_more=cursor < len(findings),
                basis="RESEARCH_DESK_FINDINGS; NOT_ORIGINAL_SOURCE_TEXT",
                single_finding_exceeds_requested_window=(len(selected) == 1 and used > request.max_characters),
            )
            if request.cursor > len(findings):
                result.update(status="INVALID_CURSOR", has_more=False)
            return result
        try:
            relative = Path(str(source.get("archived_path") or ""))
            expected = source.get("archived_sha256")
            if not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
                result.update(status="NO_VERIFIED_PUBLIC_ORIGINAL", original_available=False)
                return result
            path = public_file(self.root, relative, prefixes=PUBLIC_ORIGINAL_ROOTS)
            key = (str(relative), expected)
            if key not in self.original_cache:
                if path.stat().st_size > 50_000_000:
                    raise ValueError("original exceeds safe extraction size")
                content = path.read_bytes()
                if hashlib.sha256(content).hexdigest() != expected:
                    raise ValueError("original digest mismatch")
                # Cache at most two decoded originals per request. Independent
                # model lanes must not each retain every PDF they requested.
                if len(self.original_cache) >= 2:
                    self.original_cache.pop(next(iter(self.original_cache)))
                if content.startswith(b"%PDF-"):
                    from pypdf import PdfReader
                    from project_ensemble.research.pdf_warnings import capture_recoverable_pdf_warnings
                    with capture_recoverable_pdf_warnings():
                        document = PdfReader(io.BytesIO(content))
                    self.original_cache[key] = ("pdf", document)
                elif path.suffix.lower() in {".txt", ".md", ".html", ".htm", ".json", ".xml"}:
                    text = content.decode("utf-8")
                    if path.suffix.lower() in {".html", ".htm"}:
                        from project_ensemble.research.source_reading import _VisibleText
                        parser = _VisibleText()
                        parser.feed(text)
                        text = "\n".join(parser.parts)
                    self.original_cache[key] = ("text", text)
                else:
                    raise ValueError("unsupported public original format")
            kind, document = self.original_cache[key]
            page_count = len(document.pages) if kind == "pdf" else 1
            if request.page >= page_count:
                result.update(status="INVALID_PAGE", page_count=page_count)
                return result
            if kind == "pdf":
                from project_ensemble.research.pdf_warnings import capture_recoverable_pdf_warnings
                with capture_recoverable_pdf_warnings():
                    text = document.pages[request.page].extract_text() or ""
            else:
                text = document
            if not text.strip():
                result.update(status="NO_EXTRACTABLE_TEXT", page=request.page, page_count=page_count,
                              note="This page was not read; image-only pages need OCR, not invented text.")
                return result
            if request.cursor > len(text):
                result.update(status="INVALID_CURSOR")
                return result
            for span in MATH_SPANS.finditer(text):
                if span.start() < request.cursor < span.end():
                    result.update(status="CURSOR_INSIDE_MATH_BLOCK", suggested_cursor=span.start(),
                                  note="Resume at a complete formula boundary; no partial formula was read.")
                    return result
            end = _safe_end(text, request.cursor, request.max_characters)
            if end - request.cursor > request.max_characters + 2000:
                result.update(status="MATH_BLOCK_TOO_LARGE_TO_INLINE", original_available=True)
                return result
            result.update(
                status="READABLE_ORIGINAL_FRAGMENT", text=text[request.cursor:end],
                content_sha256=expected, page=request.page, page_count=page_count,
                start_cursor=request.cursor, next_cursor=end,
                page_character_count=len(text), page_has_more=end < len(text),
                next_page=request.page + 1 if end == len(text) and request.page + 1 < page_count else None,
                extracted_text_complete_in_this_result=(
                    request.page == 0 and request.cursor == 0 and end == len(text) and page_count == 1),
                document_complete_in_this_result=(
                    kind == "text" and path.suffix.lower() not in {".html", ".htm"}
                    and request.cursor == 0 and end == len(text)),
                nontext_content_not_shown=(kind == "pdf" or path.suffix.lower() in {".html", ".htm"}),
                extraction_note=(
                    "PDF text extraction only: images, plots and formula layout were not viewed or verified."
                    if kind == "pdf" else "Text/visible HTML only; no visual rendering or embedded images were read."),
                basis="VERIFIED_LOCAL_ORIGINAL; FRAGMENT_ONLY_UNLESS_EXPLICITLY_COMPLETE",
            )
        except Exception as exc:
            # Optional local extraction must not reject a draft or reopen a
            # meeting. KeyboardInterrupt/SystemExit still propagate.
            result.update(status="ORIGINAL_UNAVAILABLE_OR_UNAUTHORIZED", error_type=type(exc).__name__,
                          note="No arbitrary path/private record/network access; failure is not proof of absence.")
        return result


def module_scope(stage: str, payload: dict) -> list[str]:
    explicit = payload.get("module")
    if isinstance(explicit, dict) and re.fullmatch(r"RM-\d+", str(explicit.get("module_id"))):
        return [explicit["module_id"]]
    match = re.search(r"RM-\d+", stage)
    if match:
        return [match.group(0)]
    return sorted(set(re.findall(r"RM-\d+", json.dumps(payload, ensure_ascii=False))))
