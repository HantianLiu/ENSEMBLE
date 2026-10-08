import hashlib
import json
import shutil
from pathlib import Path

import pytest

from project_ensemble import cli
from project_ensemble.errors import PolicyNotConfiguredError
from project_ensemble.storage.governance_snapshot import GOVERNANCE_SNAPSHOT, freeze_governance_docs
from project_ensemble.storage.meeting import MeetingRepository, directory_digest


def create_repo(tmp_path):
    governance = tmp_path / "rules"
    governance.mkdir()
    (governance / "rule.md").write_text("frozen rule\n", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "workspace", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs=governance,
        task_description="task",
    )
    return repo, governance


def test_new_meeting_uses_exact_local_snapshot_after_live_rules_change(tmp_path):
    repo, governance = create_repo(tmp_path)
    original_manifest = repo.docs.read_text("identity_private/meeting_manifest.json")
    snapshot = repo.root / GOVERNANCE_SNAPSHOT
    assert (snapshot / "rule.md").read_text() == "frozen rule\n"
    assert directory_digest(snapshot) == json.loads(original_manifest)["governance_digest"]
    (governance / "rule.md").write_text("updated rule\n")
    resolved = cli._meeting_governance_docs(repo, governance)
    assert resolved == snapshot
    cli._assert_meeting_runtime_compatible(repo, governance_docs=resolved)
    assert repo.docs.read_text("identity_private/meeting_manifest.json") == original_manifest


def test_snapshot_remains_usable_after_meeting_move_and_source_removal(tmp_path):
    repo, governance = create_repo(tmp_path)
    destination = tmp_path / "moved" / repo.root.name
    destination.parent.mkdir()
    repo.root.rename(destination)
    shutil.rmtree(governance)
    moved = MeetingRepository(destination)
    resolved = cli._meeting_governance_docs(moved, governance)
    assert resolved == destination / GOVERNANCE_SNAPSHOT
    cli._assert_meeting_runtime_compatible(moved, governance_docs=resolved)


@pytest.mark.parametrize("damage", ["edit", "extra", "symlink"])
def test_snapshot_tampering_is_not_hidden_by_matching_external_rules(tmp_path, damage):
    repo, governance = create_repo(tmp_path)
    snapshot = repo.root / GOVERNANCE_SNAPSHOT
    if damage == "edit":
        (snapshot / "rule.md").write_text("tampered\n")
    elif damage == "extra":
        (snapshot / "unexpected.txt").write_text("extra")
    else:
        (snapshot / "rule.md").unlink()
        (snapshot / "rule.md").symlink_to(governance / "rule.md")
    with pytest.raises(PolicyNotConfiguredError, match="SNAPSHOT_MISMATCH"):
        cli._meeting_governance_docs(repo, governance)


def test_verified_legacy_recovery_adds_snapshot_without_rewriting_frozen_records(tmp_path):
    repo, governance = create_repo(tmp_path)
    shutil.rmtree(repo.root / GOVERNANCE_SNAPSHOT)
    before = {str(p.relative_to(repo.root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in repo.root.rglob("*") if p.is_file()}
    expected = json.loads(repo.docs.read_text("identity_private/meeting_manifest.json"))["governance_digest"]
    recovered = tmp_path / "recovered"
    shutil.copytree(governance, recovered)
    (governance / "rule.md").write_text("updated rule\n")
    freeze_governance_docs(recovered, repo.root / GOVERNANCE_SNAPSHOT, expected_digest=expected)
    resolved = cli._meeting_governance_docs(repo, governance)
    cli._assert_meeting_runtime_compatible(repo, governance_docs=resolved)
    for relative, digest in before.items():
        assert hashlib.sha256((repo.root / relative).read_bytes()).hexdigest() == digest


def test_recovery_rejects_wrong_package_before_creating_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "rule.md").write_text("wrong rule")
    destination = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="frozen digest"):
        freeze_governance_docs(source, destination, expected_digest="0" * 64)
    assert not destination.exists()


def test_snapshot_creation_is_idempotent_but_does_not_overwrite(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "rule.md").write_text("first")
    destination = tmp_path / "snapshot"
    freeze_governance_docs(source, destination)
    assert freeze_governance_docs(source, destination) == destination
    (source / "rule.md").write_text("second")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        freeze_governance_docs(source, destination)
    assert (destination / "rule.md").read_text() == "first"
