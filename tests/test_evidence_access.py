import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from project_ensemble.domain import GenerationRequest, DecisionRigor, ReasoningEffort
from project_ensemble.errors import InputContextLimitError, PermanentProviderError
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.literature_report_execution import LiteratureReportExecutionRunner
from project_ensemble.orchestration.literature_writing_v071 import WriterChapter, ScienceChecklist
from project_ensemble.orchestration.literature_fast import FastLocalScienceRepair
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.runtime.evidence_access import EvidenceReadRequest, PublicEvidenceReader, FINDING_GROUPS
from project_ensemble.runtime.evidence_read_loop import invoke_with_evidence_reads, reading_enabled, _read_or_target_schema
from project_ensemble.runtime.prompt_contract import parse_json_prompt
from project_ensemble.storage.meeting import MeetingRepository


SID = "https://example.test/paper"
CID = "C00001-00001"
BASE = "public/literature_report/modules/RM-01/research/"


def save(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def fixture_evidence(root, text="Verified original text.", *, private=False):
    relative = ("human_private/institutional_documents/originals/paper.txt" if private else
                "public/research/literature_bundle/documents/paper.txt")
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    source = {
        "source_id": SID, "title": "Test paper", "publication_year": 2025,
        "authors": ["Author"], "url": SID, "archive_status": "ARCHIVED",
        "archived_path": relative, "archived_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "access_basis": "INSTITUTIONAL_SUBSCRIPTION" if private else "OPEN_ACCESS",
        "evidence_use_class": "DIRECT_SOURCE",
    }
    packet = {
        "packet_id": "RP-ONE", "normalized_claim": "Claim under condition A",
        "sources": [source], "knowledge_status": "SOURCE_BACKED", "consensus": "QUALIFIED",
        "unresolved_questions": ["Condition B remains unchecked"],
        "query_trace": "MUST NOT BE SENT", "score": "PRIVATE METRIC",
    }
    for group in FINDING_GROUPS:
        packet[group] = [{
            "source_id": SID, "direction": group.upper(), "evidence_summary": group + " full finding",
            "applicability": "Only condition A", "limitations": "Not condition B",
        }]
    # Real module dossiers omit the alternatives group; the ID reader must use
    # the full explicitly published packet rather than perpetuate that omission.
    dossier_packet = {key: value for key, value in packet.items() if key != "canonical_alternatives"}
    save(root, BASE + "evidence_dossier.json", {
        "module_id": "RM-01", "packets": [dossier_packet], "omitted_packet_ids": [],
    })
    save(root, "public/research/evidence_packets/RP-ONE.json", packet)
    save(root, BASE + "chapter_citation_catalog.json", {
        "sources": [{"source_id": SID, "citation_id": CID, "packet_ids": ["RP-ONE"]}],
    })
    return packet, path


class CapturingAdapter(ScriptedProviderAdapter):
    def __init__(self, answers):
        super().__init__("fake", ["m"], answers)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return super().generate(request)


def runner_for(tmp_path, answers=(), **kwargs):
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rule.md").write_text("rules")
    repo = MeetingRepository.create(
        tmp_path / "meetings", governance_docs=governance, task_description="test task",
        selected_models=[("fake", "m")], chair_model=("fake", "m"), writer_model=("fake", "m"),
        writer_reasoning_effort=ReasoningEffort.DEFAULT,
        **kwargs,
    )
    adapter = CapturingAdapter(list(answers))
    engine = MeetingEngine(
        repo=repo, adapters={"fake": adapter},
        notifier=SimpleNamespace(send_escalation=lambda **kwargs: True),
    )
    runner = LiteratureReportExecutionRunner.__new__(LiteratureReportExecutionRunner)
    runner.repo, runner.engine, runner.max_output_tokens = repo, engine, None
    runner._reader_facing_prose_contract = lambda: "Preserve the scientific meaning."
    return runner, adapter


def prompt(schema=ScienceChecklist):
    return json.dumps({"module": {"module_id": "RM-01"}, "objections": ["Keep this unresolved issue"]}) + (
        "\n\nTARGET JSON SCHEMA:\n" + json.dumps(schema.model_json_schema()))


def read_request(view="findings", **kwargs):
    return json.dumps({"evidence_read_requests": [{"source_id": CID, "view": view, **kwargs}]})


def test_full_findings_include_counterevidence_scope_and_alternatives_without_private_trails(tmp_path):
    fixture_evidence(tmp_path)
    reader = PublicEvidenceReader(tmp_path, module_ids=["RM-01"], writer=True)
    assert reader.index()["sources"][0]["source_id"] == CID
    result = reader.read(EvidenceReadRequest(source_id="C1-1"))
    assert result["source"]["publication_year"] == 2025
    assert {item["category"] for item in result["findings"]} == set(FINDING_GROUPS)
    assert all(item["limitations"] == "Not condition B" for item in result["findings"])
    assert result["basis"].endswith("NOT_ORIGINAL_SOURCE_TEXT")
    wire = json.dumps(result)
    assert "MUST NOT BE SENT" not in wire and "PRIVATE METRIC" not in wire and "RP-ONE" not in wire
    assert reader.read(EvidenceReadRequest(source_id=SID))["status"] == "UNKNOWN_OR_UNAUTHORIZED_SOURCE"
    reviewer = PublicEvidenceReader(tmp_path, module_ids=["RM-01"])
    assert {item["packet_id"] for item in reviewer.read(
        EvidenceReadRequest(source_id=CID))["findings"]} == {"RP-ONE"}


def test_only_released_snapshot_authorizes_global_sources(tmp_path):
    packet, _ = fixture_evidence(tmp_path)
    assert PublicEvidenceReader(tmp_path).index()["sources"] == []
    snapshot = save(tmp_path, "public/research/rounds/round1/evidence_snapshot.json", {
        "status": "PREPARED", "packets": [packet],
    })
    assert PublicEvidenceReader(tmp_path).index()["sources"] == []
    snapshot.write_text(json.dumps({"status": "RELEASED", "packet_ids": ["RP-ONE"]}))
    reader = PublicEvidenceReader(tmp_path)
    assert reader.read(EvidenceReadRequest(source_id=SID))["status"] == "READABLE_FINDINGS"


def test_findings_use_complete_records_and_explicit_cursors(tmp_path):
    packet, _ = fixture_evidence(tmp_path)
    packet["supporting_evidence"][0]["evidence_summary"] = "x" * 15000
    save(tmp_path, "public/research/evidence_packets/RP-ONE.json", packet)
    reader = PublicEvidenceReader(tmp_path, module_ids=["RM-01"])
    first = reader.read(EvidenceReadRequest(source_id=SID, max_characters=500))
    assert len(first["findings"]) == 1 and first["single_finding_exceeds_requested_window"]
    assert len(first["findings"][0]["evidence_summary"]) == 15000
    rest = reader.read(EvidenceReadRequest(source_id=SID, cursor=first["next_cursor"]))
    assert len(rest["findings"]) == 3 and not rest["has_more"]
    assert reader.read(EvidenceReadRequest(source_id=SID, cursor=999))["status"] == "INVALID_CURSOR"


def test_original_fragments_are_continuable_without_cutting_math(tmp_path):
    original = "a" * 495 + "$$x=" + "b" * 45 + "$$" + "c" * 650
    _, path = fixture_evidence(tmp_path, original)
    reader = PublicEvidenceReader(tmp_path, module_ids=["RM-01"], writer=True)
    first = reader.read(EvidenceReadRequest(source_id=CID, view="original", max_characters=500))
    assert first["status"] == "READABLE_ORIGINAL_FRAGMENT"
    assert first["text"].count("$$") == 2 and first["page_has_more"]
    assert not first["document_complete_in_this_result"]
    rest = reader.read(EvidenceReadRequest(source_id=CID, view="original", cursor=first["next_cursor"]))
    assert first["text"] + rest["text"] == original
    assert rest["content_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    inside = reader.read(EvidenceReadRequest(source_id=CID, view="original", cursor=502))
    assert inside["status"] == "CURSOR_INSIDE_MATH_BLOCK" and "text" not in inside


@pytest.mark.parametrize("damage", ["private", "digest", "symlink", "traversal", "broken_pdf"])
def test_original_failures_are_explicit_and_do_not_disclose_private_content(tmp_path, damage):
    packet, path = fixture_evidence(tmp_path, "SECRET", private=damage == "private")
    source = packet["sources"][0]
    if damage == "digest":
        path.write_text("tampered")
    elif damage == "symlink":
        elsewhere = tmp_path / "human_private/secret.txt"
        elsewhere.parent.mkdir(exist_ok=True)
        elsewhere.write_text("SECRET")
        path.unlink()
        path.symlink_to(elsewhere)
    elif damage == "traversal":
        source["archived_path"] = "public/research/literature_bundle/documents/../../../../human_private/secret"
    elif damage == "broken_pdf":
        path.write_bytes(b"%PDF-1.7\nbad data")
        source["archived_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    save(tmp_path, "public/research/evidence_packets/RP-ONE.json", packet)
    result = PublicEvidenceReader(tmp_path, module_ids=["RM-01"]).read(
        EvidenceReadRequest(source_id=SID, view="original"))
    assert result["status"] == "ORIGINAL_UNAVAILABLE_OR_UNAUTHORIZED"
    assert "SECRET" not in json.dumps(result) and "human_private/" not in json.dumps(result)


def test_image_only_pdf_is_not_reported_as_read(tmp_path):
    import io
    from pypdf import PdfWriter
    packet, path = fixture_evidence(tmp_path)
    writer, stream = PdfWriter(), io.BytesIO()
    writer.add_blank_page(width=100, height=100)
    writer.write(stream)
    path.write_bytes(stream.getvalue())
    packet["sources"][0]["archived_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    save(tmp_path, "public/research/evidence_packets/RP-ONE.json", packet)
    result = PublicEvidenceReader(tmp_path, module_ids=["RM-01"]).read(
        EvidenceReadRequest(source_id=SID, view="original"))
    assert result["status"] == "NO_EXTRACTABLE_TEXT" and "text" not in result


def test_unknown_and_private_path_requests_never_resolve_as_sources(tmp_path):
    fixture_evidence(tmp_path)
    reader = PublicEvidenceReader(tmp_path, module_ids=["RM-01"])
    for key in ["../../identity_private/meeting_manifest.json", "/etc/passwd", "RP-STAGED", "C1-999"]:
        assert reader.read(EvidenceReadRequest(source_id=key))["status"] == "UNKNOWN_OR_UNAUTHORIZED_SOURCE"


@pytest.mark.parametrize("field,value", [("cursor", True), ("page", -1), ("max_characters", 999999), ("view", "network")])
def test_read_request_rejects_unsafe_parameters(field, value):
    with pytest.raises(ValidationError):
        EvidenceReadRequest.model_validate({"source_id": CID, field: value})


@pytest.mark.parametrize("schema", [WriterChapter, FastLocalScienceRepair, ScienceChecklist])
def test_union_schema_keeps_all_root_relative_definitions_resolvable(schema):
    root = _read_or_target_schema(schema)
    def walk(value):
        if isinstance(value, dict):
            if str(value.get("$ref", "")).startswith("#/$defs/"):
                assert value["$ref"].removeprefix("#/$defs/") in root["$defs"]
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(root)


def test_one_reader_model_reads_then_returns_final_and_resume_reuses_all_turns(tmp_path):
    runner, adapter = runner_for(tmp_path, [read_request(), read_request("original"), '{"issues":[]}'])
    fixture_evidence(runner.repo.root)
    result = invoke_with_evidence_reads(
        runner, "CHAIR", stage="RM-01_science", schema=ScienceChecklist,
        system="Only the current science review.", user_text=prompt())
    assert result[0].text == '{"issues":[]}' and len(adapter.requests) == 3
    payload, _ = parse_json_prompt(adapter.requests[-1].user_text)
    assert payload["objections"] == ["Keep this unresolved issue"]
    assert [item["status"] for item in payload["evidence_read_results"]] == [
        "READABLE_FINDINGS", "READABLE_ORIGINAL_FRAGMENT"]
    assert list((runner.repo.root / "governance_private/evidence_reads").glob("*/*.json"))
    assert invoke_with_evidence_reads(
        runner, "CHAIR", stage="RM-01_science", schema=ScienceChecklist,
        system="Only the current science review.", user_text=prompt()) == result
    assert len(adapter.requests) == 3  # No extra paid call on recovery.


def test_broken_read_request_skips_repair_and_asks_for_final_once(tmp_path):
    runner, adapter = runner_for(tmp_path, [
        read_request("network"), '{"issues":[]}',
    ])
    fixture_evidence(runner.repo.root)
    runner.engine.validate_structured_response = lambda *a, **k: pytest.fail("optional read must not enter schema repair")
    result = invoke_with_evidence_reads(
        runner, "CHAIR", stage="RM-01_science", schema=ScienceChecklist,
        system="Review", user_text=prompt())
    assert result[0].text == '{"issues":[]}' and len(adapter.requests) == 2
    payload, suffix = parse_json_prompt(adapter.requests[1].user_text)
    assert payload["evidence_read_results"][0]["status"] == "INVALID_READ_REQUEST"
    assert not payload["evidence_library"]["further_reads_allowed"]
    assert "EvidenceReadBatch" not in suffix
    assert runner.engine.status.paused_reason is None


def test_read_loop_is_bounded_and_never_discards_objections(tmp_path):
    runner, adapter = runner_for(tmp_path, [read_request(), '{"issues":[]}'])
    fixture_evidence(runner.repo.root)
    manifest_path = runner.repo.root / "identity_private/meeting_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["evidence_read_round_limit"] = 1
    manifest_path.write_text(json.dumps(manifest))
    invoke_with_evidence_reads(
        runner, "CHAIR", stage="RM-01_review", schema=ScienceChecklist, system="Review", user_text=prompt())
    payload, suffix = parse_json_prompt(adapter.requests[1].user_text)
    assert len(adapter.requests) == 2 and payload["objections"] == ["Keep this unresolved issue"]
    assert payload["evidence_read_results"] and not payload["evidence_library"]["further_reads_allowed"]
    assert "EvidenceReadBatch" not in suffix


def test_repeated_read_is_deduplicated_without_claiming_failed_read_completed(tmp_path):
    runner, adapter = runner_for(tmp_path, [
        read_request("original"), read_request("original"), '{"issues":[]}',
    ])
    fixture_evidence(runner.repo.root, private=True)
    invoke_with_evidence_reads(
        runner, "CHAIR", stage="RM-01_review", schema=ScienceChecklist, system="Review", user_text=prompt())
    payload, _ = parse_json_prompt(adapter.requests[-1].user_text)
    first, duplicate = payload["evidence_read_results"]
    assert first["status"] == "ORIGINAL_UNAVAILABLE_OR_UNAUTHORIZED"
    assert duplicate["prior_status"] == first["status"] and duplicate["status"] == "DUPLICATE_READ_REQUEST"


def test_optional_read_budget_keeps_existing_task_and_delivered_material(tmp_path):
    runner, adapter = runner_for(tmp_path, [read_request(), '{"issues":[]}'])
    packet, _ = fixture_evidence(runner.repo.root)
    packet["supporting_evidence"][0]["evidence_summary"] = "x" * 80000
    save(runner.repo.root, "public/research/evidence_packets/RP-ONE.json", packet)
    adapter.maximum_input_characters = 20000
    invoke_with_evidence_reads(
        runner, "CHAIR", stage="RM-01_review", schema=ScienceChecklist, system="Review", user_text=prompt())
    payload, _ = parse_json_prompt(adapter.requests[-1].user_text)
    assert payload["objections"] == ["Keep this unresolved issue"]
    assert payload["evidence_read_results"][0]["status"] == "NOT_DELIVERED_CONTEXT_BUDGET"
    assert all(len(r.system_text) + len(r.user_text) <= 20000 for r in adapter.requests)


def test_existing_contracts_without_read_flag_remain_byte_compatible(tmp_path):
    runner, adapter = runner_for(tmp_path)
    fixture_evidence(runner.repo.root)
    path = runner.repo.root / "identity_private/meeting_manifest.json"
    manifest = json.loads(path.read_text())
    for version in [1, 2]:
        manifest.update(prompt_contract_version=version, context_assembly_version=version)
        manifest.pop("evidence_read_protocol_version", None)
        path.write_text(json.dumps(manifest))
        assert not reading_enabled(runner.repo.root)
        assert invoke_with_evidence_reads(
            runner, "CHAIR", stage="RM-01_science", schema=ScienceChecklist, system="s", user_text=prompt()) is None
    assert not adapter.requests


@pytest.mark.parametrize("schema, final", [
    (ScienceChecklist, {"issues": []}),
    (FastLocalScienceRepair, {"edits": [], "glossary_edits": [], "objection_responses": []}),
    (WriterChapter, {"draft": {"title": "Chapter", "body_markdown": "Supported condition A.",
                              "short_summary": "Limited to A."}}),
])
def test_writing_local_revision_and_science_service_use_the_same_read_loop(tmp_path, schema, final):
    runner, adapter = runner_for(tmp_path, [read_request(), json.dumps(final)])
    fixture_evidence(runner.repo.root)
    value = runner._invoke_service(
        "WRITER" if schema is not ScienceChecklist else "CHAIR",
        stage="RM-01_test", schema=schema, system="Current task",
        user={"module": {"module_id": "RM-01"}, "objections": ["Retain issue"]})
    assert isinstance(value, schema) and len(adapter.requests) == 2
    wire, _ = parse_json_prompt(adapter.requests[-1].user_text)
    assert wire["evidence_read_results"][0]["status"] == "READABLE_FINDINGS"


def test_full_message_character_preflight_rejects_large_system_without_calling_provider(tmp_path):
    runner, adapter = runner_for(tmp_path, ["must not be consumed"])
    adapter.maximum_input_characters = 100
    with pytest.raises(InputContextLimitError):
        runner.engine.invoke_participant("CHAIR", stage="review", system_text="x" * 100, user_text="y")
    assert not adapter.requests


def test_subscription_adapter_also_counts_system_before_starting_transport(monkeypatch):
    from project_ensemble.providers import codex_subscription
    adapter = codex_subscription.CodexSubscriptionAdapter("codex")
    adapter.maximum_input_characters = 100
    monkeypatch.setattr(codex_subscription, "_AppServer", lambda *a, **k: pytest.fail("must not start transport"))
    with pytest.raises(PermanentProviderError, match="101 characters"):
        adapter.generate(GenerationRequest(model_id="test", system_text="x" * 100, user_text="y"))


@pytest.mark.parametrize("headings", [
    ["COMMON RULES", "TASK EMPHASIS", "CURRENT STAGE", "OWN RECORD: own.md"],
    ["共同规则", "本次职能侧重", "当前阶段", "本人记录: own.md"],
])
def test_layout_only_recovery_understands_neutral_and_chinese_headers(headings):
    old = ["COMMON RULES", "YOUR PERSONA", "CURRENT STAGE", "YOUR STATE: own.md"]
    content = ["rules", "scope", "stage", "own"]
    render = lambda names, order: "\n\n".join(f"## {names[i]}\n{content[i]}" for i in order) + "\n"
    expected = MeetingEngine._representative_context_sections(render(old, [0, 1, 2, 3]))
    assert MeetingEngine._representative_context_sections(render(headings, [0, 2, 3, 1])) == expected
    assert MeetingEngine._representative_context_sections(render(headings, [0, 2, 3, 1]).replace("rules", "changed")) != expected


def test_real_fallback_does_not_duplicate_language_or_ruling_footer(tmp_path, monkeypatch):
    runner, adapter = runner_for(tmp_path, ["fallback answer"], deliberation_language="en", decision_rigor=DecisionRigor.RELAXED)
    original = adapter.generate
    attempts = []
    def fail_once(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise PermanentProviderError("fixture failure")
        return original(request)
    monkeypatch.setattr(adapter, "generate", fail_once)
    monkeypatch.setattr("project_ensemble.orchestration.engine.ModelFallbackOrderService.order_for",
                        lambda self, source: [("fake", "backup")])
    adapter.model_ids.append("backup")
    response = runner.engine.invoke_participant("CHAIR", stage="review", system_text="rules", user_text="task")
    assert response.text == "fallback answer"
    assert len(attempts) == 2 and attempts[0].system_text == attempts[1].system_text
    assert attempts[1].system_text.count("MEETING-SPECIFIC HUMAN PROCEDURAL RULING") == 1
    assert runner.engine.find_recorded_response("CHAIR", stage="review", system_text="rules", user_text="task") == response


def test_ambiguous_citation_ids_never_select_an_arbitrary_source(tmp_path):
    packet, _ = fixture_evidence(tmp_path)
    second = {**packet["sources"][0], "source_id": "https://example.test/second"}
    packet["sources"].append(second)
    save(tmp_path, "public/research/evidence_packets/RP-ONE.json", packet)
    save(tmp_path, BASE + "chapter_citation_catalog.json", {"sources": [
        {"source_id": SID, "citation_id": CID},
        {"source_id": second["source_id"], "citation_id": CID},
    ]})
    reader = PublicEvidenceReader(tmp_path, module_ids=["RM-01"], writer=True)
    assert reader.read(EvidenceReadRequest(source_id=CID))["status"] == "UNKNOWN_OR_UNAUTHORIZED_SOURCE"
    assert reader.read(EvidenceReadRequest(source_id="C1-1"))["status"] == "UNKNOWN_OR_UNAUTHORIZED_SOURCE"
    assert reader.index()["sources"] == []


def test_index_canonical_replay_rechecks_neutral_section_content(tmp_path):
    runner, adapter = runner_for(tmp_path, ["answer"])
    registry = json.loads(runner.repo.docs.read_text("identity_private/representative_registry.json"))
    rid = registry[0]["representative_id"]
    before = "## COMMON RULES\nrule\n\n## TASK EMPHASIS\nscope\n\n## CURRENT STAGE\nstage\n\n## OWN RECORD: own.md\nown\n"
    after = "## COMMON RULES\nrule\n\n## CURRENT STAGE\nstage\n\n## OWN RECORD: own.md\nown\n\n## TASK EMPHASIS\nscope\n"
    answer = runner.engine.invoke_participant(rid, stage="review", system_text=before, user_text="task")
    assert runner.engine.find_recorded_response(rid, stage="review", system_text=after, user_text="task") == answer
    assert runner.engine.find_recorded_response(
        rid, stage="review", system_text=after.replace("\nown\n", "\nnew private state\n"), user_text="task") is None
    assert len(adapter.requests) == 1


def test_optional_index_does_not_follow_parent_symlink(tmp_path):
    from project_ensemble.runtime.exchange_index import ExchangeReplayIndex
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "meeting"
    (root / "governance_private").mkdir(parents=True)
    (root / "governance_private/replay_cache").symlink_to(outside, target_is_directory=True)
    index = ExchangeReplayIndex(root, MeetingEngine._representative_context_sections)
    assert index.candidates("R1", "s", "rules", "task") is None
    assert not list(outside.iterdir())


def test_scholarly_review_keeps_substantive_no_after_optional_read(tmp_path):
    from project_ensemble.orchestration.scholarly_rendering import ScholarlyRenderingRunner, ScienceReview
    final = {"vote": "NO", "objections": [{
        "issue": "Condition B remains unverified.", "requires_external_verification": False,
    }]}
    base, adapter = runner_for(tmp_path, [read_request(), json.dumps(final)])
    fixture_evidence(base.repo.root)
    runner = ScholarlyRenderingRunner.__new__(ScholarlyRenderingRunner)
    runner.repo, runner.engine, runner.max_output_tokens = base.repo, base.engine, None
    value = runner._invoke(
        "CHAIR", stage="RM-01_scholarly_science", schema=ScienceReview,
        system="Review only.", user={"module": {"module_id": "RM-01"}, "source": "Frozen text."})
    assert value.vote == "NO" and value.objections[0].issue == final["objections"][0]["issue"]
    assert len(adapter.requests) == 2


def test_writer_initial_read_registry_excludes_same_version_later_supplement(tmp_path):
    runner, adapter = runner_for(tmp_path, [
        '{"draft":{"title":"Chapter","body_markdown":"Frozen condition A.","short_summary":"A."}}',
    ])
    packet, _ = fixture_evidence(runner.repo.root)
    next_sid, next_cid = "https://example.test/later", "C00001-00002"
    packet2 = {**packet, "packet_id": "RP-TWO",
               "sources": [{**packet["sources"][0], "source_id": next_sid}]}
    save(runner.repo.root, "public/research/evidence_packets/RP-TWO.json", packet2)
    save(runner.repo.root, "public/literature_report/modules/RM-01/writing_v071/writer_v1_citation_rechecked_catalog.json", {
        "sources": [{"source_id": SID, "citation_id": CID, "packet_ids": ["RP-ONE"]},
                    {"source_id": next_sid, "citation_id": next_cid, "packet_ids": ["RP-TWO"]}],
    })
    invoke_with_evidence_reads(
        runner, "WRITER", stage="literature_v071_writer_RM-01_v1", schema=WriterChapter,
        system="Write from the given catalog.", user_text=prompt(WriterChapter))
    wire, _ = parse_json_prompt(adapter.requests[0].user_text)
    assert [item["source_id"] for item in wire["evidence_library"]["sources"]] == [CID]
