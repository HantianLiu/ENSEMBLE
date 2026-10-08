"""Bounded, auditable reading of selected original sources before synthesis.

Search metadata and snippets are discovery aids. A successful read here means
that relevant text was extracted from a selected URL or local original; it does
not mean every page of that document was reviewed or that the source is correct.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from project_ensemble.research.documents import (
    DocumentFetcher, HttpDocumentFetcher, is_access_blocked_document,
)
from project_ensemble.research.pdf_warnings import capture_recoverable_pdf_warnings
from project_ensemble.research.retrievers import TavilyRetriever, coerce_retrieval_result
from project_ensemble.research.search_policy import general_search_allowed, general_search_engine
from project_ensemble.research.institutional_access import (
    PRIVATE_ROOT, fetch_institutional_original, institutional_access_allowed,
    original_locations, scholarly_candidate, publisher_body_text,
)
from project_ensemble.storage.meeting import MeetingRepository


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        elif tag in {"p", "br", "li", "h1", "h2", "h3", "tr", "td"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _terms(query: str) -> set[str]:
    words = {word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|\d+", query)}
    for span in re.findall(r"[\u3400-\u9fff]{2,}", query):
        words.update(span[index:index + 2] for index in range(len(span) - 1))
    return words


def _score(text: str, terms: set[str]) -> int:
    haystack = text.casefold()
    return sum(1 for term in terms if term in haystack)


def _excerpt(text: str, terms: set[str], *, limit: int = 5000) -> str:
    cleaned = re.sub(r"[ \t]+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    width = max(800, limit // 2)
    windows = [cleaned[start:start + width] for start in range(0, len(cleaned), width)]
    ranked = sorted(
        enumerate(windows), key=lambda item: (-_score(item[1], terms), item[0])
    )[:2]
    return "\n[…省略其他段落…]\n".join(windows[index] for index, _ in sorted(ranked))[:limit]


class SourceReader:
    """Read at most a few promising originals and attach provenance excerpts."""

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        retriever,
        document_fetcher: DocumentFetcher | None = None,
        max_sources: int = 4,
    ) -> None:
        self.repo = repo
        self.fetcher = document_fetcher or HttpDocumentFetcher()
        self.max_sources = max_sources
        def leaf_backends(item):
            children = getattr(item, "retrievers", None)
            if children is None:
                yield item
            else:
                for child in children:
                    yield from leaf_backends(child)

        backends = tuple(leaf_backends(retriever))
        if not general_search_allowed(repo):
            backends = tuple(item for item in backends if not set(getattr(item, "backend_ids", ())) & {"tavily", "parallel"})
        selected_engine = general_search_engine(repo)
        self.tavily = next(
            (item for item in backends if isinstance(item, TavilyRetriever)
             and selected_engine == "tavily"), None
        )
        self.academic_backend = next(
            (item for item in backends if "openalex" in getattr(item, "backend_ids", ())), None
        )
        self.search_backend = next(
            (item for item in backends if selected_engine in getattr(item, "backend_ids", ())),
            self.academic_backend,
        )

    def prepare(
        self, candidates: list[dict], *, question: str, request_key: str
    ) -> tuple[list[dict], list[dict]]:
        """Read originals and seek bounded alternatives when an original is unavailable."""
        terms = _terms(question)
        allow_institutional = institutional_access_allowed(self.repo)
        eligible = [item for item in candidates if self._readable_location(item, institutional=allow_institutional)]
        eligible.sort(key=lambda item: (
            -self._candidate_score(item, terms), str(item["source_id"])
        ))
        selected = {str(item["source_id"]) for item in eligible[:self.max_sources]}
        enriched: list[dict] = []
        ledger: list[dict] = []
        for candidate in candidates:
            item = dict(candidate)
            source_id = str(item["source_id"])
            if source_id in selected:
                record = self._read(item, terms, request_key=request_key, institutional=allow_institutional)
                item["source_read"] = {
                    key: record[key]
                    for key in ("status", "method", "excerpts", "record_path", "access_basis",
                                "private_original_path", "content_sha256", "media_type", "resolved_url")
                    if key in record
                }
                ledger.append({
                    "source_id": source_id,
                    "status": record["status"],
                    "method": record["method"],
                    "record_path": record["record_path"],
                })
            enriched.append(item)
        known = {str(item["source_id"]): item for item in enriched}
        unavailable = [
            item for item in enriched
            if not item.get("local_archive_path") and (
                (item.get("source_read") or {}).get("status")
                in {"ACCESS_BLOCKED", "UNREADABLE"}
                or not self._readable_location(item, institutional=allow_institutional)
            )
        ]
        unavailable.sort(key=lambda item: (
            0 if (item.get("source_read") or {}).get("status") == "ACCESS_BLOCKED" else 1,
            -self._candidate_score(item, terms), str(item["source_id"])
        ))
        for item in unavailable[:2]:
            alternatives, search_record = self._alternative_search(
                item, question=question, request_key=request_key,
            )
            if search_record:
                ledger.append(search_record)
            for alternative in alternatives:
                source_id = str(alternative["source_id"])
                if source_id == str(item["source_id"]):
                    continue
                if source_id in known and (known[source_id].get("source_read") or {}).get("status") == "READABLE_EXCERPT":
                    continue
                prepared = known.get(source_id) or dict(alternative)
                prepared.setdefault("alternate_for_source_id", item["source_id"])
                prepared.setdefault("source_identity_status", "UNVERIFIED_ALTERNATIVE")
                record = self._read(prepared, terms, request_key=request_key, institutional=allow_institutional)
                prepared["source_read"] = {
                    key: record[key]
                    for key in ("status", "method", "excerpts", "record_path", "access_basis",
                                "private_original_path", "content_sha256", "media_type", "resolved_url")
                    if key in record
                }
                ledger.append({
                    "source_id": source_id,
                    "status": record["status"],
                    "method": record["method"],
                    "record_path": record["record_path"],
                    "alternate_for_source_id": item["source_id"],
                })
                if source_id not in known:
                    known[source_id] = prepared
                    enriched.append(prepared)
        return enriched, ledger

    @staticmethod
    def _recovery_queries(candidate: dict, question: str) -> list[str]:
        """Use document identity, never a hard-coded website or document class."""
        doi = str(candidate.get("doi") or "").strip()
        title = re.sub(r"\s+", " ", str(candidate.get("title") or "")).strip()
        # Search-result titles often prepend the publishing site's name.
        if "::" in title:
            title = title.rsplit("::", 1)[-1].strip()
        title = title[:150]
        if len(_terms(title)) < 2:
            path = urlparse(str(candidate.get("url") or candidate.get("full_text_url") or "")).path
            slug = re.sub(r"[-_]+", " ", Path(path).stem).strip()
            if len(_terms(slug)) >= 2:
                title = slug[:150]
        if doi:
            identity = f'"{doi}"'
        elif len(_terms(title)) >= 2:
            parts = re.split(r"\s+[-–—]{2,}\s+", title, maxsplit=1)
            identity = " ".join(f'"{part[:85]}"' for part in parts if part)
        else:
            return []
        return [f"{identity} full text PDF", f"{identity} original publication alternative copy"]

    @staticmethod
    def _plausible_alternative(original: dict, candidate: dict) -> bool:
        original_url = str(original.get("full_text_url") or original.get("url") or "")
        url = str(candidate.get("full_text_url") or "")
        if not url or url.rstrip("/") == original_url.rstrip("/"):
            return False
        if not candidate.get("full_text_is_public"):
            return False
        original_doi = str(original.get("doi") or "").casefold().strip()
        candidate_text = " ".join(str(candidate.get(key) or "") for key in ("title", "abstract", "url")).casefold()
        if original_doi and original_doi in candidate_text:
            return True
        title = str(original.get("title") or "")
        if "::" in title:
            title = title.rsplit("::", 1)[-1]
        identity_terms = {term for term in _terms(title) if len(term) >= 3 or term.isdigit()}
        if not identity_terms:
            return False
        overlap = sum(term in candidate_text for term in identity_terms)
        return overlap >= min(2, len(identity_terms)) and overlap / len(identity_terms) >= 0.3

    def _alternative_search(
        self, unavailable: dict, *, question: str, request_key: str
    ) -> tuple[list[dict], dict | None]:
        # Recover scholarly originals through scholarly metadata, not through
        # automatically billed web discovery merely because a publisher blocks access.
        scholarly = (unavailable.get("retrieval_backend_id") == "openalex"
                     or str(unavailable.get("source_id", "")).startswith("https://openalex.org/")
                     or bool(unavailable.get("doi")))
        search_backend = self.academic_backend if scholarly else self.search_backend
        if search_backend is None:
            return [], None
        queries = self._recovery_queries(unavailable, question)
        if not queries:
            return [], None
        digest = hashlib.sha256(
            f"{request_key}\0{unavailable['source_id']}\0alternative-search-v1".encode("utf-8")
        ).hexdigest()
        relative = Path("audit_private/research/source_reads") / f"alternative-{digest}.json"
        path = self.repo.root / relative
        if path.is_file():
            saved = json.loads(path.read_text(encoding="utf-8"))
            return saved.get("accepted_candidates", []), {
                "stage": "ALTERNATIVE_SOURCE_SEARCH",
                "unavailable_source_id": unavailable["source_id"],
                "status": saved["status"],
                "record_path": str(relative),
            }
        record = {
            "stage": "ALTERNATIVE_SOURCE_SEARCH",
            "unavailable_source_id": unavailable["source_id"],
            "unavailable_status": (unavailable.get("source_read") or {}).get("status", "NO_READABLE_LOCATION"),
            "queries": queries,
            "backend_ids": list(search_backend.backend_ids),
            "status": "NO_MATCH",
            "accepted_candidates": [],
            "rejected_source_ids": [],
            "candidate_screening": [],
            "search_errors": [],
            "record_path": str(relative),
        }
        eligible: dict[str, dict] = {}
        for query in queries:
            try:
                result = coerce_retrieval_result(
                    search_backend.retrieve_exploratory(query),
                    default_backend_ids=search_backend.backend_ids,
                )
            except Exception as exc:
                record["search_errors"].append(f"{type(exc).__name__}: {str(exc)[:300]}")
                continue
            for candidate in result.candidates:
                plausible = self._plausible_alternative(unavailable, candidate)
                record["candidate_screening"].append({
                    "query": query,
                    "source_id": candidate.get("source_id"),
                    "title": candidate.get("title"),
                    "url": candidate.get("full_text_url"),
                    "decision": "PROVISIONAL_CANDIDATE" if plausible else "REJECTED_METADATA_MISMATCH",
                })
                if not plausible:
                    record["rejected_source_ids"].append(candidate.get("source_id"))
                    continue
                eligible.setdefault(str(candidate["source_id"]), candidate)
        for candidate in list(eligible.values())[:2]:
            alternative = dict(candidate)
            alternative["alternate_for_source_id"] = unavailable["source_id"]
            alternative["source_identity_status"] = "UNVERIFIED_ALTERNATIVE"
            alternative["retrieval_purposes"] = ["source_recovery"]
            record["accepted_candidates"].append(alternative)
        if record["accepted_candidates"]:
            record["status"] = "CANDIDATES_FOUND"
        elif record["search_errors"]:
            record["status"] = "SEARCH_DEGRADED"
        self.repo.docs.write_once(relative, json.dumps(record, ensure_ascii=False, indent=2))
        return record["accepted_candidates"], {
            "stage": "ALTERNATIVE_SOURCE_SEARCH",
            "unavailable_source_id": unavailable["source_id"],
            "status": record["status"],
            "record_path": str(relative),
        }

    def _readable_location(self, candidate: dict, *, institutional: bool | None = None) -> bool:
        if candidate.get("local_archive_path"):
            return True
        if institutional is None:
            institutional = institutional_access_allowed(self.repo)
        return bool(original_locations(candidate, institutional=institutional))

    @staticmethod
    def _candidate_score(candidate: dict, terms: set[str]) -> float:
        title = str(candidate.get("title") or "")
        abstract = str(candidate.get("abstract") or "")
        try:
            provider_score = float(candidate.get("relevance_score") or 0)
        except (ValueError, TypeError):
            provider_score = 0
        return 4 * _score(title, terms) + _score(abstract, terms) + 4 * provider_score

    def _read(self, candidate: dict, terms: set[str], *, request_key: str, institutional: bool | None = None) -> dict:
        source_id = str(candidate["source_id"])
        if institutional is None:
            institutional = institutional_access_allowed(self.repo)
        use_institutional = bool(institutional and scholarly_candidate(candidate)
                                 and not candidate.get("local_archive_path"))
        cache_version = "source-read-v2"
        if use_institutional:
            cache_version += "\0institutional-v1\0" + json.dumps(
                original_locations(candidate, institutional=True), sort_keys=True)
        digest = hashlib.sha256(
            f"{request_key}\0{source_id}\0{cache_version}".encode("utf-8")
        ).hexdigest()
        relative = Path("audit_private/research/source_reads") / f"{digest}.json"
        path = self.repo.root / relative
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        record = {
            "source_id": source_id,
            "question_terms": sorted(terms)[:80],
            "source_url": candidate.get("full_text_url") or candidate.get("local_archive_path"),
            "status": "UNREADABLE",
            "method": "NONE",
            "excerpts": [],
            "content_sha256": None,
            "media_type": None,
            "error": None,
            "read_at": datetime.now(timezone.utc).isoformat(),
            "record_path": str(relative),
        }
        try:
            local = candidate.get("local_archive_path")
            if local:
                relative_local = Path(str(local))
                target = (self.repo.root / relative_local).resolve()
                if (relative_local.is_absolute() or self.repo.root.resolve() not in target.parents
                        or not str(relative_local).startswith("public/human_references/files/")
                        or (self.repo.root / relative_local).is_symlink()):
                    raise ValueError("invalid meeting-local original path")
                content = target.read_bytes()
                if hashlib.sha256(content).hexdigest() != candidate.get("local_archive_sha256"):
                    raise ValueError("human original digest mismatch")
                media_type = "application/pdf" if target.suffix.lower() == ".pdf" else "text/plain"
                method = "HUMAN_ORIGINAL"
            elif use_institutional:
                original = fetch_institutional_original(self.repo, self.fetcher, candidate)
                record["institutional_attempt_record"] = original["record_path"]
                record["access_basis"] = original["access_basis"]
                if original["status"] != "FETCHED":
                    record["status"] = original["status"]
                    record["error"] = "未取得可读原文；访问失败或页面仅含元数据。机构尝试已缓存，不重复请求。"
                    self.repo.docs.write_once(relative, json.dumps(record, ensure_ascii=False, indent=2))
                    return record
                target = (self.repo.root / original["archived_path"]).resolve()
                if not target.is_relative_to((self.repo.root / PRIVATE_ROOT / "originals").resolve()):
                    raise ValueError("invalid institutional original path")
                content = target.read_bytes()
                if hashlib.sha256(content).hexdigest() != original["archived_sha256"]:
                    raise ValueError("institutional original hash mismatch")
                record["private_original_path"] = original["archived_path"]
                record["resolved_url"] = original["resolved_url"]
                media_type = original["media_type"]
                method = "INSTITUTIONAL_ORIGINAL" if original["access_basis"] == "INSTITUTIONAL_SUBSCRIPTION" else "DIRECT_ORIGINAL"
            else:
                url = str(candidate["full_text_url"])
                if (candidate.get("source_type") == "web_page" and self.tavily is not None
                        and self.tavily.extract_enabled
                        and candidate.get("retrieval_backend_id") != "openalex"
                        and not str(candidate.get("source_id", "")).startswith("https://openalex.org/")
                        and not candidate.get("doi")
                        and not urlparse(url).path.lower().endswith(".pdf")):
                    try:
                        extracted = self.tavily.extract_url(url)
                    except Exception as exc:
                        record["tavily_extract_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                        downloaded = self.fetcher.fetch(url)
                        content = downloaded.content
                        media_type = downloaded.media_type
                        method = "DIRECT_ORIGINAL_AFTER_EXTRACT_FAILURE"
                        record["resolved_url"] = downloaded.final_url
                    else:
                        content = extracted["raw_content"].encode("utf-8")
                        media_type = "text/markdown"
                        method = "TAVILY_EXTRACT"
                        record["tavily_request_id"] = extracted.get("request_id")
                        record["tavily_usage"] = extracted.get("usage")
                else:
                    downloaded = self.fetcher.fetch(url)
                    content = downloaded.content
                    media_type = downloaded.media_type
                    method = "DIRECT_ORIGINAL"
                    record["resolved_url"] = downloaded.final_url
            record["content_sha256"] = hashlib.sha256(content).hexdigest()
            record["media_type"] = media_type
            record["method"] = method
            if is_access_blocked_document(
                content, media_type=media_type,
                final_url=record.get("resolved_url"),
            ):
                record["status"] = "ACCESS_BLOCKED"
                record["error"] = "访问限制页，不是来源正文。"
                self.repo.docs.write_once(
                    relative, json.dumps(record, ensure_ascii=False, indent=2)
                )
                return record
            if media_type == "application/pdf" or content.startswith(b"%PDF-"):
                from pypdf import PdfReader

                pages = []
                page_errors = []
                with capture_recoverable_pdf_warnings() as pdf_warnings:
                    reader = PdfReader(io.BytesIO(content))
                    for index in range(min(len(reader.pages), 300)):
                        try:
                            text = reader.pages[index].extract_text() or ""
                        except Exception as exc:
                            if len(page_errors) < 5:
                                page_errors.append({
                                    "page": index + 1,
                                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                                })
                            continue
                        if text.strip():
                            pages.append((index + 1, text[:20000]))
                record["pdf_duplicate_length_warning_count"] = pdf_warnings.duplicate_length_count
                if pdf_warnings.first_duplicate_length:
                    record["pdf_first_duplicate_length_warning"] = pdf_warnings.first_duplicate_length
                record["pdf_broken_cmap_line_count"] = pdf_warnings.broken_cmap_line_count
                if pdf_warnings.first_broken_cmap_line:
                    record["pdf_first_broken_cmap_line"] = pdf_warnings.first_broken_cmap_line
                record["pdf_wrong_pointing_object_warning_count"] = (
                    pdf_warnings.wrong_pointing_object_count
                )
                if pdf_warnings.first_wrong_pointing_object:
                    record["pdf_first_wrong_pointing_object_warning"] = (
                        pdf_warnings.first_wrong_pointing_object
                    )
                if page_errors:
                    record["pdf_page_read_errors"] = page_errors
                pages.sort(key=lambda item: (-_score(item[1], terms), item[0]))
                record["excerpts"] = [
                    {"locator": f"PDF 第 {index} 页", "text": _excerpt(text, terms, limit=2500)}
                    for index, text in pages[:2]
                ]
                record["extraction_note"] = "PDF 文本抽取可能遗漏公式、表格或扫描图像。"
                if (pdf_warnings.duplicate_length_count
                        or pdf_warnings.broken_cmap_line_count
                        or pdf_warnings.wrong_pointing_object_count or page_errors):
                    record["extraction_note"] += " PDF 结构异常；重要论断须复核原页。"
            else:
                text = content.decode("utf-8-sig", errors="replace")
                if media_type in {"text/html", "application/xhtml+xml"}:
                    if use_institutional:
                        text = publisher_body_text(text)
                    else:
                        parser = _VisibleText()
                        parser.feed(text)
                        text = "".join(parser.parts)
                excerpt = _excerpt(text[:250000], terms)
                if excerpt:
                    record["excerpts"] = [{"locator": "正文摘录；网页未提供页码", "text": excerpt}]
            if record["excerpts"]:
                record["status"] = "READABLE_EXCERPT"
            else:
                record["error"] = "原件没有可抽取的文字；扫描件可能需要 OCR。"
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        self.repo.docs.write_once(relative, json.dumps(record, ensure_ascii=False, indent=2))
        return record
