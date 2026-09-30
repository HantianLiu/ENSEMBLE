from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from project_ensemble.orchestration import literature_writing_v071 as writing
from project_ensemble.orchestration.literature_report_execution import (
    _effective_chapter_citation_catalog_path,
)
from project_ensemble.orchestration.consultations import (
    HumanConsultationIssue, HumanConsultationService,
)
from project_ensemble.runtime.progress import NullProgressReporter
from project_ensemble.storage.meeting import MeetingRepository


class _Docs:
    def __init__(self, root: Path):
        self.root = root

    def write_once(self, relative: Path, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        assert not path.exists()
        path.write_text(text, encoding="utf-8")


def _runner(tmp_path: Path):
    return SimpleNamespace(repo=SimpleNamespace(root=tmp_path, docs=_Docs(tmp_path)))


def test_glossary_preserves_alternative_name_for_critical_method(tmp_path):
    runner = _runner(tmp_path)
    chapter = writing.WriterChapter.model_construct(
        draft=writing.ModuleDraft.model_construct(
            body_markdown="alpha-shape 的尺度参数会改变离散边界。"
        ),
        glossary_additions=[writing.GlossaryTerm(
            term="α-形状（alpha-shape）", explanation_mode="NATURAL_LANGUAGE",
            explanation="由点集和尺度参数构造的几何边界，须说明筛选规则。",
        )],
    )
    path = writing._freeze_glossary(runner, SimpleNamespace(module_id="RM-01"), chapter)
    assert json.loads(path.read_text(encoding="utf-8"))[0]["term"] == "α-形状（alpha-shape）"


def test_outline_and_science_models_reject_incomplete_decisions():
    with pytest.raises(ValidationError):
        writing.OutlineBallot(vote="NO")
    with pytest.raises(ValidationError):
        writing.GlossaryTerm(term="sigma", explanation_mode="FORMULA", explanation="张力")
    with pytest.raises(ValidationError):
        writing.LocalCheck(status="MATERIAL_PROBLEM")


def test_writer_citation_cleanup_keeps_frozen_sources_and_removes_packet_metadata():
    chapter = writing.WriterChapter(draft=writing.ModuleDraft(
        title="供体筛选", short_summary="已核实的结论。",
        body_markdown=("原文支持这个具体结论【C00003-00001】。\n"
                       "工作元数据·证据包标识备案：[RP-MULTI] [RP-UNIQUE]\n"),
        cited_packet_ids=["RP-MULTI", "RP-UNIQUE"],
    ))
    catalog = {"sources": [
        {"citation_id": "C00003-00001", "packet_ids": ["RP-MULTI"]},
        {"citation_id": "C00003-00002", "packet_ids": ["RP-MULTI"]},
        {"citation_id": "C00003-00003", "packet_ids": ["RP-UNIQUE"]},
    ]}
    repaired, operations = writing._mechanically_repair_writer_citations(chapter, catalog)
    assert repaired.draft.body_markdown == "原文支持这个具体结论[C00003-00001]。"
    assert repaired.draft.cited_packet_ids == chapter.draft.cited_packet_ids
    assert operations[0]["operation"] == "REMOVE_INTERNAL_PACKET_METADATA_LINE"


def test_writer_citation_cleanup_only_maps_packets_with_one_source():
    chapter = writing.WriterChapter(draft=writing.ModuleDraft(
        title="研究", short_summary="简述", body_markdown="甲[RP-ONE]；乙[RP-MANY]。",
        cited_packet_ids=["RP-ONE", "RP-MANY"],
    ))
    catalog = {"sources": [
        {"citation_id": "C1-1", "packet_ids": ["RP-ONE", "RP-MANY"]},
        {"citation_id": "C1-2", "packet_ids": ["RP-MANY"]},
    ]}
    repaired, operations = writing._mechanically_repair_writer_citations(chapter, catalog)
    assert repaired.draft.body_markdown == "甲[C1-1]；乙[RP-MANY]。"
    assert len(operations) == 1


def test_writer_receives_c_labeled_knowledge_cards_without_packet_handles():
    catalog = {"sources": [{
        "citation_id": "C00003-00001", "source_id": "source-1",
        "packet_ids": ["RP-SECRET"], "title": "Original paper",
        "url": "https://example.org/source-1",
    }]}
    dossier = {"module_id": "RM-03", "packets": [{
        "packet_id": "RP-SECRET", "normalized_claim": "A checkable claim",
        "knowledge_status": "QUALIFIED", "consensus": "QUALIFIED",
        "sources": [{"source_id": "source-1", "title": "Original paper"}],
        "supporting_evidence": [{
            "source_id": "source-1", "evidence_summary": "Observed result",
            "applicability": "Only this population", "limitations": "Small sample",
        }],
    }]}
    visible = writing._writer_evidence_view(dossier, catalog)
    assert visible["knowledge_cards"][0]["findings"][0]["citation_id"] == "C00003-00001"
    assert visible["knowledge_cards"][0]["findings"][0]["finding"] == "Observed result"
    assert "RP-SECRET" not in json.dumps(visible, ensure_ascii=False)
    assert "packet_id" not in json.dumps(visible, ensure_ascii=False)
    assert "packet_ids" not in json.dumps(writing._prompt_citation_catalog(catalog))
    legacy = {"previous_draft": {"body_markdown": "Claim [RP-SECRET]",
                                 "cited_packet_ids": ["RP-SECRET"]}}
    sanitized = writing._writer_visible_payload(legacy, catalog)
    assert "RP-SECRET" not in json.dumps(sanitized)


def test_ambiguous_writer_citation_rechecks_original_question_without_overwriting(tmp_path):
    runner = _runner(tmp_path)
    calls = []
    source = SimpleNamespace(model_dump=lambda **_kwargs: {
        "source_id": "new-source", "title": "Primary evidence", "authors": [],
        "publication_year": 2026, "doi": None, "url": "https://example.org/new",
    })
    packet = SimpleNamespace(
        packet_id="RP-NEW", knowledge_status=SimpleNamespace(value="SOURCE_BACKED"),
        consensus=SimpleNamespace(value="CLEAR"), sources=[source],
        supporting_evidence=[], contradictory_evidence=[], scope_limitations=[],
    )
    def research(**kwargs):
        calls.append(kwargs)
        return packet
    runner._research_or_restore_model_prior = research
    module = SimpleNamespace(module_id="RM-03")
    dossier = {"packets": [{"packet_id": "RP-OLD", "original_claim": "Original testable question"}]}
    catalog = {"sources": [{"citation_id": "C00003-00001", "packet_ids": ["RP-OLD"],
                            "doi": None, "url": "https://example.org/old"}]}
    chapter = writing.WriterChapter(draft=writing.ModuleDraft(
        title="Chapter", short_summary="Summary", body_markdown="Claim [RP-OLD].",
        cited_packet_ids=["RP-OLD"],
    ))
    supplemented, findings = writing._recheck_writer_citation_evidence(
        runner, module, 1, dossier, catalog, chapter,
    )
    assert calls[0]["claim"] == "Original testable question"
    assert calls[0]["force_refresh"] is True
    assert catalog["sources"] == [{"citation_id": "C00003-00001", "packet_ids": ["RP-OLD"],
                                   "doi": None, "url": "https://example.org/old"}]
    assert supplemented["sources"][1]["citation_id"] == "C00003-00002"
    assert findings[0]["new_packet_id"] == "RP-NEW"
    restored, _ = writing._recheck_writer_citation_evidence(
        runner, module, 1, dossier, catalog, chapter,
    )
    assert restored == supplemented
    assert len(calls) == 1


def test_later_writer_and_publication_see_latest_supplemental_catalog(tmp_path):
    module_root = tmp_path / "public/literature_report/modules/RM-03"
    base = module_root / "research/chapter_citation_catalog.json"
    base.parent.mkdir(parents=True)
    base.write_text('{"sources": []}', encoding="utf-8")
    supplements = module_root / "writing_v071"
    supplements.mkdir()
    first = supplements / "writer_v1_citation_rechecked_catalog.json"
    second = supplements / "writer_v2_citation_rechecked_catalog.json"
    first.write_text('{"sources": [1]}', encoding="utf-8")
    second.write_text('{"sources": [1, 2]}', encoding="utf-8")
    assert _effective_chapter_citation_catalog_path(
        tmp_path, "RM-03", before_writer_version=1,
    ) == base
    assert _effective_chapter_citation_catalog_path(
        tmp_path, "RM-03", before_writer_version=2,
    ) == first
    assert _effective_chapter_citation_catalog_path(tmp_path, "RM-03") == second


def test_previously_frozen_manual_pause_reopens_a_resolvable_consultation(tmp_path, monkeypatch):
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Review a module.",
    )
    runner = SimpleNamespace(repo=repo)
    module = SimpleNamespace(module_id="RM-10")
    base = repo.root / "public/literature_report/modules/RM-10/writing_v071"
    base.mkdir(parents=True, exist_ok=True)
    for check_number in (1, 2):
        (base / f"final_local_science_check_{check_number}.json").write_text(
            json.dumps({"status": "MATERIAL_PROBLEM", "checks": [
                {"status": "MATERIAL_PROBLEM", "problem": "仍需限定条件"},
            ]}, ensure_ascii=False), encoding="utf-8",
        )
    monkeypatch.setattr(writing, "_local_science_check", lambda _runner, _module,
                        _prior, _final, _dossier, *, check_number:
                        base / f"final_local_science_check_{check_number}.json")
    monkeypatch.setattr(writing, "_writer_chapter", lambda *_args, **_kwargs:
                        (base / "writer_v4_validated.json", object()))
    service = HumanConsultationService(repo)
    for number, decision in ((1, "RETURN_TO_WRITER_LOCAL_REPAIR"),
                             (2, "PAUSE_FOR_MANUAL_REVIEW")):
        issue_id = f"HC-LW-RM-10-LOCAL-{number:02d}"
        service.open_issue(HumanConsultationIssue(
            issue_id=issue_id, meeting_id=repo.meeting_id,
            reason_code="LITERATURE_LOCAL_SCIENCE_CHECK_REQUIRED",
            stage="LITERATURE_MODULE_REVIEW",
            question=("智库长发现最终局部返修仍可能存在实质科学问题。"
                      "请选择要求主笔只修正这些段落，或在人类知悉风险后保留当前文本。"),
            options=(["RETURN_TO_WRITER_LOCAL_REPAIR", "PUBLISH_WITH_DISCLOSED_LIMITATION"]
                     if number == 1 else
                     ["PAUSE_FOR_MANUAL_REVIEW", "PUBLISH_WITH_DISCLOSED_LIMITATION"]),
            affected_items=["RM-10"],
            context={"check_path": str((base / f"final_local_science_check_{number}.json")
                                        .relative_to(repo.root)), "problems": ["仍需限定条件"]},
        ))
        service.resolve(issue_id=issue_id, decision=decision, rationale="人工决定",
                        scope="THIS_CONSULTATION_ONLY")
    with pytest.raises(writing.LiteratureWritingPaused,
                       match="LITERATURE_LOCAL_SCIENCE_CHECK_REOPENED"):
        writing._resolve_local_science_check(
            runner, module, object(), object(), base, base,
        )
    followup_id = "HC-LW-RM-10-LOCAL-02-REOPEN"
    assert any(item.issue_id == followup_id for item in service.open_issues())
    service.resolve(issue_id=followup_id,
                    decision="PUBLISH_WITH_DISCLOSED_LIMITATION",
                    rationale="知悉仍有科学疑点", scope="THIS_CONSULTATION_ONLY")
    result = writing._resolve_local_science_check(
        runner, module, object(), object(), base, base,
    )
    assert result[0].name == "v071-v4.json"


def test_science_rescue_automatic_majority_needs_no_new_vote(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    monkeypatch.setattr(writing, "_frozen_or_call", lambda *_args, **_kwargs: pytest.fail("unexpected vote"))
    librarians = [{"representative_id": item} for item in ("A", "B", "C")]
    issue = {"issue_id": "SCI-R1-001", "proposer_labels": ["L01", "L02"], "issue": {}}
    result = writing._rescue_rejection(
        runner, SimpleNamespace(module_id="RM-01"), 1, issue,
        {"L01": "A", "L02": "B"}, librarians,
    )
    assert result["adopted"] is True
    assert result["basis"] == "INDEPENDENT_LIBRARIAN_MAJORITY_AUTOMATIC_RESCUE"


def test_science_rescue_only_nonproposers_vote_and_freezes(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    called = []

    def vote(_runner, _relative, _schema, rid, *_args):
        called.append(rid)
        return writing.RescueVote(vote="YES")

    monkeypatch.setattr(writing, "_frozen_or_call", vote)
    librarians = [{"representative_id": item} for item in ("A", "B", "C")]
    issue = {"issue_id": "SCI-R1-001", "proposer_labels": ["L01"], "issue": {}}
    result = writing._rescue_rejection(
        runner, SimpleNamespace(module_id="RM-01"), 1, issue,
        {"L01": "A"}, librarians,
    )
    assert called == ["B", "C"]
    assert (result["yes"], result["required_yes"]) == (2, 2)
    assert writing._rescue_rejection(
        runner, SimpleNamespace(module_id="RM-01"), 1, issue,
        {"L01": "A"}, librarians,
    ) == result
    assert called == ["B", "C"]


def test_rescue_keeps_two_reviewer_electorate_for_preexisting_frozen_round(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    runner.active = [{"representative_id": rid} for rid in ("A", "B")]
    monkeypatch.setattr(writing, "_science_librarians",
                        lambda *_args, **_kwargs: pytest.fail("old round electorate changed"))
    called = []
    def vote(_runner, _relative, _schema, rid, *_args):
        called.append(rid)
        return writing.RescueVote(vote="YES")
    monkeypatch.setattr(writing, "_frozen_or_call", vote)
    result = writing._rescue_rejection(
        runner, SimpleNamespace(module_id="RM-01"), 1,
        {"issue_id": "SCI-R1-001", "proposer_labels": ["L01"], "issue": {}},
        {"L01": "A", "L02": "B"}, runner.active,
    )
    assert result["eligible_count"] == 1
    assert called == ["B"]


def test_changed_paragraphs_is_local_not_full_document():
    assert writing._changed_paragraphs("alpha\n\nbeta\n\ngamma", "alpha\n\nnew beta\n\ngamma") == [
        {"action": "replace", "old": "beta", "new": "new beta"}
    ]


def test_science_review_context_is_bounded_and_includes_contradictions():
    source = {"source_id": "S", "title": "A source", "publication_year": 2025,
              "archive_status": "ARCHIVED", "evidence_use_class": "PRIMARY"}
    finding = {"source_id": "S", "evidence_summary": "A" * 2000,
               "applicability": "B" * 1000, "limitations": "C" * 1000}
    dossier = {"module_id": "RM-07", "packets": [
        {"packet_id": f"RP-{index}", "normalized_claim": "claim",
         "sources": [source] * 40,
         "supporting_evidence": [finding] * 30,
         "contradictory_evidence": [dict(finding, evidence_summary="counterclaim")] * 2,
         "scope_limitations": [dict(finding, evidence_summary="limited scope")] * 2}
        for index in range(32)
    ]}
    chapter = writing.WriterChapter.model_construct(draft=writing.ModuleDraft.model_construct(
        title="chapter", body_markdown="body", short_summary="summary",
        cited_packet_ids=[f"RP-{index}" for index in range(32)],
    ))
    compact = writing._science_review_evidence(dossier, chapter)
    assert len(json.dumps(compact, ensure_ascii=False)) < 700_000
    assert len(compact["packets"]) == 32
    assert any(item["direction"] == "contradictory_evidence"
               for item in compact["packets"][0]["findings"])
    assert compact["packets"][0]["omitted_finding_count"] > 0


def test_science_review_prioritizes_late_finding_from_cited_source():
    findings = [
        {"source_id": "UNCITED", "evidence_summary": "background " + "x" * 500,
         "applicability": "background", "limitations": ""}
        for _ in range(12)
    ]
    findings.append({"source_id": "CITED", "evidence_summary": "糖尿病风险信号",
                     "applicability": "本章所引来源", "limitations": "只适用于该研究"})
    dossier = {"module_id": "RM-02", "packets": [{
        "packet_id": "RP-TARGET", "normalized_claim": "claim",
        "sources": [{"source_id": "CITED", "title": "Cited study"}],
        "supporting_evidence": findings, "contradictory_evidence": [],
        "scope_limitations": [],
    }]}
    chapter = writing.WriterChapter.model_construct(draft=writing.ModuleDraft.model_construct(
        title="chapter", body_markdown="引用来源", short_summary="summary",
        cited_packet_ids=["RP-TARGET"],
    ))
    view = writing._science_review_evidence(
        dossier, chapter, cited_source_ids={"CITED"}, max_chars=4500)
    packet = view["packets"][0]
    assert any("糖尿病" in item["finding"] for item in packet["findings"])
    assert packet["omitted_cited_source_finding_count"] == 0
    assert packet["omitted_finding_count"] > 0


def test_standby_librarian_only_takes_seat_after_failed_primary_output(tmp_path):
    runner = _runner(tmp_path)
    runner.active = [
        {"representative_id": rid, "runtime": {"persona": "librarian"}}
        for rid in ("PRIMARY", "STANDBY")
    ]
    assert [item["representative_id"] for item in writing._science_librarians(runner)] == ["PRIMARY"]
    exchange_dir = tmp_path / "governance_private/provider_exchanges"
    exchange_dir.mkdir(parents=True)
    exchange_dir.joinpath("X-FAILED.json").write_text(json.dumps({
        "exchange_id": "X-FAILED", "participant_id": "PRIMARY",
        "stage": "literature_v071_science_RM-10_r1",
        "response": {"text": "", "raw": {"choices": [{"finish_reason": "length"}]}},
    }), encoding="utf-8")
    assert [item["representative_id"] for item in writing._science_librarians(
        runner, stage="literature_v071_science_RM-09_r1"
    )] == ["PRIMARY"]
    assert [item["representative_id"] for item in writing._science_librarians(
        runner, stage="literature_v071_science_RM-10_r1"
    )] == ["STANDBY"]
    activation = tmp_path / "governance_private/literature_report/writing_v071/librarian_standby_activation.json"
    assert json.loads(activation.read_text(encoding="utf-8"))["failed_exchange_id"] == "X-FAILED"
    assert [item["representative_id"] for item in writing._science_librarians(runner)] == ["STANDBY"]


def test_resumed_science_round_reuses_frozen_standby_review(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    runner.active = [
        {"representative_id": rid, "runtime": {"persona": "librarian",
         "provider_id": "fake", "model_id": rid}}
        for rid in ("PRIMARY", "STANDBY")
    ]
    runner.engine = SimpleNamespace(progress=NullProgressReporter(),
                                    model_concurrency_limit=lambda *_args: 1)
    runner._invoke_service = lambda *_args, **_kwargs: pytest.fail("frozen standby must be reused")
    monkeypatch.setattr(writing, "_literature_step", lambda *_args: None)
    exchange_dir = tmp_path / "governance_private/provider_exchanges"
    exchange_dir.mkdir(parents=True)
    exchange_dir.joinpath("X-FAILED.json").write_text(json.dumps({
        "exchange_id": "X-FAILED", "participant_id": "PRIMARY",
        "stage": "literature_v071_science_RM-10_r1",
        "response": {"text": "", "raw": {"choices": [{"finish_reason": "length"}]}},
    }), encoding="utf-8")
    review_path = tmp_path / "governance_private/literature_report/writing_v071/RM-10/science_round_1_STANDBY.json"
    review_path.parent.mkdir(parents=True)
    review_path.write_text(writing.ScienceChecklist().model_dump_json(), encoding="utf-8")
    dossier = tmp_path / "dossier.json"
    dossier.write_text('{"module_id":"RM-10","packets":[]}', encoding="utf-8")
    module = SimpleNamespace(module_id="RM-10", model_dump=lambda **_: {"module_id": "RM-10"})
    chapter = writing.WriterChapter(draft=writing.ModuleDraft(
        title="Module", body_markdown="Supported text", short_summary="Summary"
    ))
    result = writing._science_round(runner, module, 1, chapter, dossier, None)
    assert len(json.loads(result.read_text(encoding="utf-8"))["reviews"]) == 1
    assert json.loads(result.read_text(encoding="utf-8"))["reviews"][0]["reviewer_label"] == "L01"


def test_all_writing_evidence_views_have_aggregate_limits():
    source = {"source_id": "S", "title": "T" * 400, "publication_year": 2025}
    finding = {"source_id": "S", "evidence_summary": "F" * 3000,
               "applicability": "A" * 1000, "limitations": "L" * 1000}
    dossier = {"module_id": "RM-07", "packets": [
        {"packet_id": f"RP-{number}", "normalized_claim": "C" * 2000,
         "sources": [source] * 20, "supporting_evidence": [finding] * 20,
         "contradictory_evidence": [finding], "scope_limitations": [finding]}
        for number in range(80)
    ]}
    view = writing._science_review_evidence(dossier)
    assert len(json.dumps(view, ensure_ascii=False, indent=2)) < 340_000
    assert view["omitted_packet_count"] > 0 or any(
        item["omitted_finding_count"] for item in view["packets"]
    )
    index = {"module_id": "RM-07", "cards": [
        {"claim": "C" * 2000, "finding": "F" * 3000, "direction": "SUPPORTING",
         "applicability": "A" * 1000, "limitations": "L" * 1000,
         "citation_id": f"C00007-{number:05d}"}
        for number in range(200)
    ], "unresolved_questions": [{"claim": "C" * 1000,
                                 "questions": ["Q" * 1000]}] * 100}
    compact_index = writing._prompt_fact_index(index)
    assert len(json.dumps(compact_index, ensure_ascii=False, indent=2)) < 290_000
    assert compact_index["omitted_card_count"] > 0
    catalog = {"sources": [
        {"citation_id": f"C00007-{number:05d}", "packet_ids": ["RP-1"],
         "title": "T" * 1000, "url": "https://example.org/" + "u" * 1000}
        for number in range(500)
    ]}
    compact_catalog = writing._prompt_citation_catalog(catalog)
    assert len(compact_catalog["sources"]) == 500
    assert len(json.dumps(compact_catalog, ensure_ascii=False, indent=2)) < 225_000
    glossary = [{"term": f"term-{number}", "explanation_mode": "FORMULA",
                 "explanation": "E" * 1800, "formula": "x" * 1200}
                for number in range(300)]
    compact_glossary = writing._prompt_glossary(glossary)
    assert len(json.dumps(compact_glossary, ensure_ascii=False, indent=2)) < 85_000
    assert len(compact_glossary["terms"]) == 300


def test_local_science_check_uses_both_draft_citations_not_full_dossier(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    runner._v071_progress = (1, 1)
    runner.engine = SimpleNamespace(status=SimpleNamespace(phase=None),
                                    progress=NullProgressReporter())
    runner.active = [{"representative_id": "LIB", "runtime": {"persona": "librarian"}}]
    module = SimpleNamespace(module_id="RM-07", title="Module")
    module.model_dump = lambda **_kwargs: {"module_id": "RM-07", "title": "Module"}
    dossier_path = tmp_path / "dossier.json"
    dossier_path.write_text(json.dumps({"module_id": "RM-07", "packets": [
        {"packet_id": f"RP-{number}", "normalized_claim": "claim",
         "sources": [], "supporting_evidence": []} for number in range(100)
    ]}), encoding="utf-8")
    prior = writing.WriterChapter.model_construct(draft=writing.ModuleDraft.model_construct(
        body_markdown="old", cited_packet_ids=["RP-1"]))
    final = writing.WriterChapter.model_construct(draft=writing.ModuleDraft.model_construct(
        body_markdown="new", cited_packet_ids=["RP-2"]))
    captured = []

    def invoke(_runner, _relative, _schema, _rid, _stage, _system, user):
        captured.append(user)
        return writing.LocalCheck(status="PASS")

    monkeypatch.setattr(writing, "_frozen_or_call", invoke)
    result = writing._local_science_check(runner, module, prior, final, dossier_path)
    assert result.exists()
    assert "source_catalog" not in captured[0]
    assert {item["packet_id"] for item in captured[0]["review_evidence"]["packets"]} == {
        "RP-1", "RP-2"
    }
    assert runner.engine.status.phase == writing.MeetingPhase.LITERATURE_MODULE_REVIEW


def test_glossary_only_revision_still_receives_local_science_check(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    runner._v071_progress = (1, 1)
    runner.engine = SimpleNamespace(status=SimpleNamespace(phase=None),
                                    progress=NullProgressReporter())
    runner.active = [{"representative_id": "LIB", "runtime": {"persona": "librarian"}}]
    module = SimpleNamespace(module_id="RM-01", title="Module")
    module.model_dump = lambda **_kwargs: {"module_id": "RM-01", "title": "Module"}
    dossier = tmp_path / "dossier.json"
    dossier.write_text(json.dumps({"module_id": "RM-01", "packets": []}), encoding="utf-8")
    draft = writing.ModuleDraft.model_construct(body_markdown="same", cited_packet_ids=[])
    prior = writing.WriterChapter.model_construct(draft=draft, glossary_additions=[])
    final = writing.WriterChapter.model_construct(draft=draft, glossary_additions=[writing.GlossaryTerm(
        term="线张力", explanation_mode="NATURAL_LANGUAGE",
        explanation="指定条件下界面长度变化的响应系数。",
    )])
    reviewed = []

    def invoke(_runner, _relative, _schema, _rid, _stage, _system, user):
        reviewed.append(user)
        return writing.LocalCheck(status="PASS")

    monkeypatch.setattr(writing, "_frozen_or_call", invoke)
    writing._local_science_check(runner, module, prior, final, dossier)
    assert len(reviewed) == 1
    assert reviewed[0]["changed_paragraphs"] == []
    assert reviewed[0]["final_glossary_additions"][0]["term"] == "线张力"


def test_immutable_frozen_artifact_rejects_conflicting_resume(tmp_path):
    runner = _runner(tmp_path)
    relative = Path("public/literature_report/example.json")
    writing._freeze(runner, relative, {"accepted": True})
    assert json.loads((tmp_path / relative).read_text(encoding="utf-8")) == {"accepted": True}
    writing._freeze(runner, relative, {"accepted": True})
    with pytest.raises(ValueError, match="conflicts"):
        writing._freeze(runner, relative, {"accepted": False})


def test_fact_first_index_leads_with_findings_and_keeps_source_mapping(tmp_path):
    runner = _runner(tmp_path)
    module = SimpleNamespace(module_id="RM-01")
    dossier = tmp_path / "dossier.json"
    catalog = tmp_path / "catalog.json"
    dossier.write_text(json.dumps({"packets": [{
        "packet_id": "RP-1", "normalized_claim": "A result depends on sampling window",
        "knowledge_status": "SOURCE_BACKED", "consensus": "QUALIFIED",
        "sources": [{"source_id": "S-1", "doi": "10.1000/test", "url": "https://example.org/paper"}],
        "supporting_evidence": [{"source_id": "S-1", "evidence_summary": "Width changes with window",
                                 "applicability": "Model X", "limitations": "Only one condition"}],
        "unresolved_questions": ["Does this hold in Model Y?"],
    }]}), encoding="utf-8")
    catalog.write_text(json.dumps({"sources": [{"source_id": "OTHER-ID", "doi": "10.1000/test",
                                                "url": "https://example.org/paper",
                                                "citation_id": "C00001-00001"}]}), encoding="utf-8")
    index = writing._fact_first_index(runner, module, dossier, catalog)
    assert index["cards"][0]["finding"] == "Width changes with window"
    assert index["cards"][0]["citation_id"] == "C00001-00001"
    assert index["cards"][0]["source_access_basis"] == "METADATA_OR_ABSTRACT"
    assert index["cards"][0]["packet_id_for_audit_only"] == "RP-1"
    assert writing._fact_first_index(runner, module, dossier, catalog) == index


def test_one_module_runs_outline_writer_and_two_science_rounds_without_provider(tmp_path):
    runner = _runner(tmp_path)
    runner.repo.meeting_id = "LR-TEST"
    events = []
    runner.repo.events = SimpleNamespace(append=lambda *args, **kwargs: events.append(args[0]))
    runner.engine = SimpleNamespace(
        status=SimpleNamespace(phase=None), progress=NullProgressReporter(),
        model_concurrency_limit=lambda *_args: 1,
    )
    personas = (
        "systems_integrator", "pragmatic_minimalist", "exploratory_synthesist", "librarian"
    )
    runner.active = [{"representative_id": f"R-{index}",
                      "runtime": {"persona": persona, "provider_id": "fake", "model_id": "m"}}
                     for index, persona in enumerate(personas)]
    runner._select_drafting_team = lambda *_args: runner.active
    calls = []

    def invoke(participant_id, *, stage, schema, system, user):
        calls.append((participant_id, stage))
        if participant_id == "WRITER":
            assert "RP-" not in json.dumps(user, ensure_ascii=False)
        if schema is writing.ModuleWritingOutline:
            return schema(steps=[writing.OutlineStep(
                heading="Evidence", purpose="Explain the result", evidence_boundary="Use only dossier"
            )])
        if schema is writing.OutlineBallot:
            return schema(vote="YES")
        if schema is writing.WriterChapter:
            if stage.endswith("citation_repair"):
                body = "A concise, sourced module [C00001-00001]."
            else:
                body = "A concise, sourced module [RP-1]."
            return schema(draft=writing.ModuleDraft(
                title="Module", body_markdown=body,
                short_summary="A concise module summary.", cited_packet_ids=["RP-1"],
            ))
        if schema is writing.ScienceChecklist:
            return schema()
        pytest.fail(f"unexpected schema: {schema}")

    runner._invoke_service = invoke
    runner._writing_preferences = lambda: {"language": "en"}
    validation_order = []
    def verify_model_priors(*, draft, **_kwargs):
        validation_order.append("model_priors")
        return draft
    def validate_sources(_module, draft, _catalog, **_kwargs):
        validation_order.append("chapter_sources")
        return draft
    runner._verify_model_prior_claims = verify_model_priors
    runner._validate_chapter_source_citations = validate_sources
    runner._validate_citations = lambda _ids: None

    def catalog(module, _dossier):
        path = tmp_path / "public/literature_report/modules" / module.module_id / "research/chapter_citation_catalog.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sources": [{"citation_id": "C00001-00001",
                         "packet_ids": ["RP-1"]}]}), encoding="utf-8")
        return path

    runner._ensure_chapter_citation_catalog = catalog
    module = SimpleNamespace(module_id="RM-01", title="Module",
                             model_dump=lambda **_kwargs: {"module_id": "RM-01", "title": "Module"})
    dossier = tmp_path / "dossier.json"
    dossier.write_text('{"packets": []}', encoding="utf-8")
    outcome = writing.run_module_v071(runner, None, module, 1, dossier)
    assert outcome["writing_policy"] == "v071"
    assert outcome["local_science_check_status"] == "PASS"
    assert len([item for item in calls if item[0] == "WRITER"]) == 1
    assert "[C00001-00001]" in (
        tmp_path / outcome["draft_path"]
    ).read_text(encoding="utf-8")
    assert len([item for item in calls if "science_RM-01" in item[1]]) == 2
    assert "LITERATURE_MODULE_FROZEN_V071" in events
    assert validation_order[:3] == ["chapter_sources", "model_priors", "chapter_sources"]
    assert writing.run_module_v071(runner, None, module, 1, dossier) == outcome
