import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_ensemble.orchestration.literature_report_execution import (
    LiteratureReportExecutionRunner, ModuleDraft, _normalize_chapter_citation_ids,
)
from project_ensemble.orchestration.literature_writing_v071 import (
    WriterChapter, _mechanically_repair_writer_citations, _writer_chapter,
)
from project_ensemble.storage.documents import ImmutableDocumentStore


CATALOG = {"sources": [
    {"citation_id": "C00004-00011", "packet_ids": ["RP-ELEVEN"]},
    {"citation_id": "C00004-00012", "packet_ids": ["RP-TWELVE"]},
]}


@pytest.mark.parametrize("citation", ["C00004-0011", "C4-11", "C04-000011"])
def test_padding_alias_uses_only_unique_existing_frozen_source(citation):
    assert _normalize_chapter_citation_ids(f"[{citation}]", CATALOG) == "[C00004-00011]"


def test_padding_does_not_guess_unknown_other_chapter_or_ambiguous_source():
    text = "[C00004-0099] [C5-11] [C4-11]"
    ambiguous = {"sources": CATALOG["sources"] + [{"citation_id": "C04-011"}]}
    assert _normalize_chapter_citation_ids(text, ambiguous) == text
    assert _normalize_chapter_citation_ids("[C04-011]", ambiguous) == "[C04-011]"


def test_writer_normalizes_body_summary_glossary_and_deviation_without_mutating_source():
    chapter = WriterChapter.model_validate({
        "draft": {"title": "论证", "body_markdown": "论断【C00004-0011】。",
                  "short_summary": "概要［C4-12］。"},
        "glossary_additions": [{"term": "概念", "explanation_mode": "NATURAL_LANGUAGE",
                                "explanation": "定义[C4-11]。", "source_citation_ids": ["C4-11"]}],
        "outline_deviations": [{"outline_step_number": 1, "new_evidence_citation_ids": ["C4-12"],
                                "reason": "补充来源"}],
    })
    before = chapter.model_dump_json()
    normalized, operations = _mechanically_repair_writer_citations(chapter, CATALOG)
    assert chapter.model_dump_json() == before
    assert normalized.draft.body_markdown == "论断[C00004-00011]。"
    assert normalized.draft.short_summary == "概要[C00004-00012]。"
    assert normalized.glossary_additions[0].source_citation_ids == ["C00004-00011"]
    assert normalized.glossary_additions[0].explanation == "定义[C00004-00011]。"
    assert normalized.outline_deviations[0].new_evidence_citation_ids == ["C00004-00012"]
    assert all(item["operation"] == "CANONICALIZE_CITATION_ZERO_PADDING" for item in operations)
    again, operations = _mechanically_repair_writer_citations(normalized, CATALOG)
    assert again == normalized and operations == []


def test_final_source_validator_accepts_padding_but_rejects_real_unknown(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(CATALOG))
    runner = object.__new__(LiteratureReportExecutionRunner)
    runner.repo = SimpleNamespace(meeting_id="TEST", events=SimpleNamespace(append=lambda *args, **kwargs: None))
    module = SimpleNamespace(module_id="RM-04")
    draft = ModuleDraft(title="报告", body_markdown="论断[C00004-0011]。", short_summary="摘要【C4-12】。")
    result = runner._validate_chapter_source_citations(module, draft, catalog_path)
    assert result.body_markdown == "论断[C00004-00011]。"
    assert result.cited_packet_ids == ["RP-ELEVEN", "RP-TWELVE"]
    assert draft.body_markdown == "论断[C00004-0011]。"
    with pytest.raises(ValueError, match="unknown chapter sources"):
        runner._validate_chapter_source_citations(
            module, ModuleDraft(title="报告", body_markdown="[C4-99]", short_summary="摘要"), catalog_path,
        )


def test_resume_uses_validated_frozen_draft_without_replaying_earlier_model_responses(tmp_path):
    docs = ImmutableDocumentStore(tmp_path)
    chapter = WriterChapter(draft=ModuleDraft(title="已修稿", body_markdown="已修稿[C00004-00011]。", short_summary="摘要"))
    relative = "public/literature_report/modules/RM-04/writing_v071/writer_v1_validated.json"
    docs.write_once(relative, chapter.model_dump_json())
    before = (tmp_path / relative).read_bytes()
    runner = SimpleNamespace(repo=SimpleNamespace(root=tmp_path, docs=docs))
    path, result = _writer_chapter(runner, SimpleNamespace(module_id="RM-04"), 1,
                                   Path("unused_dossier"), Path("unused_outline"))
    assert result == chapter
    assert json.loads(path.read_text())["body_markdown"] == chapter.draft.body_markdown
    assert (tmp_path / relative).read_bytes() == before
