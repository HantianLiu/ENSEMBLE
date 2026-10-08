import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_ensemble import cli
from project_ensemble.storage.meeting_index import (
    indexed_meetings,
    register_meeting,
    registered_meeting,
)
from project_ensemble.storage.meeting import MeetingRepository


def _meeting(path: Path, meeting_id: str) -> Path:
    public = path / "public"
    public.mkdir(parents=True)
    (public / "meeting_manifest.json").write_text(
        json.dumps({"meeting_id": meeting_id, "title": "搬迁测试会议"}), encoding="utf-8"
    )
    return path


class _Wizard:
    def __init__(self, *choices):
        self.choices = iter(choices)
        self.prompts = []

    def _choose_one(self, title, options):
        self.prompts.append((title, options))
        choice = next(self.choices)
        assert choice in {value for value, _ in options}
        return choice


def test_moved_meeting_requires_confirmation_and_then_appears_in_list(tmp_path, monkeypatch):
    config = tmp_path / "ensemble.toml"
    original = _meeting(tmp_path / "original" / "LR-ABCDEF12", "LR-ABCDEF12")
    register_meeting(original, config)
    moved_parent = tmp_path / "moved"
    moved_parent.mkdir()
    moved = moved_parent / "renamed-meeting"
    original.rename(moved)
    monkeypatch.chdir(moved_parent)

    assert cli._available_meeting_paths(config) == []
    assert registered_meeting("LR-ABCDEF12", config).path == str(original)
    assert cli._select_meeting_from_current_directory(_Wizard("back"), config) is None
    assert registered_meeting("LR-ABCDEF12", config).path == str(original)

    wizard = _Wizard("update")
    assert cli._select_meeting_from_current_directory(wizard, config) == moved
    assert "是否更新" in wizard.prompts[0][0]
    assert registered_meeting("LR-ABCDEF12", config).path == str(moved)
    assert cli._available_meeting_paths(config) == [moved]


def test_current_directory_adds_unlisted_meeting_without_renaming_files(tmp_path, monkeypatch):
    config = tmp_path / "ensemble.toml"
    moved = _meeting(tmp_path / "my-renamed-folder", "LR-ABCDEF13")
    monkeypatch.chdir(moved)
    wizard = _Wizard()
    assert cli._select_meeting_from_current_directory(wizard, config) == moved
    assert registered_meeting("LR-ABCDEF13", config).path == str(moved)
    assert not wizard.prompts


def test_open_once_does_not_change_saved_location(tmp_path, monkeypatch):
    config = tmp_path / "ensemble.toml"
    original = _meeting(tmp_path / "original", "LR-ABCDEF14")
    copied = _meeting(tmp_path / "other", "LR-ABCDEF14")
    register_meeting(original, config)
    monkeypatch.chdir(copied)
    assert cli._select_meeting_from_current_directory(_Wizard("once"), config) == copied
    assert registered_meeting("LR-ABCDEF14", config).path == str(original)


def test_register_preserves_other_missing_meetings_for_later_relocation(tmp_path):
    config = tmp_path / "ensemble.toml"
    first = _meeting(tmp_path / "first", "LR-ABCDEF15")
    second = _meeting(tmp_path / "second", "LR-ABCDEF16")
    register_meeting(first, config)
    register_meeting(second, config)
    first.rename(tmp_path / "first-moved")
    register_meeting(second, config)
    assert registered_meeting("LR-ABCDEF15", config).path == str(first)
    assert [item.meeting_id for item in indexed_meetings(config)] == ["LR-ABCDEF16"]


def test_home_offers_current_directory_before_meeting_list(tmp_path, monkeypatch):
    config = tmp_path / "ensemble.toml"
    meeting = _meeting(tmp_path / "meeting", "LR-ABCDEF17")
    wizard = _Wizard("existing", "current", "quit")
    wizard.show_home = lambda: None
    monkeypatch.setattr(cli, "TerminalWizard", lambda: wizard)
    monkeypatch.setattr(cli, "_config_path", lambda _value: str(config))
    monkeypatch.setattr(cli, "_load_config", lambda _value: SimpleNamespace(source_path=config))
    monkeypatch.setattr(cli, "_select_meeting_from_current_directory", lambda *_: meeting)
    monkeypatch.setattr(cli, "_open_meeting", lambda *_: None)
    assert cli.cmd_home(SimpleNamespace(config=None)) == 0
    assert [value for value, _ in wizard.prompts[1][1]][:2] == ["current", "list"]


def test_moved_meeting_can_use_its_frozen_config_when_source_is_missing(tmp_path):
    root = _meeting(tmp_path / "relocated", "LR-ABCDEF18")
    snapshot = root / "human_private/configuration_snapshot/ensemble.toml"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text('[project]\ngovernance_docs = "@package"\n', encoding="utf-8")
    reference = root / "human_private/session_configuration.json"
    reference.write_text(json.dumps({
        "config_path": str(tmp_path / "missing" / "ensemble.toml"),
        "snapshot_path": "human_private/configuration_snapshot/ensemble.toml",
        "config_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    assert MeetingRepository(root).config_path() == snapshot
    snapshot.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen digest"):
        MeetingRepository(root).config_path()


def test_moved_meeting_rejects_changed_model_config_snapshot(tmp_path):
    root = _meeting(tmp_path / "relocated", "LR-ABCDEF19")
    snapshot_dir = root / "human_private/configuration_snapshot"
    snapshot_dir.mkdir(parents=True)
    config = snapshot_dir / "ensemble.toml"
    models = snapshot_dir / "model_config.toml"
    config.write_text('[project]\nmodel_config_file = "./model_config.toml"\n', encoding="utf-8")
    models.write_text("[providers]", encoding="utf-8")
    reference = root / "human_private/session_configuration.json"
    reference.write_text(json.dumps({
        "config_path": str(tmp_path / "missing" / "ensemble.toml"),
        "snapshot_path": "human_private/configuration_snapshot/ensemble.toml",
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "model_config_snapshot_path": "human_private/configuration_snapshot/model_config.toml",
        "model_config_sha256": hashlib.sha256(models.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    assert MeetingRepository(root).config_path() == config
    models.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="model configuration snapshot.*frozen digest"):
        MeetingRepository(root).config_path()


def test_update_command_registers_and_relocates_nearby_meetings(tmp_path, monkeypatch, capsys):
    original = _meeting(tmp_path / "old" / "LR-ABCDEF20", "LR-ABCDEF20")
    config = tmp_path / "ensemble.toml"
    register_meeting(original, config)
    current = tmp_path / "current"
    current.mkdir()
    moved = current / "renamed-meeting"
    original.rename(moved)
    added = _meeting(current / "another-meeting", "LR-ABCDEF21")
    monkeypatch.chdir(current)
    monkeypatch.setattr(cli, "enable_utf8_terminal_erase", lambda: None)
    monkeypatch.setattr(sys, "argv", ["ensemble", "update"])

    assert cli._main_impl() == 0
    assert registered_meeting("LR-ABCDEF20", config).path == str(moved)
    assert registered_meeting("LR-ABCDEF21", config).path == str(added)
    assert "新增 1 个，更新位置 1 个" in capsys.readouterr().out
    assert cli._main_impl() == 0
    assert "位置未变 2 个" in capsys.readouterr().out


def test_update_skips_ambiguous_same_id_copies_without_changing_index(
    tmp_path, monkeypatch, capsys
):
    original = _meeting(tmp_path / "original", "LR-ABCDEF22")
    config = tmp_path / "ensemble.toml"
    register_meeting(original, config)
    current = tmp_path / "current"
    current.mkdir()
    _meeting(current / "copy-a", "LR-ABCDEF22")
    _meeting(current / "copy-b", "LR-ABCDEF22")
    monkeypatch.chdir(current)

    assert cli.cmd_update(SimpleNamespace(config=None)) == 1
    assert registered_meeting("LR-ABCDEF22", config).path == str(original)
    assert "多个同 ID 副本" in capsys.readouterr().err


def test_update_processes_valid_meetings_and_reports_invalid_manifests(
    tmp_path, monkeypatch, capsys
):
    current = tmp_path / "current"
    current.mkdir()
    valid = _meeting(current / "valid", "LR-ABCDEF23")
    invalid = current / "invalid" / "public"
    invalid.mkdir(parents=True)
    (invalid / "meeting_manifest.json").write_text("not json", encoding="utf-8")
    monkeypatch.chdir(current)

    assert cli.cmd_update(SimpleNamespace(config=None)) == 1
    assert registered_meeting("LR-ABCDEF23", current / "ensemble.toml").path == str(valid)
    assert "跳过无法识别的会议目录" in capsys.readouterr().err
