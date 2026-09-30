import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from project_ensemble.domain import GenerationResponse
from project_ensemble.orchestration.post_meeting_accountability import (
    PostMeetingAccountabilityRunner,
    PostMeetingSubmission,
)
from project_ensemble.storage.meeting import MeetingRepository


class _Progress:
    def __init__(self):
        self.speeches = []

    def status(self, *_args):
        pass

    def info(self, *_args):
        pass

    def speech(self, *_args):
        self.speeches.append(_args)


class _Engine:
    def __init__(self):
        self.status = SimpleNamespace(phase=None, paused_reason=None)
        self.progress = _Progress()
        self.calls: list[dict] = []

    def find_recorded_response(self, *_args, **_kwargs):
        return None

    def invoke_participant(self, participant_id, *, system_text, user_text, stage, **_kwargs):
        self.calls.append(
            {
                "participant_id": participant_id,
                "system_text": system_text,
                "user_text": user_text,
                "stage": stage,
            }
        )
        if participant_id == "CHAIR":
            text = json.dumps(
                {
                    "cli_brief": "会议完成；1 名代表请求审计。",
                    "full_report_markdown": (
                        "# 最终述职\n\n会议完成。\n\n"
                        "## R-AAAAAA\n贡献稳定。\n\n"
                        "## R-BBBBBB\n提出程序审计申请。"
                    ),
                },
                ensure_ascii=False,
            )
        elif participant_id == "R-AAAAAA":
            text = json.dumps(
                {
                    "representative_id": participant_id,
                    "requests_audit": False,
                    "audit_petitions": [],
                    "minority_report": None,
                }
            )
        else:
            text = json.dumps(
                {
                    "representative_id": participant_id,
                    "requests_audit": True,
                    "audit_petitions": [
                        {
                            "involved_procedure": "status transition",
                            "observed_problem": "The trial cutoff may suppress continuity.",
                            "why_procedural_not_substantive": "It concerns participation rights.",
                            "institutional_consequence": "Later review may lose context.",
                            "references": ["public/general_principle/status_transition.json"],
                        }
                    ],
                    "minority_report": {
                        "preserved_dissent": "One uncertainty remains.",
                        "alternative": "Retain an explicit unresolved marker.",
                        "predicted_failure_mode": "Execution may overstate certainty.",
                        "suggested_future_test": "Compare both interpretations.",
                        "evidence_refs": ["final_report.md §正文"],
                    },
                }
            )
        return GenerationResponse(text=text, provider_id="fake", model_id="fake")

    def validate_structured_response(self, _participant_id, *, response, schema_model, **_kwargs):
        return schema_model.model_validate_json(response.text)

    def pause_for_unconfigured_policy(self, **_kwargs):
        raise AssertionError("valid fixture must not pause")


def _meeting(tmp_path: Path) -> tuple[MeetingRepository, Path, Path]:
    repo = MeetingRepository(tmp_path / "M-ACCOUNT")
    repo.docs.write_once(
        "public/meeting_manifest.json",
        json.dumps(
            {
                "meeting_id": "M-ACCOUNT",
                "created_at": "2026-09-19T00:00:00+00:00",
                "meeting_type": "deliberation",
                "participant_count": 2,
                "representative_count": 2,
                "research_enabled": False,
            }
        ),
    )
    registry = [
        {
            "representative_id": "R-AAAAAA",
            "runtime": {
                "provider_id": "provider-a",
                "model_id": "model-a",
                "persona": "systems_integrator",
            },
        },
        {
            "representative_id": "R-BBBBBB",
            "runtime": {
                "provider_id": "provider-b",
                "model_id": "model-b",
                "persona": "librarian",
            },
        },
    ]
    repo.docs.write_once("identity_private/representative_registry.json", json.dumps(registry))
    transition = {
        "meeting_id": "M-ACCOUNT",
        "primary_drafter_id": "R-AAAAAA",
        "representative_statuses": {
            "R-AAAAAA": "ACTIVE",
            "R-BBBBBB": "CONSULTATIVE",
        },
    }
    repo.docs.write_once(
        "identity_private/general_principle/status_transition.json",
        json.dumps(transition),
    )
    repo.docs.write_once(
        "public/general_principle/status_transition.json",
        json.dumps(
            {
                "meeting_id": "M-ACCOUNT",
                "representative_statuses": transition["representative_statuses"],
            }
        ),
    )
    repo.docs.write_once("public/task.json", '{"description":"test task"}')
    repo.docs.write_once(
        "public/final/chair_procedural_certification.json",
        '{"status":"CERTIFIED"}',
    )
    repo.docs.write_once(
        "public/final/think_tank_epistemic_reviews.json",
        '{"reviews":[]}',
    )
    repo.docs.write_once(
        "public/final/think_tank_execution_reviews.json",
        '{"reviews":[]}',
    )
    final_report = repo.docs.write_once(
        "public/final/final_report.md",
        "# Final result\n\nThe certified result is complete.\n",
    )
    publication_manifest = repo.docs.write_once(
        "public/final/final_publication_manifest.json",
        '{"status":"FROZEN","report_pdf_path":"public/final/final_report.pdf"}',
    )
    return repo, final_report, publication_manifest


def test_audit_request_boolean_must_match_petition_list():
    with pytest.raises(ValidationError, match="must exactly match"):
        PostMeetingSubmission.model_validate(
            {
                "representative_id": "R-AAAAAA",
                "requests_audit": False,
                "audit_petitions": [
                    {
                        "involved_procedure": "vote",
                        "observed_problem": "problem",
                        "why_procedural_not_substantive": "procedure",
                        "institutional_consequence": "consequence",
                        "references": ["record"],
                    }
                ],
                "minority_report": None,
            }
        )


def test_post_meeting_context_view_omits_only_redundant_literature_appendix(
    tmp_path,
):
    repo, _final_report, _publication_manifest = _meeting(tmp_path)
    runner = PostMeetingAccountabilityRunner(
        repo=repo,
        engine=_Engine(),
        governance_docs=Path(__file__).resolve().parents[1] / "docs/governance",
    )
    source_path = repo.docs.write_once(
        "public/final/context_source.md",
        (
            "# 正文\n\n认证正文。\n\n"
            "# 附录 A：独立知识完整性审查\n\n知识审查。\n\n"
            "# 附录 B：独立执行移交审查\n\n执行审查。\n\n"
            "# 附录 C：会议文献证据与参考文献\n\n"
            + "RP-TEST 文献条目。\n" * 1000
            + "\n# 附录 D：来源与完整性\n\n来源哈希。\n"
        ),
    )
    original = source_path.read_text(encoding="utf-8")

    view_path = runner._post_meeting_representative_view(source_path)
    view = view_path.read_text(encoding="utf-8")

    assert source_path.read_text(encoding="utf-8") == original
    assert "认证正文" in view
    assert "知识审查" in view and "执行审查" in view
    assert "来源哈希" in view
    assert "RP-TEST 文献条目" not in view
    assert "省略不表示证据不存在" in view
    assert len(view) < len(original) // 4
    provenance = json.loads(
        (
            repo.root
            / "public/final/post_meeting_representative_view.provenance.json"
        ).read_text(encoding="utf-8")
    )
    assert provenance["source_path"] == "public/final/context_source.md"
    assert provenance["authority"] == (
        "DERIVED_CONTEXT_VIEW_ONLY_FINAL_PUBLICATION_UNCHANGED"
    )
    assert provenance["omitted_character_count"] > 10_000

    assert runner._post_meeting_representative_view(source_path) == view_path
    event_types = [
        json.loads(line)["event_type"]
        for line in repo.events.path.read_text(encoding="utf-8").splitlines()
    ]
    assert event_types.count("POST_MEETING_REPRESENTATIVE_CONTEXT_VIEW_FROZEN") == 1


def test_compact_context_reuses_response_recorded_with_complete_report(tmp_path):
    repo, _final_report, _publication_manifest = _meeting(tmp_path)

    class RecoveryEngine(_Engine):
        def find_recorded_response(
            self, participant_id, *, system_text, user_text, stage
        ):
            if "LEGACY-EVIDENCE-ENTRY" not in system_text:
                return None
            return GenerationResponse(
                text=json.dumps(
                    {
                        "representative_id": participant_id,
                        "requests_audit": False,
                        "audit_petitions": [],
                        "minority_report": None,
                    }
                ),
                provider_id="provider-a",
                model_id="model-a",
            )

        def invoke_participant(self, *_args, **_kwargs):
            raise AssertionError("the completed legacy response must be reused")

    source_path = repo.docs.write_once(
        "public/final/recovery_source.md",
        (
            "# 正文\n\n认证正文。\n\n"
            "# 附录 A：独立知识完整性审查\n\n知识审查。\n\n"
            "# 附录 B：独立执行移交审查\n\n执行审查。\n\n"
            "# 附录 C：会议文献证据与参考文献\n\nLEGACY-EVIDENCE-ENTRY\n\n"
            "# 附录 D：来源与完整性\n\n来源哈希。\n"
        ),
    )
    engine = RecoveryEngine()
    runner = PostMeetingAccountabilityRunner(
        repo=repo,
        engine=engine,
        governance_docs=Path(__file__).resolve().parents[1] / "docs/governance",
    )
    view_path = runner._post_meeting_representative_view(source_path)
    record = json.loads(
        repo.docs.read_text("identity_private/representative_registry.json")
    )[0]

    representative_id, submission = runner._collect_one_submission(
        record=record,
        current_status="ACTIVE",
        final_report_path=source_path,
        representative_view_path=view_path,
    )

    assert representative_id == "R-AAAAAA"
    assert submission.requests_audit is False
    assert engine.calls == []


def test_post_meeting_window_freezes_requests_then_chair_debriefs(tmp_path):
    repo, final_report, publication_manifest = _meeting(tmp_path)
    engine = _Engine()
    runner = PostMeetingAccountabilityRunner(
        repo=repo,
        engine=engine,
        governance_docs=Path(__file__).resolve().parents[1] / "docs/governance",
    )

    result = runner.run(
        final_report_path=final_report,
        publication_manifest_path=publication_manifest,
    )

    assert result.minority_report_count == 1
    assert result.audit_requester_count == 1
    assert result.audit_petition_count == 1
    petitions = json.loads(
        (repo.root / result.audit_petition_bundle_path).read_text(encoding="utf-8")
    )
    assert petitions["submission_count"] == 2
    assert petitions["requester_ids"] == ["R-BBBBBB"]

    report = (repo.root / result.chair_accountability_report_path).read_text(
        encoding="utf-8"
    )
    assert "R-AAAAAA" in report and "R-BBBBBB" in report
    assert "provider-a:model-a" in report
    assert "Chair 未读取真实模型/人格映射" in report
    assert "Builder / 建构者" in report
    brief = (
        repo.root / result.chair_accountability_cli_brief_path
    ).read_text(encoding="utf-8")
    assert brief.strip() == "会议完成；1 名代表请求审计。"
    assert engine.progress.speeches[-1][2] == "会议完成；1 名代表请求审计。"
    assert (repo.root / "CHAIR_BRIEF.txt").is_symlink()
    assert (repo.root / "MEETING_RESULTS.md").exists()

    representative_calls = [
        call for call in engine.calls if call["participant_id"] != "CHAIR"
    ]
    assert all("provider-a" not in call["system_text"] for call in representative_calls)
    chair_call = next(call for call in engine.calls if call["participant_id"] == "CHAIR")
    assert "provider-a" not in chair_call["user_text"]
    assert "model-a" not in chair_call["user_text"]

    events = [
        json.loads(line)["event_type"]
        for line in (repo.root / "governance_private/events.jsonl").read_text().splitlines()
    ]
    assert events.index("POST_MEETING_MINORITY_MATERIAL_FROZEN") < events.index(
        "POST_MEETING_AUDIT_PETITIONS_FROZEN"
    ) < events.index("CHAIR_FINAL_ACCOUNTABILITY_REPORT_FROZEN")

    # Frozen submissions and debrief are reused without repeating model calls.
    runner.run(
        final_report_path=final_report,
        publication_manifest_path=publication_manifest,
    )
    assert len(engine.calls) == 3


def test_post_meeting_scheduler_serializes_each_model_lane_and_parallelizes_models(
    tmp_path, monkeypatch
):
    repo, final_report, _publication_manifest = _meeting(tmp_path)
    registry = [
        {
            "representative_id": representative_id,
            "runtime": {
                "provider_id": "provider-a",
                "model_id": model_id,
                "persona": persona,
            },
        }
        for representative_id, model_id, persona in (
            ("R-AAAAAA", "model-a", "systems_integrator"),
            ("R-BBBBBB", "model-a", "pragmatic_minimalist"),
            ("R-CCCCCC", "model-b", "systems_integrator"),
            ("R-DDDDDD", "model-b", "pragmatic_minimalist"),
        )
    ]
    (repo.root / "identity_private/representative_registry.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    transition_path = repo.root / "identity_private/general_principle/status_transition.json"
    transition = json.loads(transition_path.read_text(encoding="utf-8"))
    transition["representative_statuses"] = {
        record["representative_id"]: "ACTIVE" for record in registry
    }
    transition_path.write_text(json.dumps(transition), encoding="utf-8")

    runner = PostMeetingAccountabilityRunner(
        repo=repo,
        engine=_Engine(),
        governance_docs=Path(__file__).resolve().parents[1] / "docs/governance",
    )
    guard = threading.Lock()
    active_models: set[str] = set()
    same_model_overlap: list[str] = []
    observed_order = {"model-a": [], "model-b": []}
    peak_active_models = 0

    def fake_collect(*, record, current_status, final_report_path, **_kwargs):
        nonlocal peak_active_models
        assert current_status == "ACTIVE"
        assert final_report_path == final_report
        model_id = record["runtime"]["model_id"]
        with guard:
            if model_id in active_models:
                same_model_overlap.append(model_id)
            active_models.add(model_id)
            observed_order[model_id].append(record["runtime"]["persona"])
            peak_active_models = max(peak_active_models, len(active_models))
        time.sleep(0.03)
        with guard:
            active_models.remove(model_id)
        representative_id = record["representative_id"]
        return representative_id, PostMeetingSubmission(
            representative_id=representative_id,
            requests_audit=False,
            audit_petitions=[],
            minority_report=None,
        )

    monkeypatch.setattr(runner, "_collect_one_submission", fake_collect)
    _minority_path, _petition_path, bundle = runner._collect_post_meeting_submissions(
        final_report_path=final_report
    )

    assert bundle["submission_count"] == 4
    assert not same_model_overlap
    assert peak_active_models == 2
    assert observed_order == {
        "model-a": ["systems_integrator", "pragmatic_minimalist"],
        "model-b": ["systems_integrator", "pragmatic_minimalist"],
    }


def test_post_meeting_window_persists_complete_submission_before_other_lane_fails(
    tmp_path, monkeypatch
):
    repo, final_report, _publication_manifest = _meeting(tmp_path)
    runner = PostMeetingAccountabilityRunner(
        repo=repo,
        engine=_Engine(),
        governance_docs=Path(__file__).resolve().parents[1] / "docs/governance",
    )

    def fake_collect(*, record, **_kwargs):
        representative_id = record["representative_id"]
        if representative_id == "R-BBBBBB":
            time.sleep(0.05)
            raise RuntimeError("provider failed")
        return representative_id, PostMeetingSubmission(
            representative_id=representative_id,
            requests_audit=False,
            audit_petitions=[],
            minority_report=None,
        )

    monkeypatch.setattr(runner, "_collect_one_submission", fake_collect)
    with pytest.raises(RuntimeError, match="provider failed"):
        runner._collect_post_meeting_submissions(final_report_path=final_report)

    assert (
        repo.root
        / "governance_private/post_meeting/submissions/R-AAAAAA.json"
    ).exists()
    assert not (
        repo.root
        / "governance_private/post_meeting/submissions/R-BBBBBB.json"
    ).exists()
    assert repo.events.verify()
