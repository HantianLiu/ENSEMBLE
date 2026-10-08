import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from project_ensemble import cli
from project_ensemble.research.documents import DownloadedDocument, LiteratureBundleManager
from project_ensemble.research.institutional_access import (
    fetch_institutional_original, institutional_access_allowed, publisher_body_text,
)
from project_ensemble.research.models import EvidenceSource
from project_ensemble.research.openalex import OpenAlexRetriever
from project_ensemble.research.source_reading import SourceReader
from project_ensemble.runtime.run_controls import record_run_control
from project_ensemble.startup import TerminalWizard
from project_ensemble.storage.meeting import MeetingRepository


def _repo(tmp_path, allowed=False):
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rule.md").write_text("test")
    return MeetingRepository.create(tmp_path / "meeting", selected_models=[("fake", "m")],
                                    chair_model=("fake", "m"), governance_docs=governance,
                                    institutional_access_allowed=allowed)


def _candidate():
    return OpenAlexRetriever._candidate({
        "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/test",
        "display_name": "Institutional source", "type": "article",
        "open_access": {"is_oa": False},
        "primary_location": {"landing_page_url": "https://publisher.test/paper"}})


class Fetcher:
    def __init__(self, documents):
        self.documents, self.calls = documents, []
    def fetch(self, url):
        self.calls.append(url)
        return self.documents[url]


class NoSearch:
    backend_ids = ()


def _html():
    return ('<html><head><meta name="citation_doi" content="10.1/test"></head>'
            '<body><div class="article-body">' + "A readable scientific result and its limitations. " * 50
            + '</div></body></html>').encode()


def test_authorization_defaults_off_and_runtime_is_append_only(tmp_path):
    repo = _repo(tmp_path)
    before = (repo.root / "public/meeting_manifest.json").read_bytes()
    assert not institutional_access_allowed(repo)
    record_run_control(repo, kind="institutional_access_allowed", target=None, value=True)
    assert institutional_access_allowed(repo)
    assert (repo.root / "public/meeting_manifest.json").read_bytes() == before
    record_run_control(repo, kind="institutional_access_allowed", target=None, value=False)
    assert not institutional_access_allowed(repo)


def test_legacy_candidate_retains_oa_fact_and_publisher_location():
    source = _candidate()
    assert not source["full_text_is_public"]
    assert source["full_text_url"] is None
    assert source["institutional_full_text_urls"][0] == "https://publisher.test/paper"


def test_no_institutional_network_without_explicit_permission(tmp_path):
    repo = _repo(tmp_path)
    fetcher = Fetcher({})
    reader = SourceReader(repo=repo, retriever=NoSearch(), document_fetcher=fetcher)
    reader.prepare([_candidate()], question="scientific result", request_key="one")
    assert not fetcher.calls
    with pytest.raises(ValueError, match="not authorized"):
        fetch_institutional_original(repo, fetcher, _candidate())


def test_reuses_original_across_questions_and_excludes_from_public_zip(tmp_path):
    repo = _repo(tmp_path, True)
    url = "https://publisher.test/paper"
    fetcher = Fetcher({url: DownloadedDocument(_html(), "text/html", url)})
    reader = SourceReader(repo=repo, retriever=NoSearch(), document_fetcher=fetcher)
    first, _ = reader.prepare([_candidate()], question="scientific result", request_key="one")
    second, _ = reader.prepare([_candidate()], question="limitations", request_key="two")
    assert fetcher.calls == [url]
    reading = first[0]["source_read"]
    assert reading["status"] == "READABLE_EXCERPT"
    assert reading["access_basis"] == "INSTITUTIONAL_SUBSCRIPTION"
    assert second[0]["source_read"]["method"] == "INSTITUTIONAL_ORIGINAL"
    assert not first[0]["full_text_is_public"]
    source = EvidenceSource(source_id=_candidate()["source_id"], title="Institutional source",
                            url=url, evidence_use_class="PRIMARY")
    manager = LiteratureBundleManager(repo=repo, fetcher=fetcher)
    archived = manager.archive_sources(packet_id="P1", sources=[source], candidates=first)[0]
    assert archived.archive_status.value == "ARCHIVED"
    assert archived.evidence_use_class.value == "PRIMARY"
    assert archived.archived_path.startswith("human_private/institutional_documents/")
    assert archived.access_basis == "INSTITUTIONAL_SUBSCRIPTION"
    archive = manager.rebuild_download_bundle(export_zip=True)
    with zipfile.ZipFile(repo.root / archive) as bundle:
        assert all(not name.endswith((".pdf", ".html")) for name in bundle.namelist())
    assert fetcher.calls == [url]


def test_abstract_only_and_failures_are_not_readable_and_not_retried(tmp_path):
    repo = _repo(tmp_path, True)
    candidate = _candidate()
    candidate["doi"] = None
    candidate["institutional_full_text_urls"] = [candidate["url"]]
    url = candidate["url"]
    fetcher = Fetcher({url: DownloadedDocument(
        b"<html><title>Scientific abstract</title><p>Only an abstract.</p></html>", "text/html", url)})
    reader = SourceReader(repo=repo, retriever=NoSearch(), document_fetcher=fetcher)
    for request in ("one", "two"):
        prepared, _ = reader.prepare([candidate], question="result", request_key=request)
        assert prepared[0]["source_read"]["status"] == "UNREADABLE"
        assert prepared[0]["source_read"]["excerpts"] == []
    assert fetcher.calls == [url]


def test_blocked_page_not_followed_and_no_paid_extract(tmp_path):
    repo = _repo(tmp_path, True)
    candidate = _candidate()
    url = candidate["url"]
    candidate["institutional_full_text_urls"] = [url]
    candidate["doi"] = None
    html = b'<html><title>Sign in</title><meta name="citation_pdf_url" content="/file.pdf"></html>'
    fetcher = Fetcher({url: DownloadedDocument(html, "text/html", url)})
    prepared, _ = SourceReader(repo=repo, retriever=NoSearch(), document_fetcher=fetcher).prepare(
        [candidate], question="source", request_key="one")
    assert prepared[0]["source_read"]["status"] == "ACCESS_BLOCKED"
    assert fetcher.calls == [url]


def test_known_pdf_links_are_bounded_and_external_links_not_followed(tmp_path):
    repo = _repo(tmp_path, True)
    candidate = _candidate()
    url = candidate["url"]
    candidate["doi"] = None
    candidate["institutional_full_text_urls"] = [url]
    page = ('<meta name="citation_pdf_url" content="https://broker.test/leak.pdf">'
            '<a type="application/pdf" href="/original.pdf">Original</a>').encode()
    fetcher = Fetcher({url: DownloadedDocument(page, "text/html", url),
                       "https://publisher.test/original.pdf": DownloadedDocument(
                           b"%PDF-fixture", "application/pdf", "https://publisher.test/original.pdf")})
    result = fetch_institutional_original(repo, fetcher, candidate)
    assert result["status"] == "FETCHED"
    assert fetcher.calls == [url, "https://publisher.test/original.pdf"]


def test_new_grant_does_not_replay_old_failure_record(tmp_path):
    repo = _repo(tmp_path)
    candidate = _candidate()
    candidate["full_text_url"] = candidate["url"]
    # Deliberately no publicly readable URL: this produces no old read attempt.
    fetcher = Fetcher({candidate["url"]: DownloadedDocument(_html(), "text/html", candidate["url"])})
    reader = SourceReader(repo=repo, retriever=NoSearch(), document_fetcher=fetcher)
    assert not reader._readable_location(candidate)
    record_run_control(repo, kind="institutional_access_allowed", target=None, value=True)
    prepared, _ = reader.prepare([candidate], question="result", request_key="same")
    assert prepared[0]["source_read"]["status"] == "READABLE_EXCERPT"


def test_runtime_institutional_menu_can_enable_without_changing_search(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    choices = iter([0])
    monkeypatch.setattr(cli, "_replacement_menu_choice", lambda *args, **kwargs: next(choices))
    cli._interactive_run_control(repo=repo, cfg=None, mode=10, deferred=False, output=io.StringIO())
    assert institutional_access_allowed(repo)


def test_initialization_explicit_institutional_choice():
    wizard = TerminalWizard(input_fn=lambda _: "2", output=io.StringIO())
    assert wizard._choose_academic_search_engine() == "openalex"
    assert wizard._institutional_access_allowed is True


def test_html_body_extraction_handles_void_elements():
    assert publisher_body_text('<div class="article-body">One<br/>Two</div>') == "One\nTwo"
