import json
import hashlib
from pathlib import Path
import pytest
from project_ensemble.domain import DeliverableType, InheritanceMode, MeetingType, ReasoningEffort
from project_ensemble.storage.meeting import MeetingRepository
from project_ensemble.storage.documents import ImmutableDocumentStore
from project_ensemble.errors import ImmutableWriteError


def test_immutable_copy_streams_large_source_and_records_digest(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    content = b"research-packet\0" * 400_000
    source.write_bytes(content)
    store = ImmutableDocumentStore(tmp_path / "meeting")
    original_read_bytes = Path.read_bytes

    def reject_whole_source_read(path):
        if path == source:
            raise AssertionError("source must be copied in bounded chunks")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_whole_source_read)
    destination, byte_count, digest = store.copy_once(
        "public/research/bundle.bin", source, chunk_size=4096
    )
    assert byte_count == len(content)
    assert digest == hashlib.sha256(content).hexdigest()
    assert destination.read_bytes() == content
    with pytest.raises(ImmutableWriteError):
        store.copy_once("public/research/bundle.bin", source)


def test_interrupted_creation_removes_only_its_uncommitted_staging(tmp_path, monkeypatch):
    import project_ensemble.storage.meeting as meeting_module

    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule", encoding="utf-8")
    parent = tmp_path / "M-PARENT"
    (parent / "public/final").mkdir(parents=True)
    (parent / "public/meeting_manifest.json").write_text(
        json.dumps({"meeting_id": "M-PARENT"}), encoding="utf-8"
    )
    (parent / "public/final/final_report.md").write_text("# Source\n", encoding="utf-8")

    def interrupt_import(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(meeting_module, "_import_parent_meeting_assets", interrupt_import)
    workspace = tmp_path / "ws"
    with pytest.raises(KeyboardInterrupt):
        MeetingRepository.create(
            workspace,
            selected_models=[("fake", "m")],
            chair_model=("fake", "m"),
            governance_docs=gov,
            parent_meeting_id="M-PARENT",
            parent_meeting_path=parent,
            inheritance_mode=InheritanceMode.BOTH,
            forced_meeting_id="M-CHILD",
        )
    assert not (workspace / "M-CHILD").exists()
    assert not list(workspace.glob(".M-CHILD.creating-*"))


def test_meeting_repo_separates_public_and_identity(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    ws = tmp_path / "ws"
    repo = MeetingRepository.create(
        ws,
        selected_models=[("deepseek", "m1"), ("kimi", "m2"), ("gemini", "m3")],
        chair_model=("deepseek", "chair-model"),
        governance_docs=gov,
        forced_meeting_id="M-TEST",
    )
    pub = json.loads((repo.root / "public/representatives.json").read_text())
    priv = json.loads((repo.root / "identity_private/representative_registry.json").read_text())
    assert len(pub) == 12 == len(priv)
    assert "runtime" not in pub[0]
    assert "runtime" in priv[0]
    public_manifest = json.loads((repo.root / "public/meeting_manifest.json").read_text())
    private_manifest = json.loads((repo.root / "identity_private/meeting_manifest.json").read_text())
    assert "selected_models" not in public_manifest
    assert "chair_model" not in public_manifest
    assert private_manifest["chair_model"] == ["deepseek", "chair-model"]
    assert private_manifest["software_version"] == "0.7.1"
    assert private_manifest["governance_version"] == "0.7.1"
    assert repo.events.verify()


def test_meeting_freezes_original_prompt_lineage_and_portable_config_snapshots(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    model_config = config_dir / "models.toml"
    model_config.write_text(
        "[providers.fake]\nkind='openai_compatible'\nbase_url='https://example.invalid'\n"
        "api_key_env='FAKE_API_KEY'\n",
        encoding="utf-8",
    )
    config = config_dir / "ensemble.toml"
    config.write_text(
        "[project]\nmodel_config_file='./models.toml'\n",
        encoding="utf-8",
    )

    parent = MeetingRepository.create(
        tmp_path / "parent-ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        task_description="原始问题，保留精确文本。",
        meeting_title="父会议",
        config_path=config,
        forced_meeting_id="M-PARENT",
    )
    parent.docs.write_once("public/final/final_report.md", "# Parent result\n")
    child = MeetingRepository.create(
        tmp_path / "child-ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
        task_description="接续问题。",
        meeting_title="子会议",
        parent_meeting_id="M-PARENT",
        parent_meeting_path=parent.root,
        inheritance_mode=InheritanceMode.FINAL_DOCUMENT,
        config_path=config,
        forced_meeting_id="M-CHILD",
    )

    assert (parent.root / "original_prompt.txt").read_text() == "原始问题，保留精确文本。"
    assert (child.root / "original_prompt.txt").read_text() == "接续问题。"
    assert (child.root / "parent_prompt.txt").read_text() == "原始问题，保留精确文本。"
    lineage = json.loads((child.root / "meeting_lineage.json").read_text())
    assert [item["meeting_id"] for item in lineage["meetings"]] == [
        "M-PARENT",
        "M-CHILD",
    ]
    assert lineage["meetings"][-1]["inherited_material_categories"] == [
        "FINAL_DOCUMENT"
    ]
    config_ref = json.loads(
        (child.root / "human_private/session_configuration.json").read_text()
    )
    assert len(config_ref["config_sha256"]) == 64
    assert len(config_ref["model_config_sha256"]) == 64
    assert (
        child.root / "human_private/configuration_snapshot/ensemble.toml"
    ).read_bytes() == config.read_bytes()
    assert (
        child.root / "human_private/configuration_snapshot/model_config.toml"
    ).read_bytes() == model_config.read_bytes()


def test_meeting_run_lock_rejects_concurrent_advancement(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=gov,
    )
    second_handle = MeetingRepository(repo.root)
    with repo.exclusive_run_lock():
        with pytest.raises(ValueError, match="另一个 ensemble 进程"):
            with second_handle.exclusive_run_lock():
                pass
    with second_handle.exclusive_run_lock():
        pass


def test_scholarly_rendering_repo_has_role_scoped_reviewers_and_frozen_source(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    parent = tmp_path / "M-PARENT"
    (parent / "public/final").mkdir(parents=True)
    (parent / "public/meeting_manifest.json").write_text(
        json.dumps({"meeting_id": "M-PARENT"}), encoding="utf-8"
    )
    (parent / "public/final/literature_review_report.md").write_text(
        "# Frozen review\n", encoding="utf-8"
    )
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "s1"), ("fake", "s2"), ("fake", "c1"), ("fake", "c2")],
        chair_model=("fake", "chair"),
        governance_docs=gov,
        meeting_type=MeetingType.SCHOLARLY_RENDERING,
        deliverable_type=DeliverableType.SCHOLARLY_RENDERING,
        parent_meeting_id="M-PARENT",
        parent_meeting_path=parent,
        inheritance_mode=InheritanceMode.BOTH,
        research_enabled=True,
        research_model=("fake", "research"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        rendering_science_models=[("fake", "s1"), ("fake", "s2")],
        rendering_citation_models=[("fake", "c1"), ("fake", "c2")],
        rendering_language="zh",
        rendering_academic_skeleton=True,
        rendering_full_abstract=True,
        rendering_section_abstracts=False,
        rendering_segmentation=3,
        rendering_liveliness=2,
        rendering_output_formats=["md"],
        rendering_science_order=["fake:s2", "fake:s1"],
    )
    assert repo.meeting_id.startswith("SR-")
    registry = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text()
    )
    assert len(registry) == 4
    assert {record["runtime"]["rendering_role"] for record in registry} == {
        "science_bookkeeper",
        "citation_bookkeeper",
    }
    assert all(record["runtime"]["persona"] is None for record in registry)
    assert json.loads(
        (repo.root / "public/meeting_manifest.json").read_text(encoding="utf-8")
    )["rendering_final_science_query_limit"] == 4
    assert json.loads(
        (repo.root / "identity_private/meeting_manifest.json").read_text(encoding="utf-8")
    )["rendering_final_science_query_limit"] == 4
    assert (
        repo.root
        / "public/continuation/source_artifacts/final/literature_review_report.md"
    ).read_text() == "# Frozen review\n"
    assert (repo.root / "ORIGINAL_REPORT.md").is_symlink()
    assert (repo.root / "ORIGINAL_REPORT.md").read_text() == "# Frozen review\n"


def test_completed_rendering_can_be_inherited_as_read_only_partial_source(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule", encoding="utf-8")
    parent = tmp_path / "SR-PARENT"
    (parent / "public/final/scholarly_rendering").mkdir(parents=True)
    (parent / "public/meeting_manifest.json").write_text(json.dumps({
        "meeting_id": "SR-PARENT", "deliverable_type": "scholarly_rendering",
    }), encoding="utf-8")
    source = parent / "public/final/scholarly_rendering/scholarly_review.md"
    source.write_text("# 已重绘报告\n\n原有正文。\n", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", x) for x in ("s1", "s2", "c1", "c2")],
        chair_model=("fake", "chair"), governance_docs=gov,
        meeting_type=MeetingType.SCHOLARLY_RENDERING,
        deliverable_type=DeliverableType.SCHOLARLY_RENDERING,
        parent_meeting_id="SR-PARENT", parent_meeting_path=parent,
        inheritance_mode=InheritanceMode.BOTH, research_enabled=True,
        research_model=("fake", "research"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        rendering_science_models=[("fake", "s1"), ("fake", "s2")],
        rendering_citation_models=[("fake", "c1"), ("fake", "c2")],
        rendering_language="zh", rendering_academic_skeleton=True,
        rendering_full_abstract=False, rendering_section_abstracts=False,
        rendering_segmentation=3, rendering_liveliness=2,
        rendering_output_formats=["md"], rendering_science_order=["fake:s1", "fake:s2"],
        rendering_scope_description="只重绘引言",
    )
    inherited = repo.root / "public/continuation/source_artifacts/final/scholarly_rendering/scholarly_review.md"
    assert inherited.read_bytes() == source.read_bytes()
    assert (repo.root / "ORIGINAL_REPORT.md").read_bytes() == source.read_bytes()
    assert json.loads((repo.root / "identity_private/meeting_manifest.json").read_text())[
        "rendering_scope_description"] == "只重绘引言"
    assert source.read_text(encoding="utf-8") == "# 已重绘报告\n\n原有正文。\n"


def test_rendering_can_inherit_uncertified_chair_draft_without_changing_parent(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule", encoding="utf-8")
    parent = tmp_path / "M-PARENT"
    (parent / "public").mkdir(parents=True)
    (parent / "public/meeting_manifest.json").write_text(
        json.dumps({"meeting_id": "M-PARENT", "deliverable_type": "literature_review"}),
        encoding="utf-8",
    )
    patch_path = parent / "audit_private/literature_report/publication_patches.json"
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(
        json.dumps({"decisions": [], "final_text": "# Uncertified source\n"}),
        encoding="utf-8",
    )
    cert_path = parent / "chair_private/literature_report/readability_certification.json"
    cert_path.parent.mkdir(parents=True)
    cert_path.write_text(json.dumps({
        "status": "REVISION_REQUIRED",
        "source_sha256": hashlib.sha256(b"# Uncertified source\n").hexdigest(),
    }), encoding="utf-8")
    before = {str(path.relative_to(parent)): path.read_bytes()
              for path in parent.rglob("*") if path.is_file()}
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "s1"), ("fake", "s2"), ("fake", "c1"), ("fake", "c2")],
        chair_model=("fake", "chair"),
        governance_docs=gov,
        meeting_type=MeetingType.SCHOLARLY_RENDERING,
        deliverable_type=DeliverableType.SCHOLARLY_RENDERING,
        parent_meeting_id="M-PARENT",
        parent_meeting_path=parent,
        inheritance_mode=InheritanceMode.BOTH,
        rendering_provisional_source=True,
        research_enabled=True,
        research_model=("fake", "research"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        rendering_science_models=[("fake", "s1"), ("fake", "s2")],
        rendering_citation_models=[("fake", "c1"), ("fake", "c2")],
        rendering_language="zh",
        rendering_output_formats=["md"],
        rendering_science_order=["fake:s1", "fake:s2"],
    )
    inherited = repo.root / "public/continuation/source_artifacts/final/literature_review_report.md"
    assert inherited.read_text(encoding="utf-8") == "# Uncertified source\n"
    assert (repo.root / "SOURCE_DRAFT.md").is_symlink()
    assert (repo.root / "SOURCE_DRAFT.md").read_text(encoding="utf-8") == "# Uncertified source\n"
    lineage = json.loads((repo.root / "public/continuation/lineage.json").read_text())
    assert lineage["source_status"] == "PROVISIONAL_UNCERTIFIED"
    assert lineage["files"][-1]["source_field"] == "final_text"
    assert before == {str(path.relative_to(parent)): path.read_bytes()
                      for path in parent.rglob("*") if path.is_file()}


def test_scholarly_rendering_rejects_partial_inheritance_and_duplicate_models(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule")
    parent = tmp_path / "M-PARENT"
    (parent / "public/final").mkdir(parents=True)
    (parent / "public/meeting_manifest.json").write_text(
        json.dumps(
            {
                "meeting_id": "M-PARENT",
                "deliverable_type": DeliverableType.LITERATURE_REVIEW.value,
            }
        ),
        encoding="utf-8",
    )
    (parent / "public/final/literature_review_report.md").write_text(
        "# Frozen review\n", encoding="utf-8"
    )
    common = dict(
        selected_models=[("fake", "s1"), ("fake", "s2"), ("fake", "c1"), ("fake", "c2")],
        chair_model=("fake", "chair"),
        governance_docs=gov,
        meeting_type=MeetingType.SCHOLARLY_RENDERING,
        deliverable_type=DeliverableType.SCHOLARLY_RENDERING,
        parent_meeting_id="M-PARENT",
        parent_meeting_path=parent,
        research_enabled=True,
        research_model=("fake", "research"),
        research_reasoning_effort=ReasoningEffort.DEFAULT,
        rendering_citation_models=[("fake", "c1"), ("fake", "c2")],
        rendering_language="zh",
        rendering_output_formats=["md"],
    )
    with pytest.raises(ValueError, match="inherit both"):
        MeetingRepository.create(
            tmp_path / "partial",
            **common,
            inheritance_mode=InheritanceMode.EVIDENCE,
            rendering_science_models=[("fake", "s1"), ("fake", "s2")],
            rendering_science_order=["fake:s1", "fake:s2"],
        )
    with pytest.raises(ValueError, match="must be unique"):
        MeetingRepository.create(
            tmp_path / "duplicate",
            **common,
            inheritance_mode=InheritanceMode.BOTH,
            rendering_science_models=[("fake", "s1"), ("fake", "s1")],
            rendering_science_order=["fake:s1"],
        )
