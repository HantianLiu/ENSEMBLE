import hashlib
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_ensemble import cli
from project_ensemble.audit.lightweight import ModelReader, run_lightweight_audit
from project_ensemble.config import GovernanceConfig, ProviderConfig
from project_ensemble.domain import GenerationResponse, ModelDescriptor
from project_ensemble.providers.base import ProviderAdapter
from project_ensemble.startup import TerminalWizard
from project_ensemble.storage.events import HashChainEventLog


def _write(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    path.write_text(value, encoding="utf-8")
    return path


def _meeting(root, *, archived=False, complete=True, meeting_id="LR-A1B2C3D4"):
    _write(root, "public/meeting_manifest.json", {
        "meeting_id": meeting_id, "title": "核验测试", "deliverable_type": "literature_review",
    })
    _write(root, "public/task.json", {"description": "说明结论及证据。"})
    source = _write(root, "public/final/literature_review_report.md",
                    "# 核验测试\n\n完整正文引用 [1]。\n\n## 参考文献\n\n[1] 原始文献。\n")
    if archived:
        _write(root, "public/archive_manifest.json", {
            "status": "ARCHIVED", "meeting_id": meeting_id,
            "retained_document_path": source.relative_to(root).as_posix(),
            "retained_file_sha256": {source.relative_to(root).as_posix(): hashlib.sha256(source.read_bytes()).hexdigest()},
        })
    elif complete:
        _write(root, "public/literature_report/execution_result.json", {"status": "HANDOFF_READY"})
        log = HashChainEventLog(root / "governance_private/events.jsonl")
        log.append("TEST_COMPLETE", {}, actor="test")
    return source


def _tree(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _by_code(result):
    return {item["code"]: item for item in result["checks"]}


def _ballot(root):
    _write(root, "identity_private/representative_registry.json",
           [{"representative_id": "R-AA"}, {"representative_id": "R-BB"}])
    base = "governance_private/general_principle/ratification"
    frozen = _write(root, base + "/frozen_ballot.json", {
        "ballot_id": "B-1", "status": "CLOSED", "kind": "general_ratification",
        "options": ["YES", "NO"], "eligible_count": 2,
        "eligible_representatives": ["R-AA", "R-BB"], "required_yes_votes": 2,
        "votes": {"R-AA": "YES", "R-BB": "NO"}, "tally": {"YES": 1, "NO": 1},
    })
    for rid, choice in (("R-AA", "YES"), ("R-BB", "NO")):
        _write(root, base + f"/votes/{rid}.json", {
            "ballot_id": "B-1", "representative_id": rid, "choice": choice,
            "opposition_reason": "具体理由" if choice == "NO" else None,
        })
    _write(root, base + "/general_principle_ratification_failed.json", {
        "ballot_id": "B-1", "passed": False, "tally": {"YES": 1, "NO": 1},
    })
    return frozen


class Reader(ProviderAdapter):
    provider_id = "fake"
    def __init__(self, callback=None):
        self.requests = []
        self.callback = callback
    def list_models(self):
        return []
    def generate(self, request):
        self.requests.append(request)
        if self.callback:
            return self.callback(request)
        source_sha = re.search(r"正文 SHA-256：([a-f0-9]{64})", request.user_text).group(1)
        return GenerationResponse(provider_id=self.provider_id, model_id=request.model_id, text=json.dumps({
            "source_sha256": source_sha, "summary": "在给定范围内可读。",
            "checks": [{"name": name, "status": "PASS", "explanation": "给定材料可读。",
                        "quote": "", "suggestion": ""} for name in (
                            "TASK_ALIGNMENT", "SELF_CONTAINED", "CONSISTENCY", "PRESENTATION")],
        }, ensure_ascii=False))


def test_archived_audit_skips_private_history_and_never_modifies_source(tmp_path):
    root = tmp_path / "archive"
    _meeting(root, archived=True)
    _write(root, "governance_private/ballots/frozen_ballot.json", "this must not be read")
    before = _tree(root)
    result = run_lightweight_audit(root, tmp_path / "audit")
    checks = _by_code(result)
    assert checks["INTEGRITY"]["status"] == "PASS"
    assert checks["CITATIONS"]["status"] == "PASS"
    assert all(checks[code]["status"] == "UNAVAILABLE" for code in ("BALLOTS", "APPLICATION", "SCIENCE", "PROCESS"))
    assert result["source_state"] == "ARCHIVED"
    assert not any(item["path"].startswith("governance_private") for item in result["inputs"])
    assert _tree(root) == before
    assert "不改变科学结论" in (tmp_path / "audit/AUDIT_REPORT.md").read_text()


def test_completed_audit_recomputes_votes_receipts_and_announced_result(tmp_path):
    root = tmp_path / "completed"
    _meeting(root)
    _ballot(root)
    before = _tree(root)
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)["BALLOTS"]["status"] == "PASS"
    assert _by_code(result)["BALLOTS"]["coverage"]["ballots_checked"] == 1
    assert _tree(root) == before


@pytest.mark.parametrize("corruption", ["tally", "receipt", "duplicate", "decision"])
def test_ballot_inconsistencies_are_reported_without_reopening(tmp_path, corruption):
    root = tmp_path / "completed"
    _meeting(root)
    frozen = _ballot(root)
    base = frozen.parent
    if corruption == "tally":
        record = json.loads(frozen.read_text())
        record["tally"] = {"YES": 2, "NO": 0}
        frozen.write_text(json.dumps(record))
    elif corruption == "receipt":
        _write(root, str((base / "votes/R-AA.json").relative_to(root)), {
            "ballot_id": "B-1", "representative_id": "R-AA", "choice": "NO", "opposition_reason": "不同的票",
        })
    elif corruption == "duplicate":
        _write(root, str((base / "recovered_votes/R-AA/vote_001.json").relative_to(root)), {
            "ballot_id": "B-1", "representative_id": "R-AA", "choice": "YES", "opposition_reason": None,
        })
    else:
        _write(root, str((base / "general_principle_ratification_failed.json").relative_to(root)), {
            "ballot_id": "B-1", "passed": True, "tally": {"YES": 1, "NO": 1},
        })
    before = _tree(root)
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)["BALLOTS"]["status"] == "ISSUE"
    assert _tree(root) == before


def test_unfinished_source_never_reads_private_ballots_or_invokes_models(tmp_path):
    root = tmp_path / "unfinished"
    _meeting(root, complete=False)
    _ballot(root)
    _write(root, "governance_private/events.jsonl", "secret unfinished trace")
    adapter = Reader()
    result = run_lightweight_audit(root, tmp_path / "audit", readers=(ModelReader(adapter=adapter, model_id="reader"),))
    assert result["source_state"] == "UNFINISHED"
    assert _by_code(result)["BALLOTS"]["status"] == "UNAVAILABLE"
    assert not any(item["path"].startswith(("governance_private", "identity_private")) for item in result["inputs"])
    assert not adapter.requests


def test_legacy_noop_patch_and_false_technician_completion_are_visible(tmp_path):
    root = tmp_path / "completed"
    _meeting(root)
    _write(root, "public/literature_report/modules/RM-05/writing_v071/writer_v20_item_revision_result.json", {
        "application_audit": {"source_sha256": "a" * 64, "result_sha256": "a" * 64,
            "objections": ["still unresolved"], "accepted_items": [],
            "objections_without_applied_edit": [1],
            "technician_item_repair": {"status": "COMPLETED", "omitted_item_count": 1}},
    })
    result = run_lightweight_audit(root, tmp_path / "audit")
    checks = _by_code(result)
    assert checks["PROCESS"]["status"] == checks["APPLICATION"]["status"] == "ISSUE"
    assert checks["SCIENCE"]["status"] == "UNAVAILABLE"
    assert "当前终稿" in str(checks["PROCESS"]["coverage"])


def test_broken_archive_is_an_issue_not_an_automatic_repair(tmp_path):
    root = tmp_path / "archive"
    source = _meeting(root, archived=True)
    source.write_text("modified original")
    before = _tree(root)
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)["INTEGRITY"]["status"] == "ISSUE"
    assert _tree(root) == before


def test_reader_is_independent_and_receives_no_sealed_votes_or_private_reasoning(tmp_path):
    root = tmp_path / "completed"
    _meeting(root)
    _ballot(root)
    _write(root, "audit_private/model_calls/secret.json", "DO_NOT_SEND_PRIVATE_REASONING")
    first, second = Reader(), Reader()
    result = run_lightweight_audit(root, tmp_path / "audit", readers=(
        ModelReader(adapter=first, model_id="one"), ModelReader(adapter=second, model_id="two"),
    ))
    assert len(first.requests) == len(second.requests) == 1
    for request in first.requests + second.requests:
        assert "DO_NOT_SEND_PRIVATE_REASONING" not in request.user_text
        assert "R-AA" not in request.user_text
        assert "不同的票" not in request.user_text
    assert len(result["model_readings"]) == 2
    assert _by_code(result)["READABILITY"]["status"] == "PASS"


def test_bad_model_output_has_one_request_and_preserves_mechanical_report(tmp_path):
    root = tmp_path / "archive"
    _meeting(root, archived=True)
    adapter = Reader(lambda request: GenerationResponse(provider_id="fake", model_id="bad", text="not json"))
    result = run_lightweight_audit(root, tmp_path / "audit", readers=(ModelReader(adapter=adapter, model_id="bad"),))
    assert len(adapter.requests) == 1
    assert _by_code(result)["READABILITY"]["status"] == "UNAVAILABLE"
    assert _by_code(result)["INTEGRITY"]["status"] == "PASS"
    assert (tmp_path / "audit/AUDIT_REPORT.json").is_file()


def test_truncated_review_cannot_certify_whole_report(tmp_path):
    root = tmp_path / "completed"
    source = _meeting(root)
    source.write_text("# 标题\n\n" + "很多文字。" * 1000)
    adapter = Reader()
    result = run_lightweight_audit(root, tmp_path / "audit", readers=(
        ModelReader(adapter=adapter, model_id="reader", max_review_chars=1000),
    ))
    assert result["model_readings"][0]["coverage"]["truncated"]
    assert _by_code(result)["READABILITY"]["status"] == "UNAVAILABLE"
    assert len(adapter.requests) == 1


def test_citations_do_not_accept_body_paragraph_as_bibliography(tmp_path):
    root = tmp_path / "completed"
    source = _meeting(root)
    source.write_text("# 标题\n\n[3] 正文开头引用但没有书目。\n内部标记 [C00001-0001]。\n")
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)["CITATIONS"]["status"] == "ISSUE"
    assert _by_code(result)["CITATIONS"]["findings"]


def test_interrupt_during_ai_keeps_pre_model_checkpoint(tmp_path):
    root = tmp_path / "archive"
    _meeting(root, archived=True)
    def interrupt(request):
        raise KeyboardInterrupt()
    adapter = Reader(interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_lightweight_audit(root, tmp_path / "audit", readers=(ModelReader(adapter=adapter, model_id="reader"),))
    result = json.loads((tmp_path / "audit/AUDIT_REPORT.json").read_text())
    assert _by_code(result)["INTEGRITY"]["status"] == "PASS"
    assert _by_code(result)["READABILITY"]["status"] == "UNAVAILABLE"


def test_source_changes_during_review_are_not_certified_as_current(tmp_path):
    root = tmp_path / "completed"
    source = _meeting(root)
    adapter = Reader()
    ordinary_generate = adapter.generate
    def change(request):
        response = ordinary_generate(request)
        source.write_text("a newer version")
        return response
    adapter.generate = change
    result = run_lightweight_audit(root, tmp_path / "audit", readers=(ModelReader(adapter=adapter, model_id="reader"),))
    assert any(check["code"] == "INTEGRITY" and check["status"] == "UNAVAILABLE" for check in result["checks"])


def test_cli_meeting_list_is_first_and_supports_multi_selection(tmp_path, monkeypatch, capsys):
    roots = [tmp_path / f"meeting-{index}" for index in range(3)]
    for index, root in enumerate(roots):
        _meeting(root, archived=index == 0, complete=index != 2, meeting_id=f"LR-0000000{index}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_available_meeting_paths", lambda config: roots)
    answers = iter(["1，2-3"])
    wizard = TerminalWizard(input_fn=lambda prompt: next(answers), output=io.StringIO(), color=False)
    monkeypatch.setattr(cli, "TerminalWizard", lambda: wizard)
    args = SimpleNamespace(config=None, meeting=None, model=None, no_ai=True, output=None)
    assert cli.cmd_audit(args) == 1  # Missing/private history is explicitly partial.
    folder = next(tmp_path.glob("ensemble_audit_*"))
    manifest = json.loads((folder / "audit_manifest.json").read_text())
    assert len(manifest["results"]) == 3
    assert [item["source_state"] for item in manifest["results"]] == ["ARCHIVED", "COMPLETED", "UNFINISHED"]
    assert "源会议完整位置" in capsys.readouterr().out


def test_home_audit_returns_to_menu(tmp_path, monkeypatch):
    choices = iter(["audit", "quit"])
    class Wizard:
        def show_home(self):
            pass
        def _choose_one(self, title, options):
            assert any(value == "audit" for value, label in options)
            return next(choices)
    calls = []
    monkeypatch.setattr(cli, "TerminalWizard", Wizard)
    monkeypatch.setattr(cli, "cmd_audit", lambda args: calls.append(args) or 1)
    assert cli.cmd_home(SimpleNamespace(config=None)) == 0
    assert len(calls) == 1


def test_outputs_inside_source_are_rejected_before_writes(tmp_path):
    root = tmp_path / "archive"
    _meeting(root, archived=True)
    before = _tree(root)
    with pytest.raises(ValueError, match="之外"):
        run_lightweight_audit(root, root / "audit")
    assert _tree(root) == before


def test_open_ballot_is_never_copied_or_revealed_even_in_completed_meeting(tmp_path):
    root = tmp_path / "completed"
    _meeting(root)
    _write(root, "governance_private/unfinished/frozen_ballot.json", {
        "status": "OPEN", "votes": {"R-AA": "DO_NOT_DISCLOSE_PARTIAL_VOTE"},
    })
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)["BALLOTS"]["status"] == "UNAVAILABLE"
    assert not any("frozen_ballot" in item["path"] for item in result["inputs"])
    assert "DO_NOT_DISCLOSE_PARTIAL_VOTE" not in (tmp_path / "audit/AUDIT_REPORT.json").read_text()


@pytest.mark.parametrize("changed_delivery", [False, True])
def test_revision_object_must_match_actual_versioned_delivery(tmp_path, changed_delivery):
    root = tmp_path / "completed"
    _meeting(root)
    chapter = {"draft": {"body_markdown": "corrected body", "inference_labels": ["corrected"]}}
    base = "public/literature_report/modules/RM-05/writing_v071"
    _write(root, base + "/writer_v20_item_revision_result.json", {
        "chapter": chapter, "application_audit": {
            "source_sha256": "a" * 64, "result_sha256": "b" * 64,
            "objections": ["prior concern"], "accepted_items": [{"group": "inference_labels"}],
        },
    })
    _write(root, base + "/writer_v20_validated.json",
           {"draft": {"body_markdown": "old body"}} if changed_delivery else chapter)
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)["APPLICATION"]["status"] == ("ISSUE" if changed_delivery else "PASS")


def _science_vote(root, version, *, passed=True):
    _write(root, f"public/literature_report/modules/RM-05/writing_v071/writer_v{version}_validated.json",
           {"draft": {"body_markdown": "body"}})
    return _write(root, f"public/literature_report/fast/RM-05/science_recheck_v{version}.json", {
        "passed": passed, "yes": 1 if passed else 0, "eligible": 1, "required_yes": 1,
        "votes": [{"reviewer_id": "R-AA", "resolved": passed,
                   "remaining_material_problems": [] if passed else ["具体问题"]}],
    })


def test_old_unresolved_science_is_not_current_failure_after_later_pass(tmp_path):
    root = tmp_path / "completed"
    _meeting(root)
    _science_vote(root, 2, passed=False)
    _science_vote(root, 3)
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)["SCIENCE"]["status"] == "PASS"
    assert _by_code(result)["SCIENCE"]["coverage"]["latest_dispositions"][0]["version"] == 3


@pytest.mark.parametrize("corruption", ["count", "contradiction", "duplicate", "version"])
def test_science_recheck_checks_counts_semantics_and_delivery_version(tmp_path, corruption):
    root = tmp_path / "completed"
    _meeting(root)
    path = _science_vote(root, 3)
    record = json.loads(path.read_text())
    if corruption == "count":
        record["yes"] = 0
    elif corruption == "contradiction":
        record["votes"][0]["remaining_material_problems"] = ["还没解决"]
    elif corruption == "duplicate":
        record["votes"].append(record["votes"][0])
    else:
        _write(root, "public/literature_report/modules/RM-05/writing_v071/writer_v4_validated.json", {"draft": {}})
    path.write_text(json.dumps(record))
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)["SCIENCE"]["status"] == ("UNAVAILABLE" if corruption == "version" else "ISSUE")


def test_reasoned_rejection_is_not_automatically_a_revision_loop(tmp_path):
    root = tmp_path / "completed"
    _meeting(root)
    _write(root, "public/literature_report/modules/RM-05/writing_v071/writer_v2_item_revision_result.json", {
        "application_audit": {
            "source_sha256": "a" * 64, "result_sha256": "a" * 64, "objections": ["建议"],
            "objections_without_applied_edit": [1], "objection_responses": [{"reason": "有依据不采纳"}],
        },
    })
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)["PROCESS"]["status"] == "UNAVAILABLE"


def test_public_ratification_must_match_frozen_votes(tmp_path):
    root = tmp_path / "completed"
    _meeting(root)
    _ballot(root)
    _write(root, "public/general_principle/ratification.json", {
        "ballot_id": "B-1", "passed": True, "tally": {"YES": 1, "NO": 1},
    })
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)["BALLOTS"]["status"] == "ISSUE"


def test_evidence_appeal_thats_passed_requires_explanations(tmp_path):
    root = tmp_path / "completed"
    _meeting(root)
    path = _science_vote(root, 3)
    _write(root, "public/literature_report/fast/RM-05/science_evidence_appeal_v3.json",
           json.loads(path.read_text()))
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)["SCIENCE"]["status"] == "ISSUE"


def test_large_event_chain_is_coverage_limit_not_corruption(tmp_path):
    root = tmp_path / "completed"
    _meeting(root)
    log = root / "governance_private/events.jsonl"
    with log.open("wb") as stream:
        stream.truncate(16_000_001)
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)["INTEGRITY"]["status"] == "UNAVAILABLE"


def test_cli_selects_meeting_before_ai_and_runs_fresh_reader(tmp_path, monkeypatch):
    root = tmp_path / "meeting"
    _meeting(root, archived=True)
    before = _tree(root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_available_meeting_paths", lambda config: [root])
    monkeypatch.setattr(cli, "_config_path", lambda requested: "test-config")
    cfg = SimpleNamespace(providers={"fake": ProviderConfig(
        kind="openai_compatible", base_url="https://example.invalid", api_key_env="FAKE_KEY",
    )}, governance=GovernanceConfig())
    monkeypatch.setattr(cli, "_load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "discover_models", lambda cfg, providers: [
        ModelDescriptor(provider_id="fake", model_id="reader", display_name="reader"),
    ])
    adapter = Reader()
    monkeypatch.setattr(cli, "_load_adapter_for_provider", lambda cfg, provider: adapter)
    answers = iter(["1", "1", "1", "1", "1"])
    wizard = TerminalWizard(input_fn=lambda prompt: next(answers), output=io.StringIO(), color=False)
    monkeypatch.setattr(cli, "TerminalWizard", lambda: wizard)
    args = SimpleNamespace(config=None, meeting=None, model=None, no_ai=False, output=None)
    assert cli.cmd_audit(args) == 1  # Archived private history cannot be certified.
    folder = next(tmp_path.glob("ensemble_audit_*"))
    report = next(folder.glob("*/AUDIT_REPORT.json"))
    result = json.loads(report.read_text())
    assert _by_code(result)["READABILITY"]["status"] == "PASS"
    assert len(adapter.requests) == 1
    assert _tree(root) == before


def test_model_catalog_failure_offers_mechanical_fallback(tmp_path, monkeypatch):
    root = tmp_path / "meeting"
    _meeting(root, archived=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_available_meeting_paths", lambda config: [root])
    monkeypatch.setattr(cli, "_config_path", lambda requested: "test-config")
    cfg = SimpleNamespace(providers={"fake": ProviderConfig(
        kind="openai_compatible", base_url="https://example.invalid", api_key_env="FAKE_KEY",
    )})
    monkeypatch.setattr(cli, "_load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "discover_models", lambda cfg, providers: [])
    answers = iter(["1", "1", "1", "1"])  # Meeting, AI mode, provider, mechanical fallback.
    wizard = TerminalWizard(input_fn=lambda prompt: next(answers), output=io.StringIO(), color=False)
    monkeypatch.setattr(cli, "TerminalWizard", lambda: wizard)
    assert cli.cmd_audit(SimpleNamespace(config=None, meeting=None, model=None, no_ai=False, output=None)) == 1
    folder = next(tmp_path.glob("ensemble_audit_*"))
    result = json.loads(next(folder.glob("*/AUDIT_REPORT.json")).read_text())
    assert _by_code(result)["CITATIONS"]["status"] == "PASS"
    assert _by_code(result)["READABILITY"]["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("corruption", ["event", "metadata"])
def test_malformed_record_does_not_discard_other_mechanical_checks(tmp_path, corruption):
    root = tmp_path / "completed"
    _meeting(root)
    if corruption == "event":
        _write(root, "governance_private/events.jsonl", "null\n")
        failed_code = "INTEGRITY"
    else:
        _write(root, "public/literature_report/modules/RM-05/writing_v071/writer_v1_validated.json",
               {"draft": ["invalid metadata"]})
        failed_code = "READABILITY"
    before = _tree(root)
    result = run_lightweight_audit(root, tmp_path / "audit")
    assert _by_code(result)[failed_code]["status"] == "ISSUE"
    assert _by_code(result)["CITATIONS"]["status"] == "PASS"
    assert (tmp_path / "audit/AUDIT_REPORT.md").is_file()
    assert _tree(root) == before
