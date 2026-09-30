from __future__ import annotations

import json
from pathlib import Path

from project_ensemble.chair_qa import ChairQuestionService, _read_public_text, latest_draft
from project_ensemble.domain import GenerationResponse, ReasoningEffort


class FakeAdapter:
    provider_id = "fake"

    def __init__(self, plans: list[str], answers: list[str]):
        self.plans = iter(plans)
        self.answers = iter(answers)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        result = next(self.plans if "只输出 JSON" in request.system_text else self.answers)
        return GenerationResponse(text=result, provider_id="fake", model_id="test")


class FakeRetriever:
    backend_ids = ("fake-search",)

    def __init__(self):
        self.queries = []

    def retrieve_exploratory(self, query):
        self.queries.append(query)
        return ([{
            "source_id": "https://example.org/paper", "url": "https://example.org/paper",
            "title": "Diffusion in liquids", "abstract": "Diffusion is measured by trajectories.",
            "full_text_is_public": False,
        }], [{"query": query}])


def make_meeting(tmp_path: Path) -> Path:
    root = tmp_path / "LR-TEST"
    draft = root / "public/final/literature_review_report.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("# 扩散综述\n\n扩散系数可由轨迹估计。\n", encoding="utf-8")
    (root / "public/task.json").write_text(
        json.dumps({"description": "研究液体中的扩散"}), encoding="utf-8"
    )
    return root


def test_chair_qa_preserves_meeting_and_records_versioned_dialogue(tmp_path):
    root = make_meeting(tmp_path)
    original = (root / "public/final/literature_review_report.md").read_bytes()
    adapter = FakeAdapter(
        ['{"in_scope":true,"need_research":false,"queries":[]}',
         '{"in_scope":true,"need_research":false,"queries":[]}'],
        ["报告说明扩散系数可由轨迹估计。[S1]", "新稿仍讨论轨迹。[S1]"],
    )
    service = ChairQuestionService(
        root=root, adapter=adapter, model_id="test",
        reasoning_effort=ReasoningEffort.DEFAULT,
    )
    first = service.ask("扩散系数如何估计？")
    assert first["turn"] == 1
    assert first["sources"][0]["path"] == "public/final/literature_review_report.md"
    assert (root / "public/final/literature_review_report.md").read_bytes() == original
    (root / "public/final/literature_review_report.md").write_text(
        "# 扩散综述\n\n新稿明确讨论由轨迹估计扩散系数。\n", encoding="utf-8"
    )
    second_service = ChairQuestionService(
        root=root, adapter=adapter, model_id="test",
        reasoning_effort=ReasoningEffort.DEFAULT,
    )
    second = second_service.ask("新稿如何讨论轨迹？")
    assert second["turn"] == 2
    assert first["draft_revision"] != second["draft_revision"]
    assert "扩散系数如何估计" in adapter.requests[-1].user_text
    assert len(list((root / "human_private/chair_qa/turns").glob("*.json"))) == 2


def test_chair_qa_retrieval_is_sidecar_only(tmp_path):
    root = make_meeting(tmp_path)
    adapter = FakeAdapter(
        ['{"in_scope":true,"need_research":true,"queries":["diffusion trajectories"]}'],
        ["新检索提供了轨迹研究线索。[S2]"],
    )
    retriever = FakeRetriever()
    service = ChairQuestionService(
        root=root, adapter=adapter, model_id="test",
        reasoning_effort=ReasoningEffort.DEFAULT, retriever=retriever,
    )
    result = service.ask("有新的轨迹研究吗？")
    assert retriever.queries == ["diffusion trajectories"]
    assert result["research_queries"] == ["diffusion trajectories"]
    assert (root / "human_private/chair_qa/retrievals/000001.json").is_file()
    assert not (root / "public/research").exists()


def test_latest_draft_prefers_published_markdown(tmp_path):
    root = make_meeting(tmp_path)
    (root / "SOURCE_DRAFT.md").write_text("old draft", encoding="utf-8")
    assert latest_draft(root) == root / "public/final/literature_review_report.md"


def test_archived_pdf_body_is_searchable(tmp_path):
    from reportlab.pdfgen import canvas

    path = tmp_path / "paper.pdf"
    document = canvas.Canvas(str(path))
    document.drawString(60, 700, "Measured diffusion coefficient from trajectories")
    document.save()
    assert "Measured diffusion coefficient" in _read_public_text(path)


def test_bilingual_local_terms_can_recall_archived_literature(tmp_path):
    from reportlab.pdfgen import canvas

    root = make_meeting(tmp_path)
    pdf = root / "public/research/literature_bundle/documents/diffusion-paper.pdf"
    pdf.parent.mkdir(parents=True)
    document = canvas.Canvas(str(pdf))
    document.drawString(60, 700, "Diffusion coefficient from particle trajectories")
    document.save()
    adapter = FakeAdapter(
        ['{"in_scope":true,"local_terms":["diffusion coefficient"],'
         '"need_research":false,"queries":[]}'],
        ["论文讨论了粒子轨迹。[S1]"],
    )
    service = ChairQuestionService(
        root=root, adapter=adapter, model_id="test",
        reasoning_effort=ReasoningEffort.DEFAULT,
    )
    result = service.ask("有没有英文原文讨论扩散？")
    assert any(source["path"].endswith("diffusion-paper.pdf") for source in result["sources"])
