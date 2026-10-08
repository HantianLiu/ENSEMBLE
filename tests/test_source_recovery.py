"""An inaccessible original triggers generic, auditable source discovery."""

import json
import logging
from io import StringIO

from project_ensemble.research.documents import DownloadedDocument, is_access_blocked_document
from project_ensemble.research.pdf_warnings import capture_recoverable_pdf_warnings
from project_ensemble.research.retrievers import PolicyResearchRetriever, ResearchRetrievalResult, TavilyRetriever
from project_ensemble.research.source_reading import SourceReader
from project_ensemble.storage.meeting import MeetingRepository


class RecoveryRetriever(TavilyRetriever):
    def __init__(self):
        super().__init__(api_key="fixture", extract_enabled=True)
        self.queries = []

    def retrieve_exploratory(self, query):
        self.queries.append(query)
        source = {
            "source_id": "https://repository.test/release-tests",
            "title": "21 CFR 610.1 — Tests prior to release",
            "abstract": "Tests prior to release, with a dated edition notice.",
            "url": "https://repository.test/release-tests",
            "full_text_url": "https://repository.test/release-tests",
            "full_text_is_public": True,
            "source_type": "web_page",
        }
        unrelated = dict(source, source_id="https://other.test/fruit",
                         title="Fruit prices", abstract="Apples and pears",
                         url="https://other.test/fruit", full_text_url="https://other.test/fruit")
        return ResearchRetrievalResult(
            candidates=[unrelated, source], query_trace=[],
            effective_backend_ids=("tavily",),
        )

    def extract_url(self, url):
        text = (
            "# Request Access\n" if url == "https://blocked.test/610-1"
            else "21 CFR 610.1, dated edition. Tests prior to release include the stated checks."
        )
        return {"url": url, "raw_content": text, "request_id": "fixture", "usage": {}}


def _repo(tmp_path):
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rule.md").write_text("fixture", encoding="utf-8")
    return MeetingRepository.create(
        tmp_path / "meeting", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs=governance,
    )


def test_source_reader_finds_tavily_inside_policy_retriever(tmp_path):
    class OpenAlex:
        backend_ids = ("openalex",)

    tavily = RecoveryRetriever()
    reader = SourceReader(
        repo=_repo(tmp_path),
        retriever=PolicyResearchRetriever(OpenAlex(), tavily),
    )
    assert reader.tavily is tavily


def test_access_block_page_is_not_document_text():
    assert is_access_blocked_document(
        b"<html><title>Request Access</title><body>Wait</body></html>",
        media_type="text/html", final_url="https://another-domain.test/a",
    )
    assert is_access_blocked_document(
        b"# Access Denied", media_type="text/markdown",
    )
    assert not is_access_blocked_document(
        b"A paper about access denied errors in computer networks.",
        media_type="text/plain",
    )


def test_repetitive_pdf_length_warning_is_counted_not_printed():
    logger = logging.getLogger("pypdf.generic._data_structures")
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    try:
        with capture_recoverable_pdf_warnings() as summary:
            logger.warning("Multiple definitions in dictionary at byte 0x74249 for key /Length")
            logger.warning("Other structural warning")
    finally:
        logger.removeHandler(handler)
    assert summary.duplicate_length_count == 1
    assert "Multiple definitions" not in stream.getvalue()
    assert "Other structural warning" in stream.getvalue()


def test_recoverable_pdf_cmap_warning_is_counted_not_printed():
    logger = logging.getLogger("pypdf._cmap")
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    try:
        with capture_recoverable_pdf_warnings() as summary:
            logger.warning("Skipping broken line b'14fd   14ff   10300': Odd-length string")
            logger.warning("A different PDF warning")
    finally:
        logger.removeHandler(handler)
    assert summary.broken_cmap_line_count == 1
    assert "Skipping broken line" not in stream.getvalue()
    assert "A different PDF warning" in stream.getvalue()


def test_recoverable_pdf_wrong_pointing_object_warning_is_counted_not_printed():
    logger = logging.getLogger("pypdf._reader")
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    try:
        with capture_recoverable_pdf_warnings() as summary:
            logger.warning("Ignoring wrong pointing object 17 0 (offset 0)")
            logger.warning("A different PDF reader warning")
    finally:
        logger.removeHandler(handler)
    assert summary.wrong_pointing_object_count == 1
    assert summary.first_wrong_pointing_object == "Ignoring wrong pointing object 17 0 (offset 0)"
    assert "Ignoring wrong pointing object" not in stream.getvalue()
    assert "A different PDF reader warning" in stream.getvalue()


def test_generic_alternative_search_recovers_blocked_original_without_site_mapping(tmp_path):
    repo = _repo(tmp_path)
    retriever = RecoveryRetriever()
    reader = SourceReader(repo=repo, retriever=retriever)
    original = {
        "source_id": "original", "title": "eCFR :: 21 CFR 610.1 -- Tests prior to release",
        "url": "https://blocked.test/610-1", "full_text_url": "https://blocked.test/610-1",
        "full_text_is_public": True, "source_type": "web_page",
    }
    prepared, ledger = reader.prepare([original], question="What are the release tests?", request_key="q1")
    assert prepared[0]["source_read"]["status"] == "ACCESS_BLOCKED"
    alternate = prepared[1]
    assert alternate["source_identity_status"] == "UNVERIFIED_ALTERNATIVE"
    assert alternate["alternate_for_source_id"] == "original"
    assert alternate["source_read"]["status"] == "READABLE_EXCERPT"
    assert len(retriever.queries) == 2
    assert all("site:" not in query for query in retriever.queries)
    assert all("21 CFR 610.1" in query for query in retriever.queries)
    record = json.loads((repo.root / next(entry["record_path"] for entry in ledger
                                      if entry.get("stage") == "ALTERNATIVE_SOURCE_SEARCH")).read_text())
    assert record["rejected_source_ids"] == ["https://other.test/fruit"] * 2
    assert record["status"] == "CANDIDATES_FOUND"
    reader.prepare([original], question="What are the release tests?", request_key="q1")
    assert len(retriever.queries) == 2


def test_metadata_only_paper_can_trigger_alternative_search(tmp_path):
    repo = _repo(tmp_path)
    retriever = RecoveryRetriever()
    reader = SourceReader(repo=repo, retriever=retriever)
    original = {
        "source_id": "metadata", "title": "21 CFR 610.1 -- Tests prior to release",
        "url": "https://catalog.test/record", "full_text_url": None,
        "full_text_is_public": False, "source_type": "web_page",
    }
    prepared, _ = reader.prepare([original], question="What are the release tests?", request_key="q2")
    assert len(prepared) == 2
    assert prepared[1]["source_read"]["status"] == "READABLE_EXCERPT"


def test_openalex_only_configuration_can_recover_an_unreadable_paper(tmp_path):
    class OpenAlexOnly:
        backend_ids = ("openalex",)

        def retrieve_exploratory(self, query):
            return ResearchRetrievalResult(
                candidates=[{
                    "source_id": "https://archive.test/paper.txt",
                    "title": "A Study of Interface Dynamics",
                    "abstract": "A study of interface dynamics with a revised text.",
                    "url": "https://archive.test/paper.txt",
                    "full_text_url": "https://archive.test/paper.txt",
                    "full_text_is_public": True,
                    "source_type": "journal_article",
                }], query_trace=[], effective_backend_ids=self.backend_ids,
            )

    class Fetcher:
        def fetch(self, url):
            return DownloadedDocument(
                content=b"The study measures interface dynamics under stated conditions.",
                media_type="text/plain", final_url=url,
            )

    reader = SourceReader(repo=_repo(tmp_path), retriever=OpenAlexOnly(), document_fetcher=Fetcher())
    original = {"source_id": "metadata", "title": "A Study of Interface Dynamics",
                "url": "https://catalog.test/record", "full_text_url": None,
                "full_text_is_public": False, "source_type": "journal_article"}
    prepared, _ = reader.prepare([original], question="Interface dynamics", request_key="openalex")
    assert prepared[1]["source_read"]["status"] == "READABLE_EXCERPT"
