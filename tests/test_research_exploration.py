"""Exploratory planning search is separate from claim-oriented Research Desk QC."""

import json
from pathlib import Path

from project_ensemble.domain import DeliverableType, GenerationResponse, ReasoningEffort
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.consultations import HumanConsultationService
from project_ensemble.orchestration.literature_report import LiteratureReportPlanningRunner
from project_ensemble.research.desk import ResearchDesk
from project_ensemble.research.documents import DownloadedDocument
from project_ensemble.research.exploration import ResearchExplorationService
from project_ensemble.research.retrievers import ResearchRetrievalResult
from project_ensemble.storage.meeting import MeetingRepository

from test_literature_report import NoopNotifier, PlanningAdapter


class DiscoveryRetriever:
    backend_ids = ("fixture",)

    def __init__(self):
        self.queries = []

    def retrieve_exploratory(self, query):
        self.queries.append(query)
        source = {
            "source_id": "SRC-1", "title": "A review", "authors": ["Author"],
            "publication_year": 2024, "doi": None,
            "url": "https://example.test/review", "source_type": "review",
            "abstract": "Broad landscape of the topic.", "full_text_url": None,
            "full_text_is_public": False,
        }
        return ResearchRetrievalResult(
            candidates=[source], query_trace=[{
                "backend_id": "fixture", "purpose": "exploratory", "query": query,
                "returned_source_ids": ["SRC-1"],
            }], effective_backend_ids=self.backend_ids,
        )


def test_exploratory_answer_reads_original_before_synthesis(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "report", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Survey a field.", research_enabled=True,
        research_model=("fake", "m"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
    )

    class OriginalRetriever(DiscoveryRetriever):
        def retrieve_exploratory(self, query):
            result = super().retrieve_exploratory(query)
            result.candidates[0]["full_text_url"] = "https://example.test/review.txt"
            result.candidates[0]["full_text_is_public"] = True
            return result

    class OriginalFetcher:
        def fetch(self, url):
            return DownloadedDocument(
                content=b"The reviewed method measures a defined endpoint under condition C.",
                media_type="text/plain", final_url=url,
            )

    adapter = ExplorationAdapter(repo)
    engine = MeetingEngine(
        repo=repo, adapters={"fake": adapter}, notifier=NoopNotifier(),
        configured_concurrency_limits={("fake", "m"): 1},
    )
    desk = ResearchDesk(
        repo=repo, engine=engine, retriever=OriginalRetriever(),
        document_fetcher=OriginalFetcher(),
    )
    answer = ResearchExplorationService(desk).answer(
        requester_id="CHAIR", cycle=1, turn=1, question_number=1,
        question="What method measures the endpoint under condition C?",
        search_queries=["method endpoint condition C"],
    )
    assert answer["retrieval_conditions"]["readable_original_excerpt_count"] == 1
    assert answer["source_catalog"][0]["source_read_status"] == "READABLE_EXCERPT"
    exchanges = list((repo.root / "governance_private/provider_exchanges").glob("X-*.json"))
    assert any(
        "The reviewed method measures" in path.read_text(encoding="utf-8")
        for path in exchanges
    )
    exploration_calls = [json.loads(path.read_text(encoding="utf-8")) for path in exchanges]
    exploration_calls = [item for item in exploration_calls
                         if item.get("stage", "").startswith("research_exploration_answer:")]
    assert exploration_calls
    assert all("结构化结果的自由文本表达规则" in item["request"]["system_text"]
               for item in exploration_calls)
    assert all("规划权限边界" in item["request"]["system_text"]
               for item in exploration_calls)


class ExplorationAdapter(PlanningAdapter):
    def __init__(self, repo):
        super().__init__()
        self.repo = repo
        self.decomposition_privacy_observed = []

    def generate(self, request):
        if "当前剩余检索额度：" in request.user_text:
            if "已获得的近期答复：\n[]" in request.user_text:
                question = "What literature maps this field?"
                if "不得把自己猜测的模块名称" in request.system_text:
                    question = "What review literature maps the original task?"
                payload = {"finished": False, "questions": [{
                    "question": question, "search_queries": ["review research landscape"],
                    "freshness_class": "VERSIONED",
                }]}
            else:
                payload = {"finished": True, "questions": []}
            return GenerationResponse(
                text=json.dumps(payload), provider_id="fake", model_id=request.model_id,
            )
        if "本任务是探索性文献地图" in request.system_text:
            return GenerationResponse(
                text="[SRC-1] gives an exploratory overview, not a verified verdict.",
                provider_id="fake", model_id=request.model_id,
            )
        if "在 framing_rationale 中解释" in request.user_text:
            chair_public = self.repo.root / "public/literature_report/exploration/C001/CHAIR/index.json"
            representative_public = self.repo.root / "public/literature_report/exploration/C001"
            self.decomposition_privacy_observed.append(
                (chair_public.is_file(), not any(representative_public.glob("R-*/index.json")))
            )
        return super().generate(request)


def test_planning_exploration_is_checkpointed_and_released_at_barrier(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "report", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Survey the research landscape.", research_enabled=True,
        research_model=("fake", "m"), research_reasoning_effort=ReasoningEffort.DEFAULT,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
    )
    assert json.loads(repo.docs.read_text("public/meeting_manifest.json"))[
        "planning_exploration_enabled"
    ] is True
    adapter = ExplorationAdapter(repo)
    engine = MeetingEngine(
        repo=repo, adapters={"fake": adapter}, notifier=NoopNotifier(),
        configured_concurrency_limits={("fake", "m"): 1},
    )
    retriever = DiscoveryRetriever()
    desk = ResearchDesk(repo=repo, engine=engine, retriever=retriever)
    runner = LiteratureReportPlanningRunner(
        repo=repo, engine=engine, governance_docs="docs/governance",
        exploration_service=ResearchExplorationService(desk),
    )
    first = runner.run()
    assert first.paused_reason == "HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED"
    assert adapter.decomposition_privacy_observed
    assert all(pair == (True, True) for pair in adapter.decomposition_privacy_observed), adapter.decomposition_privacy_observed
    public_root = repo.root / "public/literature_report/exploration/C001"
    assert len(list(public_root.glob("R-*/index.json"))) == 4
    assert (public_root / "CHAIR/index.json").is_file()
    # Chair and Representatives ask different questions; four identical
    # Representative questions reuse one meeting-local cached answer.
    assert retriever.queries == ["review research landscape"] * 2
    assert (repo.root / "public/research/literature_bundle/manifest.json").is_file()
    assert runner.run().paused_reason == first.paused_reason
    assert retriever.queries == ["review research landscape"] * 2

    HumanConsultationService(repo).resolve(
        issue_id="HC-LROUTLINE-C001", decision="REJECT_AND_REPLAN",
        rationale="第一版遗漏了边界条件，请重新划分。",
        scope="LITERATURE_OUTLINE_CYCLE",
    )
    assert runner.run().paused_reason == "HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED"
    exchanges = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (repo.root / "governance_private/provider_exchanges").glob("X-*.json")
    ]
    second_cycle_questions = [
        entry["request"]["user_text"] for entry in exchanges
        if entry["stage"].startswith("research_planning_exploration:C002:")
    ]
    assert second_cycle_questions
    assert all("第一版遗漏了边界条件" in prompt for prompt in second_cycle_questions)
    assert all("已否决的上一轮冻结总纲" in prompt for prompt in second_cycle_questions)


def test_old_manifest_without_exploration_flag_keeps_legacy_planning(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "report", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Survey the research landscape.", research_enabled=True,
        research_model=("fake", "m"), research_reasoning_effort=ReasoningEffort.DEFAULT,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
    )
    # Simulate a meeting created before this frozen private flag existed.
    manifest_path = repo.root / "identity_private/meeting_manifest.json"
    private_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    private_manifest.pop("planning_exploration_enabled")
    manifest_path.write_text(json.dumps(private_manifest), encoding="utf-8")
    engine = MeetingEngine(repo=repo, adapters={"fake": PlanningAdapter()}, notifier=NoopNotifier())
    result = LiteratureReportPlanningRunner(
        repo=repo, engine=engine, governance_docs="docs/governance",
        exploration_service=object(),
    ).run()
    assert result.paused_reason == "HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED"
    assert not (repo.root / "public/literature_report/exploration").exists()


def test_unsupported_exploratory_citation_is_marked_weak_not_blocking(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "report", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Survey a field.", research_enabled=True,
        research_model=("fake", "m"), research_reasoning_effort=ReasoningEffort.DEFAULT,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
    )

    class BadCitationAdapter(ExplorationAdapter):
        def generate(self, request):
            if "本任务是探索性文献地图" in request.system_text:
                return GenerationResponse(
                    text="A broad claim [NOT-IN-RETRIEVAL].",
                    provider_id="fake", model_id=request.model_id,
                )
            return super().generate(request)

    engine = MeetingEngine(
        repo=repo, adapters={"fake": BadCitationAdapter(repo)}, notifier=NoopNotifier(),
    )
    desk = ResearchDesk(repo=repo, engine=engine, retriever=DiscoveryRetriever())
    service = ResearchExplorationService(desk)
    answer = service.answer(
        requester_id="CHAIR", cycle=1, turn=1, question_number=1,
        question="What does the literature show?", search_queries=["field review"],
    )
    assert answer["status"] == "WEAK_EVIDENCE"
    assert "未在本次检索目录" in answer["answer_text"]
    public = service.publish([answer], public_prefix=Path(
        "public/literature_report/exploration/C001/CHAIR"
    ))
    assert public[0]["source_catalog"][0]["source_id"] == "SRC-1"
    audit = json.loads((repo.root / "audit_private/literature_report/exploration/"
                        "C001-CHAIR-T01-Q01.json").read_text(encoding="utf-8"))
    assert audit["unknown_citation_ids"] == ["NOT-IN-RETRIEVAL"]


def test_invalid_optional_exploration_question_does_not_stop_planning(tmp_path):
    class InvalidQuestionAdapter(PlanningAdapter):
        def generate(self, request):
            if (
                "当前剩余检索额度：" in request.user_text
                or ("TARGET JSON SCHEMA:" in request.user_text
                    and "search_queries" in request.user_text)
            ):
                return GenerationResponse(
                    text='{"finished": "not-a-boolean", "questions": "invalid"}',
                    provider_id="fake", model_id=request.model_id,
                )
            return super().generate(request)

    repo = MeetingRepository.create(
        tmp_path / "report", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Survey a field.", research_enabled=True,
        research_model=("fake", "m"), research_reasoning_effort=ReasoningEffort.DEFAULT,
        deliverable_type=DeliverableType.LITERATURE_REVIEW,
    )
    engine = MeetingEngine(
        repo=repo, adapters={"fake": InvalidQuestionAdapter()}, notifier=NoopNotifier(),
        configured_concurrency_limits={("fake", "m"): 1},
    )
    desk = ResearchDesk(repo=repo, engine=engine, retriever=DiscoveryRetriever())
    result = LiteratureReportPlanningRunner(
        repo=repo, engine=engine, governance_docs="docs/governance",
        exploration_service=ResearchExplorationService(desk),
    ).run()
    assert result.paused_reason == "HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED"
    chair_index = json.loads((repo.root / "public/literature_report/exploration/C001/CHAIR/index.json")
                             .read_text(encoding="utf-8"))
    assert chair_index["answers"] == []
