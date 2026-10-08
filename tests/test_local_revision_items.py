"""A bad suggestion must not discard valid science corrections or open a loop."""
import json
import io
from types import SimpleNamespace

import pytest

from project_ensemble.errors import RepresentativeUnavailableError, ResearchQualityControlError
from project_ensemble.orchestration.consultations import HumanConsultationService
from project_ensemble.orchestration.literature_fast import (
    FastLiteratureRunner, FastLocalScienceRepair, FastResolutionVote,
    _apply_local_science_items,
)
from project_ensemble.orchestration.literature_report import OutlineModule
from project_ensemble.orchestration.literature_report_execution import ModuleDraft
from project_ensemble.orchestration.literature_writing_v071 import WriterChapter
from project_ensemble.runtime.progress import NullProgressReporter
from project_ensemble.storage.documents import ImmutableDocumentStore


def term(name, explanation="原定义", **extra):
    return {"term": name, "explanation_mode": "NATURAL_LANGUAGE",
            "explanation": explanation, **extra}


@pytest.fixture
def revision(tmp_path):
    runner = object.__new__(FastLiteratureRunner)
    runner.repo = SimpleNamespace(
        root=tmp_path, meeting_id="LR-TEST", docs=ImmutableDocumentStore(tmp_path),
        events=SimpleNamespace(append=lambda *_args, **_kwargs: None),
    )
    runner.engine = SimpleNamespace(progress=NullProgressReporter(), status=SimpleNamespace(phase=None))
    runner._v071_progress = (2, 2)
    runner._validate_citations = lambda _ids: None
    module = OutlineModule(module_id="RM-02", title="方法导引", research_questions=["定义？"],
                           required_evidence=["原始资料"], source_submission_refs=["S-1"])
    catalog = tmp_path / "public/literature_report/modules/RM-02/research/chapter_citation_catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"sources": [{"citation_id": "C2-1", "packet_ids": ["RP-ONE"]}]}))
    review = tmp_path / "public/literature_report/fast/RM-02/science_recheck_v1.json"
    review.parent.mkdir(parents=True)
    review.write_text(json.dumps({"votes": [{"remaining_material_problems": ["异议一", "异议二", "异议三"]}]}))
    glossary = tmp_path / "public/literature_report/writing_v071/glossary_after_RM-01.json"
    glossary.parent.mkdir(parents=True)
    glossary.write_text(json.dumps([term("粗粒化")]))
    chapter = WriterChapter(draft=ModuleDraft(title="方法导引", body_markdown="第一段。\n\n第二段。",
                                             short_summary="概要。"))
    return runner, module, chapter, review


def audit(runner):
    return json.loads((runner.repo.root / "public/literature_report/modules/RM-02/writing_v071/"
                      "writer_v2_local_patch_applied.json").read_text())


def configure_technician(runner):
    path = runner.repo.root / "identity_private/meeting_manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"technician_model": ["fake", "technician"]}))


def test_inherited_glossary_upserts_are_isolated_and_match_writer_context(revision):
    runner, module, chapter, review = revision
    original = chapter.model_dump_json()
    glossary_path = runner.repo.root / "public/literature_report/writing_v071/glossary_after_RM-01.json"
    frozen_glossary = glossary_path.read_bytes()
    calls = []

    def invoke(participant, **kwargs):
        calls.append(participant)
        assert kwargs["user"]["current_draft"]["editable_glossary_entries"][0]["term"] == "粗粒化"
        assert "无需区分新增与修改" in kwargs["system"]
        return FastLocalScienceRepair(glossary_additions=[
            {"entry": term("尺度变换", "新定义"), "objection_numbers": [1]},
            {"entry": term("粗粒化", "修订定义"), "objection_numbers": [2]},
            {"entry": term("错误来源", source_citation_ids=["C999-1"]), "objection_numbers": [3]},
        ])

    runner._invoke_service = invoke
    _, repaired = runner._local_science_repair(module, 2, chapter, review)
    assert [entry.term for entry in repaired.glossary_additions] == ["尺度变换", "粗粒化"]
    assert repaired.glossary_additions[1].explanation == "修订定义"
    assert chapter.model_dump_json() == original
    assert glossary_path.read_bytes() == frozen_glossary
    assert calls == ["WRITER"]
    assert len(audit(runner)["accepted_items"]) == 2
    assert len(audit(runner)["rejected_items"]) == 1
    assert HumanConsultationService(runner.repo).open_issues() == []
    # Resume is cached, not another Writer cycle.
    assert runner._local_science_repair(module, 2, chapter, review)[1] == repaired
    assert calls == ["WRITER"]


@pytest.mark.parametrize("technician_result", ["fixed", "omitted", "unavailable", "changed_science", "invalid_json"])
def test_technician_repairs_only_bad_items_and_never_loses_valid_edits(revision, technician_result):
    runner, module, chapter, review = revision
    configure_technician(runner)
    calls = []

    def invoke(participant, **kwargs):
        calls.append(participant)
        if participant == "WRITER":
            return FastLocalScienceRepair(edits=[
                {"paragraph_number": 1, "new_text": "第一段已纠正。", "objection_numbers": [1]},
                {"paragraph_number": 999, "new_text": "第二段已纠正。", "objection_numbers": [2]},
            ], objection_responses=[{"objection_number": 3, "response": "该建议不适用，定义域已在前文说明。"}])
        assert participant == "TECHNICIAN"
        assert len(kwargs["user"]["failed_items"]) == 1
        assert kwargs["user"]["failed_items"][0]["suggestion"]["paragraph_number"] == 999
        if technician_result == "unavailable":
            raise RepresentativeUnavailableError("排障服务暂不可用")
        if technician_result == "invalid_json":
            raise ResearchQualityControlError("LOCAL_REVISION_FORMAT_UNUSABLE", "排障输出无法解析")
        if technician_result == "omitted":
            return FastLocalScienceRepair()
        return FastLocalScienceRepair(edits=[{
            "paragraph_number": 2,
            "new_text": "第二段已纠正。" if technician_result == "fixed" else "擅自改变科学结论。",
            "objection_numbers": [2],
        }])

    runner._invoke_service = invoke
    _, repaired = runner._local_science_repair(module, 2, chapter, review)
    expected_second = "第二段已纠正。" if technician_result == "fixed" else "第二段。"
    assert repaired.draft.body_markdown == "第一段已纠正。\n\n" + expected_second
    assert calls == ["WRITER", "TECHNICIAN"]
    record = audit(runner)
    assert record["objections"] == ["异议一", "异议二", "异议三"]
    assert record["science_review_required"] is True
    assert record["objection_responses"][0]["objection_number"] == 3
    if technician_result == "fixed":
        assert record["technician_item_repair"]["status"] == "COMPLETED"
    elif technician_result == "omitted":
        assert record["technician_item_repair"]["status"] == "NO_REPAIR_APPLIED"
        assert record["technician_item_repair"]["omitted_item_count"] == 1
    assert HumanConsultationService(runner.repo).open_issues() == []


def test_unknown_citation_only_rejects_its_own_edit(revision):
    runner, module, chapter, review = revision
    runner._invoke_service = lambda *_args, **_kwargs: FastLocalScienceRepair(edits=[
        {"paragraph_number": 1, "new_text": "合理修订【C2-1】。", "objection_numbers": [1]},
        {"paragraph_number": 2, "new_text": "伪造来源[C99-1]。", "objection_numbers": [2]},
    ])
    _, repaired = runner._local_science_repair(module, 2, chapter, review)
    assert repaired.draft.body_markdown == "合理修订[C2-1]。\n\n第二段。"
    assert len(audit(runner)["rejected_items"]) == 1
    assert repaired.draft.cited_packet_ids == ["RP-ONE"]


def test_all_invalid_items_continue_with_unchanged_draft_and_original_objections(revision):
    runner, module, chapter, review = revision
    runner._invoke_service = lambda *_args, **_kwargs: FastLocalScienceRepair(edits=[
        {"paragraph_number": 999, "new_text": "定位不存在。", "objection_numbers": [1]},
    ])
    _, repaired = runner._local_science_repair(module, 2, chapter, review)
    assert repaired == chapter
    assert audit(runner)["objections_without_applied_edit"] == [1, 2, 3]
    assert HumanConsultationService(runner.repo).open_issues() == []


def test_science_recheck_sees_unapplied_items_and_reasoned_response(revision):
    runner, module, chapter, review = revision
    runner._invoke_service = lambda *_args, **_kwargs: FastLocalScienceRepair(
        objection_responses=[{"objection_number": 1, "response": "原表述有依据，无需修改。"}],
    )
    _, repaired = runner._local_science_repair(module, 2, chapter, review)
    assert repaired == chapter
    runner._reviewers = lambda **_kwargs: [{"representative_id": "R-A"}, {"representative_id": "R-B"}]
    runner._review_evidence = lambda *_args: {}
    dossier = runner.repo.root / "dossier.json"
    dossier.write_text("{}")
    calls = []

    def review_call(participant, **kwargs):
        calls.append(participant)
        payload = kwargs["user"]["local_revision_application"]
        assert payload["objection_responses"][0]["response"] == "原表述有依据，无需修改。"
        assert payload["objections_without_applied_edit"] == [1, 2, 3]
        assert "技术上跳过某项不代表对应科学异议已解决" in kwargs["system"]
        return FastResolutionVote(resolved=False, remaining_material_problems=["异议二仍需纠正"])

    runner._invoke_service = review_call
    passed, _ = runner._recheck(module, repaired, review, dossier, 2)
    assert not passed
    assert calls == ["R-A", "R-B"]


def test_retry_after_failed_item_does_not_mutate_the_input_chapter(revision):
    _, _, chapter, _ = revision
    inherited = {"粗粒化": term("粗粒化")}
    original = chapter.model_dump_json()
    proposal = FastLocalScienceRepair(glossary_additions=[
        {"entry": term("尺度变换"), "objection_numbers": [1]},
        {"entry": term("粗粒化", "修订定义"), "objection_numbers": [2]},
    ])

    def reject_second(entry, _previous):
        if entry.term == "粗粒化":
            raise ValueError("模拟独立失败")

    for _ in range(3):
        repaired, record = _apply_local_science_items(
            chapter, proposal, inherited, 2, lambda new, *_: new, reject_second,
        )
        assert [entry.term for entry in repaired.glossary_additions] == ["尺度变换"]
        assert len(record["accepted_items"]) == len(record["rejected_items"]) == 1
        assert chapter.model_dump_json() == original
        assert inherited == {"粗粒化": term("粗粒化")}


def test_malformed_item_survives_parsing_for_technician_without_rejecting_response(revision):
    runner, module, chapter, review = revision
    configure_technician(runner)
    calls = []

    def invoke(participant, **kwargs):
        calls.append(participant)
        if participant == "WRITER":
            return FastLocalScienceRepair.model_validate({"edits": [
                {"paragraph_number": 1, "new_text": "有效修改。", "objection_numbers": [1]},
                {"paragraph_number": "第二段", "new_text": "修正后的第二段。", "objection_numbers": [2]},
                "不是修改对象",
            ]})
        assert len(kwargs["user"]["failed_items"]) == 2
        return FastLocalScienceRepair(edits=[{
            "paragraph_number": 2, "new_text": "修正后的第二段。", "objection_numbers": [2],
        }])

    runner._invoke_service = invoke
    _, repaired = runner._local_science_repair(module, 2, chapter, review)
    assert repaired.draft.body_markdown == "有效修改。\n\n修正后的第二段。"
    assert calls == ["WRITER", "TECHNICIAN"]
    record = audit(runner)
    assert record["technician_item_repair"]["status"] == "PARTIALLY_COMPLETED"
    assert record["technician_item_repair"]["repaired_item_count"] == 1
    assert record["technician_item_repair"]["omitted_item_count"] == 1


def test_local_revision_can_correct_a_stale_inference_label(revision):
    runner, module, chapter, review = revision
    old_label = "肌少症常见轨归属对其患病率数值不敏感：区间内任意取值均高出5/10,000一至两个数量级"
    new_label = "肌少症常见轨归属对患病率数值不敏感：区间内任意取值均高出5/10,000至少两个数量级"
    chapter = chapter.model_copy(update={
        "draft": chapter.draft.model_copy(update={"inference_labels": [old_label]})
    })
    def invoke(_participant, **kwargs):
        assert kwargs["user"]["current_draft"]["editable_inference_labels"] == [
            {"label_index": 0, "expected_text": old_label},
        ]
        assert "inference_label_edits" in kwargs["system"]
        return FastLocalScienceRepair(inference_label_edits=[{
            "label_index": 0, "expected_text": old_label, "new_text": new_label,
            "objection_numbers": [1],
        }])

    runner._invoke_service = invoke

    _, repaired = runner._local_science_repair(module, 2, chapter, review)

    record = audit(runner)
    assert repaired.draft.inference_labels == [new_label]
    assert {item["group"] for item in record["accepted_items"]} == {"inference_label_edits"}
    assert record["objections_without_applied_edit"] == [2, 3]
    assert record["result_sha256"] != record["source_sha256"]


def test_technician_can_relocate_but_not_reword_an_inference_label_edit(revision):
    runner, module, chapter, review = revision
    configure_technician(runner)
    old_label = "旧标签"
    new_label = "新标签"
    chapter = chapter.model_copy(update={
        "draft": chapter.draft.model_copy(update={"inference_labels": [old_label]})
    })

    def invoke(participant, **_kwargs):
        if participant == "WRITER":
            return FastLocalScienceRepair(inference_label_edits=[{
                "label_index": 1, "expected_text": "错误锚点", "new_text": new_label,
                "objection_numbers": [1],
            }])
        return FastLocalScienceRepair(inference_label_edits=[{
            "label_index": 0, "expected_text": old_label, "new_text": new_label,
            "objection_numbers": [1],
        }])

    runner._invoke_service = invoke
    _, repaired = runner._local_science_repair(module, 2, chapter, review)
    record = audit(runner)
    assert repaired.draft.inference_labels == [new_label]
    assert record["technician_item_repair"]["status"] == "COMPLETED"
    assert record["technician_item_repair"]["repaired_item_count"] == 1
    assert record["accepted_items"][0]["group"] == "inference_label_edits"


def test_inference_label_edit_requires_exact_original_text(revision):
    _, _, chapter, _ = revision
    chapter = chapter.model_copy(update={
        "draft": chapter.draft.model_copy(update={"inference_labels": ["现有标签"]})
    })
    proposal = FastLocalScienceRepair(inference_label_edits=[{
        "label_index": 0, "expected_text": "过期标签", "new_text": "替代标签",
        "objection_numbers": [1],
    }])
    repaired, record = _apply_local_science_items(
        chapter, proposal, {}, 1, lambda new, *_args: new, lambda *_args: None,
    )
    assert repaired == chapter
    assert record["accepted_items"] == []
    assert record["objections_without_applied_edit"] == [1]
    assert "expected_text does not match" in record["rejected_items"][0]["reason"]


def test_unchanged_inference_label_is_not_counted_as_an_applied_revision(revision):
    _, _, chapter, _ = revision
    label = "仍然错误的标签"
    chapter = chapter.model_copy(update={
        "draft": chapter.draft.model_copy(update={"inference_labels": [label]})
    })
    proposal = FastLocalScienceRepair(inference_label_edits=[{
        "label_index": 0, "expected_text": label, "new_text": label,
        "objection_numbers": [1],
    }])
    repaired, record = _apply_local_science_items(
        chapter, proposal, {}, 1, lambda new, *_args: new, lambda *_args: None,
    )
    assert repaired == chapter
    assert record["accepted_items"] == []
    assert record["objections_without_applied_edit"] == [1]
    assert "does not change" in record["rejected_items"][0]["reason"]


def test_interruption_after_proposal_freeze_reuses_writer_output(revision, monkeypatch):
    import project_ensemble.orchestration.literature_fast as fast

    runner, module, chapter, review = revision
    calls = []

    def invoke(participant, **_kwargs):
        calls.append(participant)
        return FastLocalScienceRepair(edits=[{
            "paragraph_number": 1, "new_text": "修订结果。", "objection_numbers": [1],
        }])

    runner._invoke_service = invoke
    original_apply = fast._apply_local_science_items

    def interrupt(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(fast, "_apply_local_science_items", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner._local_science_repair(module, 2, chapter, review)
    monkeypatch.setattr(fast, "_apply_local_science_items", original_apply)
    _, repaired = runner._local_science_repair(module, 2, chapter, review)
    assert repaired.draft.body_markdown.startswith("修订结果。")
    assert calls == ["WRITER"]


def test_unparseable_revision_after_schema_repair_skips_without_another_writer_call(revision):
    runner, module, chapter, review = revision
    calls = []

    def invoke(participant, **_kwargs):
        calls.append(participant)
        raise ResearchQualityControlError("LOCAL_REVISION_FORMAT_UNUSABLE", "结构修复已失败")

    runner._invoke_service = invoke
    _, repaired = runner._local_science_repair(module, 2, chapter, review)
    assert repaired == chapter
    assert audit(runner)["proposal_failure"]["science_review_required"]
    assert audit(runner)["objections_without_applied_edit"] == [1, 2, 3]
    runner._local_science_repair(module, 2, chapter, review)
    assert calls == ["WRITER"]
    assert HumanConsultationService(runner.repo).open_issues() == []


def test_local_revision_requests_nonblocking_schema_failure_and_no_fresh_generation(revision):
    runner, _, _, _ = revision
    runner.max_output_tokens = None
    runner._reader_facing_prose_contract = lambda: ""
    runner.engine.find_recorded_response = lambda *args, **kwargs: object()

    def validate(*args, **kwargs):
        assert kwargs["nonblocking_quality_failure_code"] == "LOCAL_REVISION_FORMAT_UNUSABLE"
        assert kwargs["fresh_attempts_remaining"] == 0
        return FastLocalScienceRepair()

    runner.engine.validate_structured_response = validate
    result = runner._invoke_service("WRITER", stage="local_revision", schema=FastLocalScienceRepair,
                                    system="修订", user={})
    assert result == FastLocalScienceRepair()


def test_interruption_publishing_committed_result_does_not_repeat_failed_technician(revision, monkeypatch):
    import project_ensemble.orchestration.literature_fast as fast

    runner, module, chapter, review = revision
    configure_technician(runner)
    calls = []

    def invoke(participant, **_kwargs):
        calls.append(participant)
        if participant == "TECHNICIAN":
            raise RepresentativeUnavailableError("排障服务不可用")
        return FastLocalScienceRepair(edits=[
            {"paragraph_number": 1, "new_text": "有效修改。", "objection_numbers": [1]},
            {"paragraph_number": 900, "new_text": "坏定位。", "objection_numbers": [2]},
        ])

    runner._invoke_service = invoke
    freeze = fast._freeze

    def interrupt_publish(runner, relative, payload):
        if relative.name == "writer_v2_local_patch_applied.json":
            raise KeyboardInterrupt
        return freeze(runner, relative, payload)

    monkeypatch.setattr(fast, "_freeze", interrupt_publish)
    with pytest.raises(KeyboardInterrupt):
        runner._local_science_repair(module, 2, chapter, review)
    monkeypatch.setattr(fast, "_freeze", freeze)
    _, repaired = runner._local_science_repair(module, 2, chapter, review)
    assert repaired.draft.body_markdown == "有效修改。\n\n第二段。"
    assert calls == ["WRITER", "TECHNICIAN"]


def test_legacy_technical_consultation_shows_chinese_stage_and_actual_reason(tmp_path):
    from project_ensemble import cli
    from project_ensemble.orchestration.consultations import HumanConsultationIssue
    from project_ensemble.storage.meeting import MeetingRepository

    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")], chair_model=("fake", "m"),
        governance_docs="docs/governance", task_description="修订测试",
    )
    HumanConsultationService(repo).open_issue(HumanConsultationIssue(
        issue_id="HC-FAST-SCIENCE-RM-02-LOCAL-FORMAT-V2-C2", meeting_id=repo.meeting_id,
        reason_code="FAST_LOCAL_PATCH_TECHNICAL_REPAIR_HUMAN_REQUIRED",
        stage="FAST_LOCAL_PATCH_TECHNICAL_REPAIR", question="历史格式故障",
        options=["REWRITE_WHOLE_MODULE", "PAUSE_FOR_MANUAL_REVIEW"],
        context={"last_problem": "glossary term already exists: 尺度变换"},
    ))
    output = io.StringIO()
    assert not cli._prompt_for_one_consultation(repo, input_fn=lambda _: "", output=output)
    rendered = output.getvalue()
    assert "历史修订格式故障" in rendered
    assert "具体技术原因" in rendered
    assert "glossary term already exists: 尺度变换" in rendered
    assert "FAST_LOCAL_PATCH_TECHNICAL_REPAIR" not in rendered


def test_ai_dispositions_reach_writer_without_removing_original_scientific_objections(revision):
    from project_ensemble.orchestration.consultations import HumanConsultationIssue

    runner, module, chapter, review = revision
    service = HumanConsultationService(runner.repo)
    issue_id = "HC-FAST-SCIENCE-RM-02-LOCAL-V2"
    issue = HumanConsultationIssue(
        issue_id=issue_id, meeting_id=runner.repo.meeting_id,
        reason_code="FAST_SCIENCE_REVIEW_HUMAN_REQUIRED", stage="FAST_SCIENCE_REVIEW",
        question="修订方式？", options=["RETRY_WRITER_LOCAL_REPAIR", "KEEP_PAUSED"],
        context={"module_id": module.module_id, "recheck_path": str(review.relative_to(runner.repo.root))},
    )
    service.open_issue(issue)
    auth = f"human_private/consultations/{issue_id}.ai_authorization.json"
    runner.repo.docs.write_once(auth, json.dumps({
        "meeting_id": runner.repo.meeting_id, "issue_id": issue_id, "authorized_by": "HUMAN",
        "scope": "THIS_CONSULTATION_ONLY", "allowed_decisions": ["RETRY_WRITER_LOCAL_REPAIR"],
        "science_recheck_required": True,
    }))
    ruling = {"decision": "RETRY_WRITER_LOCAL_REPAIR", "rationale": "原异议需逐条回应。",
              "objections": [{"objection_number": 1, "treatment": "NOT_ADOPT", "rationale": "已有依据。"}]}
    runner.repo.docs.write_once(f"public/procedural_consultations/{issue_id}.ai_science_decision.json", json.dumps(ruling))
    service._resolve(issue_id=issue_id, decision="RETRY_WRITER_LOCAL_REPAIR", rationale="原异议需逐条回应。",
                     scope="THIS_CONSULTATION_ONLY", authority="DELEGATED_AI", authorization_record_path=auth)

    def invoke(_participant, **kwargs):
        user = kwargs["user"]
        assert user["current_draft"]["ai_science_rulings"] == [ruling]
        assert len(user["numbered_objections"]) == 3
        return FastLocalScienceRepair(objection_responses=[{"objection_number": 1, "response": "已有依据。"}])

    runner._invoke_service = invoke
    runner._local_science_repair(module, 2, chapter, review)
    assert audit(runner)["objections"] == ["异议一", "异议二", "异议三"]
