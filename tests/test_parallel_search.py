"""Search cost boundaries, meeting-local engine choice and scholarly routing."""

import io
import json

import httpx
import pytest
from pydantic import ValidationError

from project_ensemble import cli
from project_ensemble.config import EnsembleConfig, ParallelResearchConfig, ProviderConfig, TavilyResearchConfig, load_config
from project_ensemble.domain import ReasoningEffort
from project_ensemble.errors import OpenAlexConnectionUnavailable, TransientProviderError
from project_ensemble.orchestration.literature_fast import FastBreadthSearchPlan
from project_ensemble.providers.parallel_search import ParallelRetriever
from project_ensemble.research.documents import DownloadedDocument
from project_ensemble.research.models import NormalizedClaim
from project_ensemble.research.openalex import OpenAlexRetriever
from project_ensemble.research.retrievers import CompositeRetriever, PolicyResearchRetriever, ResearchRetrievalResult, TavilyRetriever
from project_ensemble.research.search_policy import general_search_allowed, general_search_engine
from project_ensemble.research.source_reading import SourceReader
from project_ensemble.runtime.run_controls import effective_openalex_quota_policy, record_run_control
from project_ensemble.startup import TerminalWizard
from project_ensemble.storage.meeting import MeetingRepository


def _repo(tmp_path, engine="parallel", allowed=True):
    rules = tmp_path / "rules"
    rules.mkdir(exist_ok=True)
    (rules / "rule.md").write_text("test rules", encoding="utf-8")
    return MeetingRepository.create(
        tmp_path / engine, governance_docs=rules,
        selected_models=[("fake", "m")], chair_model=("fake", "m"),
        task_description="test", research_enabled=True, research_model=("fake", "m"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        academic_search_engine="openalex", general_search_engine=engine,
        general_search_allowed=allowed, openalex_quota_policy="wait",
    )


def _cfg():
    cfg = EnsembleConfig(providers={"fake": ProviderConfig(
        kind="openai_compatible", base_url="https://unused.invalid", api_key_env="UNUSED_TEST_KEY",
    )})
    cfg.research.parallel.enabled = True
    cfg.research.tavily.enabled = True
    return cfg


def _claim():
    return NormalizedClaim(
        is_researchable=True, normalized_claim="test", verification_question="test?",
        supporting_query="test support", contradictory_query="test contradiction",
        limitations_query="test limitations", alternatives_query="test alternatives",
        source_domain="GENERAL", source_domain_rationale="test",
        freshness_class="STABLE", freshness_rationale="test",
    )


@pytest.mark.parametrize("mode", ["fast", "turbo"])
@pytest.mark.parametrize("claim_check,count", [(False, 1), (True, 4)])
def test_parallel_sends_explicit_cheap_mode_and_server_side_ten_result_cap(monkeypatch, mode, claim_check, count):
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        assert str(request.url) == "https://api.parallel.ai/v1/search"
        assert request.headers["x-api-key"] == "fixture-secret"
        assert payload["mode"] == mode
        assert payload["advanced_settings"] == {"max_results": 10}
        assert payload["max_chars_total"] == 20000
        assert "max_results" not in payload
        requests.append(payload)
        return httpx.Response(200, json={
            "search_id": "search-fixture", "usage": [{"name": "search", "count": 1}],
            "warnings": [], "results": [
                {"url": "https://example.test/article", "title": "Article", "publish_date": "2025-01-02",
                 "excerpts": ["Search excerpt only."]},
                {"url": "file:///private", "title": "Invalid URL", "excerpts": []},
            ],
        })
    client_class = httpx.Client
    monkeypatch.setattr("project_ensemble.providers.parallel_search.httpx.Client",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(handler)))
    retriever = ParallelRetriever(api_key="fixture-secret", mode=mode)
    result = retriever.retrieve(_claim()) if claim_check else retriever.retrieve_exploratory("test query")
    assert len(requests) == count
    assert len(result.candidates) == 1
    assert result.candidates[0]["publication_year"] == 2025
    assert result.candidates[0]["retrieval_backend_id"] == "parallel"
    assert "source_read" not in result.candidates[0]
    assert len(result.query_trace) == count
    assert all(item["mode"] == mode and item["provider_request_id"] == "search-fixture" for item in result.query_trace)
    assert "fixture-secret" not in json.dumps(result.query_trace)
    if claim_check:
        assert result.candidates[0]["retrieval_purposes"] == ["supporting", "contradictory", "limitations", "alternatives"]


@pytest.mark.parametrize("mode", ["basic", "advanced", "", "FAST"])
def test_parallel_expensive_or_implicit_mode_is_rejected(mode):
    with pytest.raises(ValidationError):
        ParallelResearchConfig(mode=mode)
    with pytest.raises(ValueError):
        ParallelRetriever(api_key="fixture", mode=mode)


@pytest.mark.parametrize("limit", [0, 11, 20])
def test_parallel_cannot_accidentally_request_billable_extra_results(limit):
    with pytest.raises(ValidationError):
        ParallelResearchConfig(max_results_per_query=limit)
    with pytest.raises(ValueError):
        ParallelRetriever(api_key="fixture", max_results_per_query=limit)


@pytest.mark.parametrize("status", [429, 500])
def test_parallel_transport_failure_is_not_silently_treated_as_empty_search(monkeypatch, status):
    client_class = httpx.Client
    monkeypatch.setattr("project_ensemble.providers.parallel_search.httpx.Client",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(
                            lambda request: httpx.Response(status, json={"error": "fixture error"}))))
    with pytest.raises(TransientProviderError):
        ParallelRetriever(api_key="fixture").retrieve_exploratory("test")


def test_parallel_choice_is_frozen_and_never_reads_or_builds_tavily(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    originals = {(repo.root / path): (repo.root / path).read_bytes()
                 for path in ("public/meeting_manifest.json", "identity_private/meeting_manifest.json")}
    monkeypatch.setattr(TavilyResearchConfig, "api_key", lambda _: pytest.fail("Tavily key must not be read"))
    monkeypatch.setattr(TavilyRetriever, "__init__", lambda *a, **k: pytest.fail("Tavily must not be instantiated"))
    monkeypatch.setattr(ParallelResearchConfig, "api_key", lambda _: "fixture")
    retriever = cli._build_research_retriever(_cfg(), MeetingRepository(repo.root))
    assert isinstance(retriever, PolicyResearchRetriever)
    assert isinstance(retriever.tavily, ParallelRetriever)
    assert retriever.backend_ids == ("openalex", "parallel")
    reader = SourceReader(repo=repo, retriever=retriever)
    assert reader.tavily is None
    assert isinstance(reader.search_backend, ParallelRetriever)
    assert all(path.read_bytes() == data for path, data in originals.items())
    for data in originals.values():
        manifest = json.loads(data)
        assert manifest["academic_search_engine"] == "openalex"
        assert manifest["general_search_engine"] == "parallel"


def test_disabled_meeting_never_reads_either_paid_backend_key(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "disabled", False)
    monkeypatch.setattr(TavilyResearchConfig, "api_key", lambda _: pytest.fail("no Tavily credentials"))
    monkeypatch.setattr(ParallelResearchConfig, "api_key", lambda _: pytest.fail("no Parallel credentials"))
    assert isinstance(cli._build_research_retriever(_cfg(), repo), OpenAlexRetriever)


def test_runtime_engine_selection_is_meeting_local_append_only_and_resume_safe(tmp_path):
    repo = _repo(tmp_path)
    before = (repo.root / "public/meeting_manifest.json").read_bytes()
    record_run_control(repo, kind="openalex_quota_policy", target=None, value="parallel")
    assert effective_openalex_quota_policy(repo, "wait") == "parallel"
    record_run_control(repo, kind="general_search_engine", target=None, value="tavily")
    assert general_search_engine(MeetingRepository(repo.root)) == "tavily"
    assert effective_openalex_quota_policy(repo, "wait") == "wait"  # No cross-provider payment authorization.
    record_run_control(repo, kind="general_search_engine", target=None, value="disabled")
    assert general_search_allowed(repo) is False
    assert effective_openalex_quota_policy(repo, "parallel") == "wait"
    record_run_control(repo, kind="general_search_engine", target=None, value="parallel")
    assert general_search_engine(repo) == "parallel"
    assert (repo.root / "public/meeting_manifest.json").read_bytes() == before
    assert repo.events.verify()


def test_ctrl_r_can_select_parallel_and_disable_it_without_mutating_initial_manifest(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "tavily")
    before = (repo.root / "identity_private/meeting_manifest.json").read_bytes()
    monkeypatch.setattr(cli, "terminal_input", lambda _: "3")
    choice = cli._interactive_run_control(repo=repo, cfg=_cfg(), mode=9, deferred=True, output=io.StringIO())[0]
    assert choice["control_kind"] == "general_search_engine"
    assert choice["value"] == "parallel"
    record_run_control(repo, kind=choice["control_kind"], target=None, value=choice["value"])
    monkeypatch.setattr(cli, "terminal_input", lambda _: "2")
    choice = cli._interactive_run_control(repo=repo, cfg=_cfg(), mode=9, deferred=True, output=io.StringIO())[0]
    record_run_control(repo, kind=choice["control_kind"], target=None, value=choice["value"])
    assert general_search_engine(repo) == "disabled"
    assert (repo.root / "identity_private/meeting_manifest.json").read_bytes() == before


@pytest.mark.parametrize("answer,engine", [("", "disabled"), ("1", "tavily"), ("2", "disabled"), ("3", "parallel")])
def test_new_meeting_general_search_menu_defaults_to_disabled_and_selects_one_backend(answer, engine):
    wizard = TerminalWizard(input_fn=lambda _: answer, output=io.StringIO())
    assert wizard._choose_general_search_engine(_cfg()) == engine


def test_academic_engine_menu_is_explicit_and_fast_plans_default_to_academic():
    wizard = TerminalWizard(input_fn=lambda _: "", output=io.StringIO())
    assert wizard._choose_academic_search_engine() == "openalex"
    assert FastBreadthSearchPlan(question="test", search_queries=["test"]).source_domain.value == "ACADEMIC"


def test_scholarly_recovery_does_not_bill_web_search_even_without_a_doi(tmp_path):
    class Academic:
        backend_ids = ("openalex",)
        def __init__(self): self.calls = []
        def retrieve_exploratory(self, query):
            self.calls.append(query)
            return ResearchRetrievalResult([], [], self.backend_ids)
    class Web:
        backend_ids = ("parallel",)
        def retrieve_exploratory(self, _):
            pytest.fail("academic original recovery must not call paid web search")
    academic = Academic()
    reader = SourceReader(repo=_repo(tmp_path), retriever=PolicyResearchRetriever(academic, Web()))
    reader.prepare([{
        "source_id": "https://openalex.org/W123", "title": "A theory of information and entropy",
        "doi": None, "full_text_url": None, "full_text_is_public": False, "source_type": "article",
    }], question="information entropy", request_key="test")
    assert len(academic.calls) == 2


def test_web_originals_are_read_directly_by_default_without_tavily_extract(tmp_path):
    class Fetcher:
        def fetch(self, url):
            return DownloadedDocument(content=b"Original page contents and scientific details.",
                                      media_type="text/plain", final_url=url)
    web = TavilyRetriever(api_key="fixture")
    web.extract_url = lambda _: pytest.fail("no automatic paid Extract")
    reader = SourceReader(repo=_repo(tmp_path, "tavily"), retriever=web, document_fetcher=Fetcher())
    prepared, _ = reader.prepare([{
        "source_id": "https://example.test/article", "title": "Original article",
        "full_text_url": "https://example.test/article", "full_text_is_public": True, "source_type": "web_page",
    }], question="scientific details", request_key="test")
    assert prepared[0]["source_read"]["status"] == "READABLE_EXCERPT"
    assert prepared[0]["source_read"]["method"] == "DIRECT_ORIGINAL"


def test_parallel_is_excluded_from_explicit_academic_exploration():
    class Academic:
        backend_ids = ("openalex",)
        def retrieve_exploratory(self, query):
            return ResearchRetrievalResult([], [], self.backend_ids)
    class Web:
        backend_ids = ("parallel",)
        def retrieve_exploratory(self, _): pytest.fail("paid academic parallel search")
    retriever = CompositeRetriever([Academic(), Web()])
    assert retriever.retrieve_exploratory("test", academic_only=True).effective_backend_ids == ("openalex",)


def test_parallel_credential_file_is_resolved_without_copying_key_into_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_PARALLEL_KEY", raising=False)
    (tmp_path / "parallel.sh").write_text("MY_PARALLEL_KEY='fixture-secret'\n", encoding="utf-8")
    path = tmp_path / "ensemble.toml"
    path.write_text('[research.parallel]\nenabled = true\napi_key_env = "MY_PARALLEL_KEY"\napi_key_file = "./parallel.sh"\nmode = "turbo"\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.research.parallel.api_key_file == str(tmp_path / "parallel.sh")
    assert cfg.research.parallel.api_key() == "fixture-secret"
    assert "fixture-secret" not in path.read_text()
