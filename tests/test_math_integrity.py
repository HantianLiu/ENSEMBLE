from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_ensemble.errors import ModelReplacementRequested, ProviderError
from project_ensemble.orchestration.literature_fast import FastLocalScienceRepair, FastResolutionVote
from project_ensemble.orchestration.literature_report_execution import ModuleDraft, ReaderFacingLineRepair
from project_ensemble.orchestration.literature_writing_v071 import ScienceChecklist, WriterChapter
from project_ensemble.orchestration.math_integrity import (
    FORMULA_BOOKKEEPING_RULES, audit_math_round, inspect_math_text, notation_reference_from_repo,
)
from project_ensemble.storage.documents import ImmutableDocumentStore


def runner_for(tmp_path, response='{"findings": []}', *, enabled=True):
    repo = SimpleNamespace(root=tmp_path, meeting_id="LR-MATH",
                           docs=ImmutableDocumentStore(tmp_path))
    repo.docs.write_once("identity_private/meeting_manifest.json", json.dumps(
        {"technician_model": ["configured-provider", "configured-model"] if enabled else None}))
    events, calls, guards = [], [], []
    repo.events = SimpleNamespace(append=lambda *args, **kwargs: events.append((args, kwargs)))

    @contextmanager
    def guard():
        guards.append("entered")
        try:
            yield
        finally:
            guards.append("left")

    def invoke(participant_id, **kwargs):
        calls.append((participant_id, kwargs))
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(text=response)

    engine = SimpleNamespace(find_recorded_response=lambda *args, **kwargs: None,
                             invoke_participant=invoke, recoverable_call=guard)
    runner = SimpleNamespace(repo=repo, engine=engine, max_output_tokens=None)
    return runner, calls, events, guards


def records(runner):
    return [json.loads(path.read_text()) for path in
            (runner.repo.root / "audit_private/technician/math_integrity").glob("*.json")]


def test_known_escaped_newlines_are_fixed_without_changing_commands_or_symbols():
    text = r"说明 [C1-1]。$$\n\nu=\nabla f+\beta x\\ y\n$$结论。"
    revised, findings, inventory = inspect_math_text(text)
    assert revised == "说明 [C1-1]。\n\n$$\n" + r"\nu=\nabla f+\beta x\\ y" + "\n$$\n\n结论。"
    assert not findings
    assert inventory[0]["source"] in text
    assert r"\nu" in inventory[0]["symbols"]
    assert r"\nabla" in inventory[0]["symbols"]


def test_unknown_alphabetic_commands_are_not_guessed_to_be_newlines():
    text = r"$$\nx=1\n$$"
    revised, _, _ = inspect_math_text(text)
    assert r"\nx=1" in revised


@pytest.mark.parametrize("text", [
    r"\(x=\frac{1}{2\)", r"\(x\end{aligned}\)", r"\(x", "$$\nx=1",
])
def test_incomplete_math_is_recorded_not_reconstructed(text):
    revised, findings, _ = inspect_math_text(text)
    assert revised == text
    assert findings


def test_math_code_examples_are_untouched():
    tick = chr(96)
    source = tick * 3 + "\n" + r"$$\n\beta\n$$" + "\n" + tick * 3
    assert inspect_math_text(source) == (source, [], [])
    source = tick + r"\(x" + tick
    assert inspect_math_text(source) == (source, [], [])


def test_bare_term_formula_is_checked_without_adding_delimiters():
    source = r"\frac{x}{y}"
    revised, findings, inventory = inspect_math_text(source, bare_formula=True)
    assert revised == source
    assert not findings
    assert inventory[0]["offset"] == 0
    assert inventory[0]["source"] == source
    assert inspect_math_text(r"\frac{x}{y", bare_formula=True)[1]


def test_escaped_literal_braces_and_nested_environments_are_not_false_errors():
    source = r"\(\left\{x\right.\)"
    assert not inspect_math_text(source)[1]
    source = "$$\n" + r"\begin{aligned}\begin{matrix}x\end{matrix}\end{aligned}" + "\n$$"
    assert inspect_math_text(source)[:2] == (source, [])


def test_formula_context_is_deduplicated_but_keeps_all_positions(tmp_path):
    runner, calls, _, _ = runner_for(tmp_path)
    draft = ModuleDraft(title="Title", body_markdown=r"\(x\) and \(x\)", short_summary=r"\(x\)")
    audit_math_round(runner, "WRITER", "v1", draft)
    formulas = json.loads(calls[0][1]["user_text"])["formulas"]
    assert len(formulas) == 1
    assert len(formulas[0]["locations"]) == 3


def test_intermediate_repair_is_mechanical_only_and_final_round_has_one_model_check(tmp_path):
    runner, calls, _, _ = runner_for(tmp_path)
    draft = ModuleDraft(title="Title", body_markdown=r"$$\n\beta x\n$$", short_summary="Summary")
    intermediate = audit_math_round(runner, "WRITER", "citation_repair", draft, run_model_review=False)
    assert not calls
    assert intermediate.body_markdown == "$$\n" + r"\beta x" + "\n$$"
    assert records(runner)[0]["technician_review"]["status"].startswith("MECHANICAL_ONLY")
    final = audit_math_round(runner, "WRITER", "completed_round", intermediate)
    assert final == intermediate and len(calls) == 1
    assert audit_math_round(runner, "WRITER", "completed_round", intermediate) == final
    assert len(calls) == 1


def test_large_math_input_has_a_bounded_mechanical_fallback(tmp_path):
    runner, calls, _, _ = runner_for(tmp_path)
    draft = ModuleDraft(title="Title", body_markdown=r"\(" + "x+" * 65_000 + r"x\)",
                        short_summary="Summary")
    assert audit_math_round(runner, "WRITER", "v1", draft) == draft
    assert not calls
    assert records(runner)[0]["technician_review"]["status"].startswith("INPUT_TOO_LARGE")


def test_librarian_format_notes_do_not_block_adoption_or_change_votes():
    review = ScienceChecklist(formula_format_notes=["公式的定界符需要调整"])
    assert not review.issues and not review.glossary_corrections
    vote = FastResolutionVote(resolved=True, formula_format_notes=["使用真实换行"])
    assert vote.resolved and not vote.remaining_material_problems


def test_reader_facing_line_revision_is_checked(tmp_path):
    runner, calls, _, _ = runner_for(tmp_path)
    value = ReaderFacingLineRepair(revised_text=r"$$\n\beta x\n$$", explanation="只改排版")
    result = audit_math_round(runner, "WRITER", "line_revision", value)
    assert result.revised_text == "$$\n" + r"\beta x" + "\n$$"
    assert result.explanation == value.explanation
    assert len(calls) == 1


def test_prior_glossary_versions_use_numeric_order(tmp_path):
    glossary = tmp_path / "public/literature_report/writing_v071"
    glossary.mkdir(parents=True)
    for version in (9, 20):
        (glossary / f"glossary_after_RM-01_revision_{version}.json").write_text(json.dumps([
            {"term": "相关词", "explanation": f"version {version}"}]))
    reference = notation_reference_from_repo(SimpleNamespace(root=tmp_path), "RM-02",
                                             {"text": "相关词"})
    assert reference["prior_glossary"][0]["explanation"] == "version 20"


def test_audit_runs_once_replays_and_preserves_provenance(tmp_path):
    runner, calls, events, guards = runner_for(tmp_path)
    draft = ModuleDraft(title="Title", body_markdown=r"$$\n\beta x\n$$",
                        short_summary="Uncertain conclusion.", inference_labels=["Still uncertain."])
    original = draft.model_dump(mode="json")
    repaired = audit_math_round(runner, "WRITER", "writer_RM-01_v2", draft)
    assert repaired.body_markdown == "$$\n" + r"\beta x" + "\n$$"
    assert draft.model_dump(mode="json") == original
    assert repaired.inference_labels == draft.inference_labels
    assert repaired.short_summary == draft.short_summary
    assert len(calls) == len(events) == 1
    assert guards == ["entered", "left"]
    assert calls[0][0] == "TECHNICIAN"
    assert "Uncertain conclusion." not in calls[0][1]["user_text"]
    record = records(runner)[0]
    assert record["source_sha256"] != record["result_sha256"]
    assert record["source"] == original
    assert record["technician_review"]["status"] == "COMPLETED"
    assert audit_math_round(runner, "WRITER", "writer_RM-01_v2", draft) == repaired
    assert len(calls) == len(events) == 1


@pytest.mark.parametrize("response", ['not JSON', '{"body_markdown":"changed coefficient"}',
                                     ProviderError("offline")])
def test_technician_failure_is_local_bounded_and_keeps_safe_edits(tmp_path, response):
    runner, calls, events, guards = runner_for(tmp_path, response)
    draft = ModuleDraft(title="Title", body_markdown=r"$$\n\beta x\n$$", short_summary="Unchanged")
    result = audit_math_round(runner, "WRITER", "v2", draft)
    assert result.body_markdown == "$$\n" + r"\beta x" + "\n$$"
    assert len(calls) == 1
    assert guards == ["entered", "left"]
    assert records(runner)[0]["technician_review"]["status"].startswith("FAILED")
    assert audit_math_round(runner, "WRITER", "v2", draft) == result
    assert len(calls) == 1


def test_advice_does_not_rewrite_bad_math_or_resolve_science(tmp_path):
    runner, calls, _, _ = runner_for(tmp_path, '{"findings":["括号需要确认"]}')
    draft = ModuleDraft(title="Title", body_markdown=r"\(x=\frac{1}{2\)", short_summary="Unverified")
    result = audit_math_round(runner, "WRITER", "v2", draft)
    assert result == draft
    assert records(runner)[0]["mechanical_findings"]
    assert len(calls) == 1
    vote = FastResolutionVote(resolved=False, remaining_material_problems=["Unverified"])
    assert audit_math_round(runner, "R-ONE", "review", vote) is vote
    assert len(calls) == 1


def test_without_technician_there_are_no_checks_or_mutations(tmp_path):
    runner, calls, events, _ = runner_for(tmp_path, enabled=False)
    draft = ModuleDraft(title="Title", body_markdown=r"$$x=1$$", short_summary="Summary")
    assert audit_math_round(runner, "WRITER", "v1", draft) is draft
    assert not calls and not events and not records(runner)


def test_no_math_still_records_round_without_model_call(tmp_path):
    runner, calls, events, _ = runner_for(tmp_path)
    draft = ModuleDraft(title="Title", body_markdown="No equation.", short_summary="Summary")
    assert audit_math_round(runner, "WRITER", "v1", draft) == draft
    assert not calls and len(events) == 1
    assert records(runner)[0]["technician_review"]["status"] == "NO_FORMULAS"


def test_local_revision_anchors_and_objection_replies_never_change(tmp_path):
    runner, calls, _, _ = runner_for(tmp_path)
    proposal = FastLocalScienceRepair.model_validate({
        "edits": [{"old_text": r"$$\n\beta\n$$", "new_text": r"$$\n\beta x\n$$",
                   "objection_numbers": [1]}],
        "objection_responses": [{"objection_number": 1,
                                 "response": "Definition still needs review."}],
    })
    result = audit_math_round(runner, "WRITER", "local_RM-01_v2", proposal)
    assert result.edits[0].old_text == proposal.edits[0].old_text
    assert result.edits[0].new_text == "$$\n" + r"\beta x" + "\n$$"
    assert result.objection_responses == proposal.objection_responses
    assert result.edits[0].objection_numbers == [1]
    assert len(calls) == 1


def test_human_control_is_not_swallowed(tmp_path):
    runner, _, _, _ = runner_for(tmp_path, ModelReplacementRequested("replacement requested"))
    draft = ModuleDraft(title="Title", body_markdown=r"\(x\)", short_summary="Summary")
    with pytest.raises(ModelReplacementRequested):
        audit_math_round(runner, "WRITER", "v1", draft)
    assert not records(runner)


def test_librarian_notation_is_advisory_and_legacy_reviews_still_work():
    review = ScienceChecklist.model_validate({"issues": [], "glossary_corrections": []})
    assert review.notation_bookkeeping == []
    review = ScienceChecklist.model_validate({"notation_bookkeeping": [{
        "symbol": r"\nu", "meaning": "关联长度指数", "scope": "临界现象",
        "location_excerpt": "关联长度指数保留符号 ν",
    }]})
    assert not review.issues and not review.glossary_corrections
    assert review.notation_bookkeeping[0].units == "未说明"
    assert "不凭记忆补全" in FORMULA_BOOKKEEPING_RULES


def test_prior_notation_context_is_relevant_public_and_scope_preserving(tmp_path):
    glossary = tmp_path / "public/literature_report/writing_v071"
    glossary.mkdir(parents=True)
    terms = [{"term": "关联长度", "formula": r"\(\xi\sim t^{-\nu}\)", "scope": "critical"},
             {"term": "无关术语", "formula": None}]
    (glossary / "glossary_after_RM-01.json").write_text(json.dumps(terms))
    (glossary / "glossary_after_RM-03.json").write_text(json.dumps([{"term": "Future", "formula": r"\nu"}]))
    public = tmp_path / "public/literature_report/fast/RM-01"
    public.mkdir(parents=True)
    note = {"symbol": r"\nu", "meaning": "关联长度指数", "scope": "critical"}
    (public / "science_review_v1.json").write_text(json.dumps(
        {"reviews": [{"checklist": {"notation_bookkeeping": [note]}}]}))
    # Private review material must never be read or given to another reviewer.
    private = tmp_path / "governance_private/literature_report/fast/RM-02"
    private.mkdir(parents=True)
    (private / "science_v1_other.json").write_text("This is deliberately not JSON")
    context = notation_reference_from_repo(SimpleNamespace(root=tmp_path),
        "fast_science_RM-02_v1", {"body_markdown": r"\(\nu\)", "text": "关联长度"})
    assert context["prior_glossary"] == terms[:1]
    assert context["omitted_unrelated_term_count"] == 1
    assert context["prior_review_notation_notes"][0]["records"] == [note]
    assert "Future" not in json.dumps(context)
