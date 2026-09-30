import json
from pathlib import Path

import pytest

from project_ensemble.cli import _assert_meeting_runtime_compatible
from project_ensemble.errors import EventChainError
from project_ensemble.storage.legacy_migration import migrate_v06_meeting
from project_ensemble.storage.meeting import MeetingRepository, directory_digest


def _legacy_meeting(tmp_path: Path):
    old_governance = tmp_path / "old-governance"
    old_governance.mkdir()
    (old_governance / "rule.md").write_text("v0.6 rule", encoding="utf-8")
    new_governance = tmp_path / "new-governance"
    new_governance.mkdir()
    (new_governance / "rule.md").write_text("v0.7 rule", encoding="utf-8")
    config = tmp_path / "ensemble.toml"
    config.write_text(
        "[project]\n"
        f"governance_docs = {json.dumps(str(new_governance))}\n"
        "[providers.fake]\n"
        "kind = 'openai_compatible'\n"
        "base_url = 'https://unused.invalid'\n"
        "api_key_env = 'FAKE_API_KEY'\n",
        encoding="utf-8",
    )
    legacy_config = tmp_path / "legacy-ensemble.toml"
    legacy_config.write_text(
        "[project]\n"
        f"governance_docs = {json.dumps(str(old_governance))}\n",
        encoding="utf-8",
    )
    repo = MeetingRepository.create(
        tmp_path / "source",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=old_governance,
        task_description="请调研这项问题。",
        config_path=legacy_config,
        forced_meeting_id="M-LEGACY",
    )
    private_path = repo.root / "identity_private/meeting_manifest.json"
    private = json.loads(private_path.read_text(encoding="utf-8"))
    private.pop("software_version")
    private.pop("governance_version")
    private_path.write_text(json.dumps(private), encoding="utf-8")
    (repo.root / "original_prompt.txt").unlink()
    (repo.root / "meeting_lineage.json").unlink()
    (repo.root / "human_private/configuration_snapshot/ensemble.toml").unlink()
    (repo.root / "human_private/configuration_snapshot").rmdir()
    (repo.root / "human_private/session_configuration.json").write_text(
        json.dumps({"meeting_id": repo.meeting_id, "config_path": str(legacy_config)}),
        encoding="utf-8",
    )
    repo.docs.write_once("public/literature_report/frozen_note.txt", "already frozen\n")
    (repo.root / "FROZEN_NOTE.txt").symlink_to("public/literature_report/frozen_note.txt")
    return repo, config, old_governance, new_governance


def test_v06_migration_preserves_source_and_frozen_files(tmp_path):
    source, config, old_governance, new_governance = _legacy_meeting(tmp_path)
    destination = tmp_path / "M-LEGACY-v07"
    source_manifest = (source.root / "identity_private/meeting_manifest.json").read_bytes()
    source_events = (source.root / "governance_private/events.jsonl").read_bytes()
    frozen = (source.root / "public/literature_report/frozen_note.txt").read_bytes()

    preview = migrate_v06_meeting(source.root, destination, config_path=config, dry_run=True)
    assert preview["meeting_id"] == "M-LEGACY"
    assert not destination.exists()

    result = migrate_v06_meeting(source.root, destination, config_path=config)
    migrated = MeetingRepository(destination)
    _assert_meeting_runtime_compatible(migrated, governance_docs=new_governance)
    assert migrated.events.verify()
    assert result["legacy_governance_digest"] == directory_digest(old_governance)
    assert (destination / "public/literature_report/frozen_note.txt").read_bytes() == frozen
    assert (destination / "FROZEN_NOTE.txt").is_symlink()
    assert (destination / "original_prompt.txt").read_text(encoding="utf-8") == "请调研这项问题。"
    assert (destination / "human_private/configuration_snapshot/ensemble.toml").read_bytes() == config.read_bytes()
    assert json.loads((destination / "human_private/v06_migration/legacy_session_configuration.json").read_text())[
        "meeting_id"
    ] == "M-LEGACY"
    assert (source.root / "identity_private/meeting_manifest.json").read_bytes() == source_manifest
    assert (source.root / "governance_private/events.jsonl").read_bytes() == source_events
    assert (destination / "governance_private/events.jsonl").read_bytes().startswith(source_events)


def test_v06_migration_refuses_running_or_broken_source(tmp_path):
    source, config, _old_governance, _new_governance = _legacy_meeting(tmp_path)
    destination = tmp_path / "M-LEGACY-v07"
    with source.exclusive_run_lock():
        with pytest.raises(ValueError, match="仍在运行"):
            migrate_v06_meeting(source.root, destination, config_path=config)
    assert not destination.exists()

    event_path = source.root / "governance_private/events.jsonl"
    event_path.write_text(event_path.read_text(encoding="utf-8").replace("MEETING_CREATED", "TAMPERED"), encoding="utf-8")
    with pytest.raises(EventChainError, match="invalid event_hash"):
        migrate_v06_meeting(source.root, destination, config_path=config)
    assert not destination.exists()
