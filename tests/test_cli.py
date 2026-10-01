import io
import json
import os
import stat
import sys
import termios
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_ensemble import cli
from project_ensemble.config import EnsembleConfig, ProviderConfig
from project_ensemble.domain import ModelDescriptor
from project_ensemble.errors import FeatureNotImplementedError, PolicyNotConfiguredError
from project_ensemble.orchestration.consultations import (
    HumanConsultationIssue,
    HumanConsultationService,
    ThresholdRecord,
)
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.storage.meeting import MeetingRepository


def test_runtime_config_merges_user_added_providers_with_project_config(monkeypatch, tmp_path):
    project_path = tmp_path / "project.toml"
    user_path = tmp_path / "user.toml"
    project_path.touch()
    user_path.touch()
    project = EnsembleConfig(providers={
        "deepseek": ProviderConfig(kind="openai_compatible", base_url="https://project.test/v1", api_key_env="PROJECT_KEY"),
    })
    user = EnsembleConfig(providers={
        "lithos": ProviderConfig(kind="openai_compatible", display_name="LithosAI", base_url="https://api.lithosai.cloud/v1", api_key_env="LITHOSAI_API_KEY"),
        "deepseek": ProviderConfig(kind="openai_compatible", base_url="https://user.test/v1", api_key_env="USER_KEY"),
    })
    monkeypatch.setattr(cli, "user_config_path", lambda: user_path)
    monkeypatch.setattr(cli, "load_config", lambda path: user if Path(path) == user_path else project)

    loaded = cli._load_config(project_path)

    assert set(loaded.providers) == {"deepseek", "lithos"}
    assert loaded.providers["lithos"].base_url == "https://api.lithosai.cloud/v1"
    assert loaded.providers["deepseek"].base_url == "https://user.test/v1"


@pytest.mark.parametrize("raise_error", [False, True])
def test_main_restores_terminal_echo_on_every_exit(monkeypatch, raise_error):
    master_fd, slave_fd = os.openpty()
    tty_input = os.fdopen(os.dup(slave_fd), "r", encoding="utf-8")
    original = termios.tcgetattr(tty_input.fileno())
    monkeypatch.setattr(sys, "stdin", tty_input)

    def run():
        edited = termios.tcgetattr(tty_input.fileno())
        edited[3] &= ~(termios.ECHO | termios.ICANON)
        termios.tcsetattr(tty_input.fileno(), termios.TCSANOW, edited)
        if raise_error:
            raise RuntimeError("simulated unexpected exit")
        return 0

    monkeypatch.setattr(cli, "_main_impl", run)
    try:
        if raise_error:
            with pytest.raises(RuntimeError, match="simulated unexpected exit"):
                cli.main()
        else:
            assert cli.main() == 0
        restored = termios.tcgetattr(tty_input.fileno())
        assert restored[3] & termios.ECHO == original[3] & termios.ECHO
        assert restored[3] & termios.ICANON == original[3] & termios.ICANON
    finally:
        termios.tcsetattr(tty_input.fileno(), termios.TCSANOW, original)
        tty_input.close()
        os.close(master_fd)
        os.close(slave_fd)


def test_v07_refuses_in_place_resume_with_changed_governance(tmp_path):
    frozen_governance = tmp_path / "frozen-governance"
    frozen_governance.mkdir()
    (frozen_governance / "rule.md").write_text("frozen")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=frozen_governance,
        task_description="task",
    )
    cli._assert_meeting_runtime_compatible(
        repo, governance_docs=frozen_governance
    )
    changed_governance = tmp_path / "changed-governance"
    changed_governance.mkdir()
    (changed_governance / "rule.md").write_text("changed")

    with pytest.raises(PolicyNotConfiguredError, match="GOVERNANCE_DIGEST_MISMATCH"):
        cli._assert_meeting_runtime_compatible(
            repo, governance_docs=changed_governance
        )


def test_v073_can_resume_v071_meeting_with_same_frozen_governance(tmp_path):
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rule.md").write_text("frozen")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=governance,
        task_description="task",
    )
    manifest_path = repo.root / "identity_private/meeting_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["software_version"] = "0.7.1"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # A 0.7.3 executor may continue a 0.7.1 meeting when its immutable
    # governance package is unchanged.
    cli._assert_meeting_runtime_compatible(repo, governance_docs=governance)

    manifest["software_version"] = "0.7.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PolicyNotConfiguredError, match="MEETING_SOFTWARE_VERSION_MISMATCH"):
        cli._assert_meeting_runtime_compatible(repo, governance_docs=governance)


def test_v071_resume_finds_bundled_exact_governance_snapshot(tmp_path):
    from project_ensemble.paths import bundled_historical_governance_docs
    from project_ensemble.storage.meeting import directory_digest

    historical = next(
        path for path in bundled_historical_governance_docs()
        if path.name == "governance_frozen_2026_09_30_v071"
    )
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=historical,
        task_description="task",
    )
    session_path = repo.root / "human_private/session_configuration.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(
        json.dumps({"governance_docs_path": str(tmp_path / "missing-governance")}),
        encoding="utf-8",
    )
    manifest_path = repo.root / "identity_private/meeting_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["software_version"] = "0.7.1"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    resolved = cli._meeting_governance_docs(
        repo, Path(__file__).parents[1] / "docs/governance"
    )
    assert Path(resolved) == historical
    assert directory_digest(resolved) == manifest["governance_digest"]
    cli._assert_meeting_runtime_compatible(repo, governance_docs=resolved)


def test_audit_command_is_an_explicit_non_mutating_interface(tmp_path):
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rule.md").write_text("rule")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs=governance,
        meeting_type=cli.MeetingType.AUDIT,
        task_description="audit this meeting",
    )
    before = sorted(path.relative_to(repo.root) for path in repo.root.rglob("*") if path.is_file())

    with pytest.raises(FeatureNotImplementedError, match="AUDIT_STATE_MACHINE_NOT_IMPLEMENTED"):
        cli._audit_not_implemented(repo)

    after = sorted(path.relative_to(repo.root) for path in repo.root.rglob("*") if path.is_file())
    assert before == after


def test_unimplemented_audit_is_not_offered_as_a_new_meeting(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ensemble", "--help"])
    with pytest.raises(SystemExit) as help_exit:
        cli.main()
    assert help_exit.value.code == 0
    assert "run-audit" not in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["ensemble", "start", "--non-interactive", "--meeting-type", "audit"])
    with pytest.raises(SystemExit) as rejected:
        cli.main()
    assert rejected.value.code == 2
    assert "invalid choice: 'audit'" in capsys.readouterr().err


def write_config(tmp_path):
    path = tmp_path / "ensemble.toml"
    path.write_text(
        f"""
[project]
workspace = "{tmp_path / 'workspace'}"

[providers.fake]
kind = "openai_compatible"
enabled = true
base_url = "https://unused.invalid"
api_key_env = "FAKE_API_KEY"

[notifications.email]
enabled = true
host = "smtp.example.test"
from_address = "ensemble@example.test"
""".strip()
        + "\n"
    )
    return path


def test_noninteractive_cli_uses_same_validated_start_path(monkeypatch, tmp_path, capsys):
    config = write_config(tmp_path)
    launch_directory = tmp_path / "launch-directory"
    launch_directory.mkdir()
    monkeypatch.chdir(launch_directory)
    monkeypatch.setattr(
        cli,
        "discover_models",
        lambda cfg, providers: [ModelDescriptor(provider_id="fake", model_id="m")],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ensemble",
            "start",
            "--config",
            str(config),
            "--governance-docs",
            str(tmp_path),
            "--non-interactive",
            "--meeting-type",
            "deliberation",
            "--provider",
            "fake",
            "--model",
            "fake:m",
            "--chair",
            "fake:m",
            "--task",
            "secret task",
            "--email",
            "human@example.test",
        ],
    )
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert "会议已初始化" in output
    assert "secret task" not in output
    assert "human@example.test" not in output
    meetings = list(launch_directory.glob("DL-*"))
    assert len(meetings) == 1
    assert not (tmp_path / "workspace").exists()
    import json

    config_ref = json.loads((meetings[0] / "human_private/session_configuration.json").read_text())
    assert config_ref["config_path"] == str(config.resolve())


def test_cli_rejects_startup_options_without_explicit_automation_mode(monkeypatch, tmp_path, capsys):
    config = write_config(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ensemble", "start", "--config", str(config), "--task", "bypass TUI"],
    )
    assert cli.main() == 2
    assert "only be supplied with --non-interactive" in capsys.readouterr().err


def test_bare_ensemble_opens_home_screen(monkeypatch):
    observed = []
    monkeypatch.setattr(sys, "argv", ["ensemble"])
    monkeypatch.setattr(cli, "cmd_home", lambda args: observed.append(args.cmd) or 0)
    assert cli.main() == 0
    assert observed == ["home"]


def test_meeting_id_is_a_direct_open_shorthand(monkeypatch):
    observed = []
    monkeypatch.setattr(sys, "argv", ["ensemble", "M-ABC12345"])
    monkeypatch.setattr(
        cli,
        "cmd_open",
        lambda args: observed.append((args.cmd, args.selector)) or 0,
    )
    assert cli.main() == 0
    assert observed == [("open", "M-ABC12345")]


def test_resume_subcommand_uses_same_meeting_selector(monkeypatch):
    observed = []
    monkeypatch.setattr(sys, "argv", ["ensemble", "resume", "M-ABC12345"])
    monkeypatch.setattr(
        cli, "cmd_open", lambda args: observed.append((args.cmd, args.selector)) or 0
    )
    assert cli.main() == 0
    assert observed == [("resume", "M-ABC12345")]


def test_unhandled_meeting_failure_offers_audited_retry(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    import json

    meeting = tmp_path / "LR-RECOVERY"
    (meeting / "public").mkdir(parents=True)
    (meeting / "public/meeting_manifest.json").write_text(
        '{"meeting_id":"LR-RECOVERY"}', encoding="utf-8"
    )
    attempts = []
    args = SimpleNamespace(
        cmd="run-report", meeting=str(meeting), config=None,
        func=lambda _args: attempts.append("retried") or 0,
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: "1")
    assert cli._recover_unhandled_failure(args, ValueError("bad frozen input")) == 0
    assert attempts == ["retried"]
    records = list((meeting / "audit_private/runtime_recovery").glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["message"] == "bad frozen input"
    output = capsys.readouterr().err
    assert "ensemble resume" in output
    assert "进度已保留" in output


def test_unhandled_meeting_failure_without_tty_prints_recovery_command(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace

    meeting = tmp_path / "LR-RECOVERY"
    (meeting / "public").mkdir(parents=True)
    (meeting / "public/meeting_manifest.json").write_text(
        '{"meeting_id":"LR-RECOVERY"}', encoding="utf-8"
    )
    args = SimpleNamespace(cmd="run-report", meeting=str(meeting), config=None)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert cli._recover_unhandled_failure(args, RuntimeError("unexpected")) == 2
    output = capsys.readouterr().err
    assert "ensemble resume" in output
    assert "交互终端" in output


def test_main_routes_unexpected_resume_exception_to_recovery(monkeypatch, tmp_path, capsys):
    meeting = tmp_path / "LR-RECOVERY"
    (meeting / "public").mkdir(parents=True)
    (meeting / "public/meeting_manifest.json").write_text(
        '{"meeting_id":"LR-RECOVERY"}', encoding="utf-8"
    )

    def broken_open(args):
        args._resume_cmd = "run-report"
        args._resume_meeting = str(meeting)
        raise RuntimeError("unexpected pipeline failure")

    monkeypatch.setattr(sys, "argv", ["ensemble", "resume", str(meeting)])
    monkeypatch.setattr(sys, "stdin", type("QuietInput", (), {"isatty": lambda self: False})())
    monkeypatch.setattr(cli, "cmd_open", broken_open)
    assert cli.main() == 2
    output = capsys.readouterr().err
    assert "unexpected pipeline failure" in output
    assert "ensemble resume" in output
    assert "error:" not in output


def test_recovery_after_start_reopens_existing_meeting(monkeypatch, tmp_path):
    from types import SimpleNamespace

    meeting = tmp_path / "LR-RECOVERY"
    (meeting / "public").mkdir(parents=True)
    (meeting / "public/meeting_manifest.json").write_text(
        '{"meeting_id":"LR-RECOVERY"}', encoding="utf-8"
    )
    restarted = []
    args = SimpleNamespace(
        cmd="start", _resume_cmd="run-report", _resume_meeting=str(meeting),
        func=lambda _args: restarted.append("initialized-again") or 0,
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: "1")
    monkeypatch.setattr(cli, "_open_meeting", lambda _args, path: restarted.append(path) or 0)
    assert cli._recover_unhandled_failure(args, RuntimeError("failed after creation")) == 0
    assert restarted == [meeting]


@pytest.mark.parametrize("entrypoint", [cli.cmd_run_general, cli.cmd_run_report])
def test_old_resume_entrypoints_offer_rendering_on_frozen_readability_failure(
    monkeypatch, tmp_path, entrypoint
):
    repo = SimpleNamespace(root=tmp_path / "M-FAILED")
    monkeypatch.setattr(cli, "MeetingRepository", lambda path: repo)
    monkeypatch.setattr(cli, "_has_frozen_readability_failure", lambda current: True)
    opened = []
    monkeypatch.setattr(cli, "_open_meeting", lambda args, path: opened.append(path) or 0)
    args = SimpleNamespace(meeting=str(repo.root))
    assert entrypoint(args) == 0
    assert opened == [repo.root]


def test_completed_report_can_directly_enter_rendering_without_restarting_report(
    monkeypatch, tmp_path
):
    source = tmp_path / "M-SOURCE"
    report = source / "public/final/literature_review_report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Frozen report\n", encoding="utf-8")
    repo = SimpleNamespace(root=source)
    cfg = SimpleNamespace(source_path=tmp_path / "ensemble.toml")
    entry = SimpleNamespace(
        meeting_id="M-SOURCE", title="Literature review",
        deliverable_type=cli.DeliverableType.LITERATURE_REVIEW.value,
    )
    monkeypatch.setattr(cli, "inspect_meeting", lambda root: entry)
    chosen = []

    class Wizard:
        def _choose_one(self, title, options):
            chosen.append((title, options))
            return "render"

    monkeypatch.setattr(
        cli, "_interactive_start_arguments",
        lambda args, cfg_path: SimpleNamespace(),
    )
    started = []
    monkeypatch.setattr(cli, "cmd_start", lambda args: started.append(args) or 0)

    assert cli._continue_completed_meeting(SimpleNamespace(), repo, cfg, Wizard()) == 0
    assert chosen[0][1][0][0] == "render"
    assert started[0]._continuation_parent == str(source)
    assert started[0]._continuation_inheritance == cli.InheritanceMode.BOTH
    assert started[0]._continuation_meeting_type == cli.MeetingType.SCHOLARLY_RENDERING


def test_completed_non_report_has_no_direct_rendering_option(monkeypatch, tmp_path):
    repo = SimpleNamespace(root=tmp_path / "M-SOURCE")
    cfg = SimpleNamespace(source_path=tmp_path / "ensemble.toml")
    entry = SimpleNamespace(
        meeting_id="M-SOURCE", title="Resolution",
        deliverable_type=cli.DeliverableType.NORMATIVE_INSTRUMENT.value,
    )
    monkeypatch.setattr(cli, "inspect_meeting", lambda root: entry)

    class Wizard:
        def _choose_one(self, title, options):
            assert "render" not in [value for value, _ in options]
            return "results"

    assert cli._continue_completed_meeting(SimpleNamespace(), repo, cfg, Wizard()) == 0


def test_completed_normative_meeting_offers_direct_literature_successor(monkeypatch, tmp_path):
    root = tmp_path / "DL-SOURCE"
    document = root / "public/final/final_report.md"
    document.parent.mkdir(parents=True)
    document.write_text("# 决议", encoding="utf-8")
    repo = SimpleNamespace(root=root)
    cfg = SimpleNamespace(source_path=tmp_path / "ensemble.toml")
    monkeypatch.setattr(cli, "inspect_meeting", lambda root: SimpleNamespace(
        meeting_id="DL-SOURCE", title="规范会议",
        deliverable_type=cli.DeliverableType.NORMATIVE_INSTRUMENT.value,
    ))
    chosen = []

    class Wizard:
        def _choose_one(self, title, options):
            chosen.append(options)
            return "literature"

    monkeypatch.setattr(
        cli, "_interactive_start_arguments", lambda args, cfg_path: SimpleNamespace()
    )
    started = []
    monkeypatch.setattr(cli, "cmd_start", lambda args: started.append(args) or 0)
    assert cli._continue_completed_meeting(SimpleNamespace(), repo, cfg, Wizard()) == 0
    assert "literature" in [value for value, _ in chosen[0]]
    assert started[0]._continuation_meeting_type == cli.MeetingType.DELIBERATION
    assert started[0]._continuation_inheritance == cli.InheritanceMode.BOTH


def test_completed_successor_menu_can_return_without_starting(monkeypatch, tmp_path):
    root = tmp_path / "DL-SOURCE"
    document = root / "public/final/final_report.md"
    document.parent.mkdir(parents=True)
    document.write_text("# 决议", encoding="utf-8")
    repo = SimpleNamespace(root=root)
    cfg = SimpleNamespace(source_path=tmp_path / "ensemble.toml")
    monkeypatch.setattr(cli, "inspect_meeting", lambda _root: SimpleNamespace(
        meeting_id="DL-SOURCE", title="规范会议",
        deliverable_type=cli.DeliverableType.NORMATIVE_INSTRUMENT.value,
    ))
    class Wizard:
        def _choose_one(self, _title, options):
            assert "back" in [value for value, _ in options]
            return "back"
    monkeypatch.setattr(cli, "cmd_start", lambda _args: pytest.fail("must not start"))
    assert cli._continue_completed_meeting(SimpleNamespace(), repo, cfg, Wizard()) is None


def test_successor_inheritance_back_returns_one_menu(monkeypatch, tmp_path):
    root = tmp_path / "DL-SOURCE"
    root.mkdir()
    repo = SimpleNamespace(root=root)
    cfg = SimpleNamespace(source_path=tmp_path / "ensemble.toml")
    monkeypatch.setattr(cli, "inspect_meeting", lambda _root: SimpleNamespace(
        meeting_id="DL-SOURCE", title="规范会议",
        deliverable_type=cli.DeliverableType.NORMATIVE_INSTRUMENT.value,
    ))
    choices = iter(("continue", "back", "back"))
    class Wizard:
        def _choose_one(self, _title, _options):
            return next(choices)
    assert cli._continue_completed_meeting(SimpleNamespace(), repo, cfg, Wizard()) is None


def test_open_meeting_returns_from_successor_to_meeting_menu(monkeypatch, tmp_path):
    root = tmp_path / "LR-COMPLETE"
    source = root / "public/final/literature_review_report.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 报告", encoding="utf-8")
    (root / "public/meeting_manifest.json").write_text(
        json.dumps({"meeting_id": "LR-COMPLETE", "deliverable_type": "literature_review"}),
        encoding="utf-8",
    )
    repo = SimpleNamespace(root=root)
    cfg = SimpleNamespace(source_path=tmp_path / "ensemble.toml")
    monkeypatch.setattr(cli, "MeetingRepository", lambda _path: repo)
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    monkeypatch.setattr(cli, "_config_path", lambda _value, _repo: str(cfg.source_path))
    monkeypatch.setattr(cli, "meeting_is_complete", lambda _path: True)
    monkeypatch.setattr(cli, "complete_render_source", lambda _path: source)
    monkeypatch.setattr(cli, "inspect_meeting", lambda _path: SimpleNamespace(
        meeting_id="LR-COMPLETE", title="报告",
    ))
    choices = iter(("successor", "back"))
    monkeypatch.setattr(cli.TerminalWizard, "_choose_one",
                        lambda self, title, options: next(choices))
    monkeypatch.setattr(cli, "_continue_completed_meeting", lambda *_args: None)
    assert cli._open_meeting(SimpleNamespace(config=None), root) is None


def test_open_incomplete_meeting_can_enter_chair_qa_without_resuming(monkeypatch, tmp_path):
    root = tmp_path / "LR-DRAFT"
    draft = root / "public/final/literature_review_report.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("# 草稿", encoding="utf-8")
    (root / "public/meeting_manifest.json").write_text(
        json.dumps({"meeting_id": "LR-DRAFT", "deliverable_type": "literature_review"}),
        encoding="utf-8",
    )
    repo = SimpleNamespace(root=root)
    cfg = SimpleNamespace(source_path=tmp_path / "ensemble.toml")
    monkeypatch.setattr(cli, "MeetingRepository", lambda path: repo)
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "_config_path", lambda value, repo: str(cfg.source_path))
    monkeypatch.setattr(cli, "meeting_is_complete", lambda root: False)
    monkeypatch.setattr(cli, "inspect_meeting", lambda root: SimpleNamespace(
        meeting_id="LR-DRAFT", title="草稿会议",
    ))
    monkeypatch.setattr(cli.TerminalWizard, "_choose_one", lambda self, title, options: "ask")
    asked = []
    monkeypatch.setattr(cli, "_chair_qa_interactive", lambda repo, cfg, wizard: asked.append(repo.root) or 0)
    monkeypatch.setattr(cli, "_resume_selected_meeting", lambda *args: pytest.fail("should not resume"))
    assert cli._open_meeting(SimpleNamespace(config=None), root) == 0
    assert asked == [root]


def test_completed_meeting_offers_supplementary_formats_without_resuming(monkeypatch, tmp_path):
    root = tmp_path / "LR-COMPLETE"
    source = root / "public/final/literature_review_report.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 已完成报告\n", encoding="utf-8")
    (root / "public/meeting_manifest.json").write_text(
        json.dumps({"meeting_id": "LR-COMPLETE", "deliverable_type": "literature_review"}),
        encoding="utf-8",
    )
    repo = SimpleNamespace(root=root)
    cfg = SimpleNamespace(source_path=tmp_path / "ensemble.toml")
    monkeypatch.setattr(cli, "MeetingRepository", lambda path: repo)
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "_config_path", lambda value, repo: str(cfg.source_path))
    monkeypatch.setattr(cli, "meeting_is_complete", lambda path: True)
    monkeypatch.setattr(cli, "complete_render_source", lambda path: source)
    monkeypatch.setattr(cli, "inspect_meeting", lambda path: SimpleNamespace(
        meeting_id="LR-COMPLETE", title="已完成报告",
    ))
    def choose(self, title, options):
        assert "additional_formats" in [value for value, _ in options]
        return "additional_formats"
    monkeypatch.setattr(cli.TerminalWizard, "_choose_one", choose)
    called = []
    monkeypatch.setattr(cli, "_additional_formats_interactive", lambda repo, wizard: called.append(repo.root) or 0)
    monkeypatch.setattr(cli, "_resume_selected_meeting", lambda *args: pytest.fail("must not resume"))
    assert cli._open_meeting(SimpleNamespace(config=None), root) == 0
    assert called == [root]


def test_supplementary_formats_back_returns_to_meeting_menu(monkeypatch, tmp_path):
    root = tmp_path / "LR-COMPLETE"
    source = root / "public/final/literature_review_report.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 已完成报告\n", encoding="utf-8")
    (root / "public/meeting_manifest.json").write_text(
        json.dumps({"meeting_id": "LR-COMPLETE", "deliverable_type": "literature_review"}),
        encoding="utf-8",
    )
    repo = SimpleNamespace(root=root)
    cfg = SimpleNamespace(source_path=tmp_path / "ensemble.toml")
    monkeypatch.setattr(cli, "MeetingRepository", lambda path: repo)
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "_config_path", lambda value, repo: str(cfg.source_path))
    monkeypatch.setattr(cli, "meeting_is_complete", lambda path: True)
    monkeypatch.setattr(cli, "complete_render_source", lambda path: source)
    monkeypatch.setattr(cli, "inspect_meeting", lambda path: SimpleNamespace(
        meeting_id="LR-COMPLETE", title="已完成报告",
    ))
    choices = iter(("additional_formats", "results"))
    monkeypatch.setattr(cli.TerminalWizard, "_choose_one", lambda self, title, options: next(choices))
    monkeypatch.setattr(cli, "_additional_formats_interactive", lambda repo, wizard: None)
    assert cli._open_meeting(SimpleNamespace(config=None), root) == 0


def test_resume_v06_meeting_delegates_to_version_pinned_runtime(monkeypatch, tmp_path):
    root = tmp_path / "M-LEGACY"
    (root / "public").mkdir(parents=True)
    (root / "identity_private").mkdir()
    (root / "public/meeting_manifest.json").write_text(
        json.dumps({"deliverable_type": "literature_review"}), encoding="utf-8"
    )
    (root / "identity_private/meeting_manifest.json").write_text(
        json.dumps({"software_version": "0.6.0"}), encoding="utf-8"
    )
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/ensemble-v06")
    calls = []
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda command, check: calls.append(command) or SimpleNamespace(returncode=0),
    )
    assert cli._resume_selected_meeting(SimpleNamespace(), SimpleNamespace(root=root), None) == 0
    assert calls == [["/bin/ensemble-v06", "run-report", "--meeting", str(root)]]


def test_successor_from_v06_uses_explicit_v07_config_not_old_governance(monkeypatch, tmp_path):
    root = tmp_path / "M-LEGACY"
    private = root / "identity_private/meeting_manifest.json"
    private.parent.mkdir(parents=True)
    private.write_text(json.dumps({"software_version": "0.6.0"}), encoding="utf-8")
    monkeypatch.delenv("ENSEMBLE_CONFIG", raising=False)
    current_config = write_config(tmp_path)
    old_config = tmp_path / "v06.toml"
    selected = cli._successor_config_path(
        SimpleNamespace(config=str(current_config)), SimpleNamespace(root=root),
        SimpleNamespace(source_path=old_config),
    )
    assert selected == str(current_config)
    assert selected != str(old_config)


def test_legacy_resume_script_using_unversioned_ensemble_still_delegates(monkeypatch, tmp_path):
    root = tmp_path / "M-OLD"
    (root / "public").mkdir(parents=True)
    (root / "identity_private").mkdir()
    (root / "public/meeting_manifest.json").write_text(
        json.dumps({"deliverable_type": "literature_review"}), encoding="utf-8"
    )
    (root / "identity_private/meeting_manifest.json").write_text(
        json.dumps({"software_version": "0.6.0"}), encoding="utf-8"
    )
    monkeypatch.setattr(cli, "MeetingRepository", lambda path: SimpleNamespace(root=root))
    monkeypatch.setattr(cli, "_config_path", lambda value, repo: "unused.toml")
    monkeypatch.setattr(cli, "load_config", lambda path: SimpleNamespace(source_path="unused.toml"))
    monkeypatch.setattr(cli, "register_meeting", lambda *args: None)
    delegated = []
    monkeypatch.setattr(
        cli, "_resume_selected_meeting",
        lambda args, repo, cfg: delegated.append(repo.root) or 0,
    )
    args = SimpleNamespace(
        meeting=str(root), config=None, governance_docs=None,
        max_output_tokens=None, no_progress=False,
    )
    assert cli.cmd_run_general(args) == 0
    assert delegated == [root]


def test_unreadable_report_offers_provisional_rendering_instead_of_stuck_resume(
    monkeypatch, tmp_path
):
    source = tmp_path / "M-UNREADABLE"
    cert = source / "chair_private/literature_report/readability_certification.json"
    cert.parent.mkdir(parents=True)
    cert.write_text(json.dumps({"status": "REVISION_REQUIRED"}), encoding="utf-8")
    (source / "public").mkdir()
    (source / "public/meeting_manifest.json").write_text(
        json.dumps({"deliverable_type": "literature_review"}), encoding="utf-8"
    )
    repo = SimpleNamespace(root=source)
    cfg = SimpleNamespace(source_path=tmp_path / "ensemble.toml")
    monkeypatch.setattr(cli, "MeetingRepository", lambda path: repo)
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "_config_path", lambda value, repo: str(cfg.source_path))
    monkeypatch.setattr(cli, "register_meeting", lambda root, config: None)
    monkeypatch.setattr(cli, "meeting_is_complete", lambda root: False)
    monkeypatch.setattr(cli, "inspect_meeting", lambda root: SimpleNamespace(
        meeting_id="M-UNREADABLE", title="Report",
        deliverable_type=cli.DeliverableType.LITERATURE_REVIEW.value,
    ))
    monkeypatch.setattr(cli, "provisional_rendering_text", lambda root: "# Frozen draft\n")
    monkeypatch.setattr(cli.TerminalWizard, "_choose_one", lambda self, title, options: "render")
    started = []
    monkeypatch.setattr(cli, "cmd_start", lambda args: started.append(args) or 0)

    assert cli._open_meeting(SimpleNamespace(config=None), source) == 0
    assert started[0]._continuation_parent == str(source)
    assert started[0].config == str(cfg.source_path)
    assert started[0]._continuation_inheritance == cli.InheritanceMode.BOTH
    assert started[0]._continuation_meeting_type == cli.MeetingType.SCHOLARLY_RENDERING
    assert started[0]._continuation_provisional_source is True


def test_continuation_replaces_argparse_empty_config_with_source_config():
    args = SimpleNamespace(config=None)
    prepared = cli._interactive_start_arguments(
        args, cfg_path="/installation/ensemble.toml"
    )
    assert prepared.config == "/installation/ensemble.toml"
    assert prepared.human_reference is None
    assert prepared.literature_writing_policy is None
    assert prepared.writer_model is None
    assert prepared.writer_reasoning_effort is None


def test_home_start_arguments_reach_interactive_wizard(monkeypatch, tmp_path):
    class WizardReached(Exception):
        pass

    received = {}
    def collect(*args, **kwargs):
        received.update(kwargs)
        raise WizardReached

    monkeypatch.setattr(cli, "_config_path", lambda value: str(tmp_path / "ensemble.toml"))
    monkeypatch.setattr(
        cli, "load_config",
        lambda value: SimpleNamespace(source_path=tmp_path / "ensemble.toml"),
    )
    monkeypatch.setattr(
        cli, "TerminalWizard",
        lambda: SimpleNamespace(collect=collect),
    )
    args = cli._interactive_start_arguments(SimpleNamespace(config=None))
    with pytest.raises(WizardReached):
        cli.cmd_start(args)
    assert received["prompt_for_literature_dialogue"] is True


def test_meeting_management_returns_to_list_then_home_after_background_delete(monkeypatch, tmp_path):
    meeting = tmp_path / "LR-A1B2C3D4"
    meeting.mkdir()
    selections = iter(["manage", "delete", "quit"])
    selected_meetings = iter([meeting, None])
    class Wizard:
        def show_home(self):
            pass
        def _choose_one(self, title, options):
            return next(selections)

    monkeypatch.setattr(cli, "TerminalWizard", Wizard)
    monkeypatch.setattr(cli, "_config_path", lambda value: str(tmp_path / "ensemble.toml"))
    monkeypatch.setattr(cli, "_select_existing_meeting", lambda *args: next(selected_meetings))
    monkeypatch.setattr(cli, "inspect_meeting", lambda path: SimpleNamespace(
        meeting_id=meeting.name, title="测试会议"
    ))
    monkeypatch.setattr(cli, "terminal_input", lambda prompt: f"DELETE {meeting.name}")
    queued = []
    monkeypatch.setattr(cli, "launch_background_deletion", lambda path: (
        queued.append(path) or tmp_path / "receipt.json", tmp_path / "delete.log", 123
    ))
    assert cli.cmd_home(SimpleNamespace(config=None)) == 0
    assert queued == [meeting]


def test_management_confirmation_cancel_returns_to_action_menu(monkeypatch, tmp_path):
    meeting = tmp_path / "LR-A1B2C3D4"
    meeting.mkdir()
    actions = iter(["delete", "back"])
    prompts = []
    class Wizard:
        def _choose_one(self, title, options):
            prompts.append(title)
            return next(actions)

    monkeypatch.setattr(cli, "inspect_meeting", lambda path: SimpleNamespace(
        meeting_id=meeting.name, title="测试会议"
    ))
    monkeypatch.setattr(cli, "terminal_input", lambda prompt: "b")
    monkeypatch.setattr(cli, "launch_background_deletion", lambda path: pytest.fail("deleted"))
    assert cli._manage_existing_meeting(Wizard(), meeting, tmp_path / "ensemble.toml") is None
    assert len(prompts) == 2


def test_archived_meeting_compact_is_noop_and_returns_to_management_menu(monkeypatch, tmp_path, capsys):
    meeting = tmp_path / "LR-A1B2C3D4"
    (meeting / "public").mkdir(parents=True)
    (meeting / "public/archive_manifest.json").write_text("{}", encoding="utf-8")
    actions = iter(["compact", "back"])
    prompts = []

    class Wizard:
        def _choose_one(self, title, options):
            prompts.append(options)
            return next(actions)

    monkeypatch.setattr(cli, "inspect_meeting", lambda path: SimpleNamespace(
        meeting_id=meeting.name, title="已归档会议"
    ))
    monkeypatch.setattr(cli, "_confirm_and_compact_meeting", lambda *_args: pytest.fail("re-archived"))
    assert cli._manage_existing_meeting(Wizard(), meeting, tmp_path / "ensemble.toml") is None
    assert len(prompts) == 2
    assert "已精简归档" in prompts[0][0][1]
    assert "无需再次操作" in capsys.readouterr().out


def test_management_compact_preflight_failure_keeps_menu_open(monkeypatch, tmp_path, capsys):
    meeting = tmp_path / "LR-A1B2C3D4"
    meeting.mkdir()
    actions = iter(["compact", "back"])

    class Wizard:
        def _choose_one(self, title, options):
            return next(actions)

    monkeypatch.setattr(cli, "inspect_meeting", lambda path: SimpleNamespace(
        meeting_id=meeting.name, title="测试会议"
    ))
    monkeypatch.setattr(cli, "_confirm_and_compact_meeting", lambda *_args: (_ for _ in ()).throw(
        ValueError("缺少完整文稿")
    ))
    assert cli._manage_existing_meeting(Wizard(), meeting, tmp_path / "ensemble.toml") is None
    assert "缺少完整文稿" in capsys.readouterr().err


def test_archiving_returns_to_meeting_list_without_exiting(monkeypatch, tmp_path):
    meeting = tmp_path / "LR-A1B2C3D4"
    meeting.mkdir()
    selections = iter(["manage", "compact", "quit"])
    selected_meetings = iter([meeting, None])
    class Wizard:
        def show_home(self):
            pass
        def _choose_one(self, title, options):
            return next(selections)

    monkeypatch.setattr(cli, "TerminalWizard", Wizard)
    monkeypatch.setattr(cli, "_config_path", lambda value: str(tmp_path / "ensemble.toml"))
    monkeypatch.setattr(cli, "_select_existing_meeting", lambda *args: next(selected_meetings))
    monkeypatch.setattr(cli, "inspect_meeting", lambda path: SimpleNamespace(
        meeting_id=meeting.name, title="测试会议"
    ))
    archived = []
    monkeypatch.setattr(cli, "_confirm_and_compact_meeting", lambda path, config: (
        archived.append(path) or True
    ))
    assert cli.cmd_home(SimpleNamespace(config=None)) == 0
    assert archived == [meeting]


def test_ctrl_c_prints_resume_command_and_writes_executable_script(
    monkeypatch, tmp_path, capsys
):
    meeting = tmp_path / "M-ABC12345"
    (meeting / "public").mkdir(parents=True)
    (meeting / "public" / "meeting_manifest.json").write_text(
        json.dumps({"meeting_id": "M-ABC12345", "title": "中文会议标题"}), encoding="utf-8"
    )
    launch_directory = tmp_path / "launch"
    launch_directory.mkdir()
    monkeypatch.chdir(launch_directory)

    def interrupted(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_run_report", interrupted)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ensemble", "run-report", "--meeting", str(meeting)],
    )

    assert cli.main() == 130
    rendered = capsys.readouterr().err
    command = f"ensemble resume {meeting.resolve()}"
    assert "恢复当前会议" in rendered
    assert command in rendered

    script = launch_directory / "M-ABC12345_resume.sh"
    assert script.exists()
    assert script.read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "# 会议 ID：M-ABC12345\n"
        "# 会议标题：中文会议标题\n"
        "printf '%s\\n' '恢复会议：M-ABC12345 · 中文会议标题'\n"
        f"exec {command}\n"
    )
    assert script.stat().st_mode & stat.S_IXUSR
    generic_script = launch_directory / "resume.sh"
    assert generic_script.read_text(encoding="utf-8") == script.read_text(encoding="utf-8")
    assert generic_script.stat().st_mode & stat.S_IXUSR


def test_ctrl_c_after_bare_home_uses_created_meeting_resume_context(
    monkeypatch, tmp_path, capsys
):
    meeting = tmp_path / "M-STARTED1"
    (meeting / "public").mkdir(parents=True)
    (meeting / "public" / "meeting_manifest.json").write_text(
        json.dumps({"meeting_id": "M-STARTED1"}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    def interrupted(args):
        args._resume_cmd = "run-general"
        args._resume_meeting = str(meeting)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_home", interrupted)
    monkeypatch.setattr(sys, "argv", ["ensemble"])

    assert cli.main() == 130
    rendered = capsys.readouterr().err
    assert "已经落盘的会议进度保持不变" in rendered
    assert f"ensemble resume {meeting.resolve()}" in rendered
    assert (tmp_path / "M-STARTED1_resume.sh").exists()
    assert (tmp_path / "resume.sh").exists()


def test_new_ctrl_c_writes_id_first_helper_and_preserves_legacy(monkeypatch, tmp_path):
    meeting = tmp_path / "M-OVERWRITE"
    (meeting / "public").mkdir(parents=True)
    (meeting / "public" / "meeting_manifest.json").write_text(
        json.dumps({"meeting_id": "M-OVERWRITE"}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    for name in ("resume_M-OVERWRITE.sh", "resume.sh"):
        (tmp_path / name).write_text(
            "#!/usr/bin/env bash\nexec ensemble run-general --meeting M-OVERWRITE\n",
            encoding="utf-8",
        )

    result = cli._write_resume_script(
        type(
            "Args",
            (),
            {
                "cmd": "run-general",
                "meeting": str(meeting),
                "config": None,
                "governance_docs": None,
                "max_output_tokens": None,
                "no_progress": False,
            },
        )()
    )

    assert result is not None
    expected = f"exec ensemble resume {meeting.resolve()}\n"
    assert expected in (tmp_path / "M-OVERWRITE_resume.sh").read_text(encoding="utf-8")
    assert expected in (tmp_path / "resume.sh").read_text(encoding="utf-8")
    assert "run-general --meeting" in (tmp_path / "resume_M-OVERWRITE.sh").read_text(encoding="utf-8")


def test_ctrl_r_menu_can_go_back_and_replace_more_than_once(monkeypatch):
    runtime = {"R-ONE": ("old", "a"), "CHAIR": ("old", "a")}
    replacements = []

    class Repo:
        def exclusive_run_lock(self):
            return nullcontext()

    class Service:
        def __init__(self, _repo):
            pass

        def replace(self, *, participant_id, provider_id, model_id, reason):
            replacements.append((participant_id, provider_id, model_id, reason))
            runtime[participant_id] = (provider_id, model_id)
            return {"participant_id": participant_id}

        def replace_all_using(self, *, from_provider_id, from_model_id, provider_id, model_id, reason):
            result = []
            for participant_id, value in runtime.items():
                if value == (from_provider_id, from_model_id):
                    replacements.append((participant_id, provider_id, model_id, reason))
                    runtime[participant_id] = (provider_id, model_id)
                    result.append({"participant_id": participant_id})
            return result

    answers = iter(("1", "1", "2", "y", "1", "测试更换", "y", "2", "2", "2", "y", "1", "再次更换", "n"))
    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: next(answers))
    monkeypatch.setattr(cli, "meeting_participant_ids", lambda _repo: list(runtime))
    monkeypatch.setattr(cli, "current_runtime_for", lambda _repo, participant: runtime[participant])
    monkeypatch.setattr(cli, "ModelReplacementService", Service)
    monkeypatch.setattr(cli, "discover_models", lambda _cfg, _providers: [
        ModelDescriptor(provider_id="old", model_id="a"),
        ModelDescriptor(provider_id="new", model_id="b"),
    ])
    cfg = SimpleNamespace(providers={
        "old": SimpleNamespace(enabled=True, display_name="Old"),
        "new": SimpleNamespace(enabled=True, display_name="New"),
    })
    cli._interactive_model_replacement(repo=Repo(), cfg=cfg)
    assert replacements == [
        ("R-ONE", "new", "b", "测试更换"),
        ("CHAIR", "new", "b", "再次更换"),
    ]


def test_ctrl_r_menu_q_safely_stops_without_replacement(monkeypatch):
    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: "q")
    with pytest.raises(KeyboardInterrupt):
        cli._interactive_model_replacement(repo=object(), cfg=object())


def test_parallel_ctrl_r_menu_can_request_force_stop(monkeypatch):
    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: "7")
    assert cli._interactive_model_replacement(
        repo=object(), cfg=object(), deferred=True
    ) == [{"kind": "force_stop"}]


def test_ctrl_r_can_queue_model_concurrency_change_for_safe_batch_boundary(tmp_path, monkeypatch):
    manifest = tmp_path / "identity_private/meeting_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"research_model": ["deepseek", "flash"],
                                    "model_concurrency_limits": {"deepseek:flash": 1}}),
                        encoding="utf-8")
    repo = SimpleNamespace(root=tmp_path)
    answers = iter(("4", "1", "4", "allow parallel synthesis"))
    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: next(answers))
    monkeypatch.setattr(cli, "meeting_participant_ids", lambda _repo: ["RESEARCH_DESK"])
    monkeypatch.setattr(cli, "current_runtime_for", lambda _repo, _person: ("deepseek", "flash"))
    monkeypatch.setattr(cli, "_meeting_concurrency_limits", lambda _repo, _cfg: {
        ("deepseek", "flash"): 1,
    })
    changes = cli._interactive_model_replacement(repo=repo, cfg=object(), deferred=True)
    assert changes == [{
        "kind": "runtime_control", "control_kind": "model_concurrency",
        "target": "deepseek:flash", "value": 4, "reason": "allow parallel synthesis",
    }]
    assert not (tmp_path / "human_private/runtime_controls").exists()


def test_ctrl_r_can_queue_tavily_quota_policy_for_existing_meeting(tmp_path, monkeypatch):
    from types import SimpleNamespace

    manifest = tmp_path / "identity_private/meeting_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"research_model": ["deepseek", "flash"]}), encoding="utf-8")
    repo = SimpleNamespace(root=tmp_path)
    cfg = SimpleNamespace(research=SimpleNamespace(tavily=SimpleNamespace(enabled=True)))
    answers = iter(("6", "2", "OpenAlex 持续限流，本次会议允许降级检索"))
    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: next(answers))

    changes = cli._interactive_model_replacement(repo=repo, cfg=cfg, deferred=True)

    assert changes == [{
        "kind": "runtime_control", "control_kind": "openalex_quota_policy",
        "target": None, "value": "tavily",
        "reason": "OpenAlex 持续限流，本次会议允许降级检索",
    }]
    assert not (tmp_path / "human_private/runtime_controls").exists()


@pytest.mark.parametrize("choice,scope", [
    ("1", "NEXT_FAILURE_ONLY"), ("2", "ON_FUTURE_FAILURES"),
])
def test_research_desk_content_rejection_offers_conditional_backup(monkeypatch, capsys, choice, scope):
    from project_ensemble.errors import ProviderContentRejectedError

    records = []

    class Repo:
        def exclusive_run_lock(self):
            return nullcontext()

    class Service:
        def __init__(self, _repo):
            pass

        def choose(self, **kwargs):
            records.append(kwargs)

    answers = iter((choice, "1", "y"))
    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: next(answers))
    monkeypatch.setattr(cli, "current_runtime_for", lambda _repo, _rid: ("deepseek", "deepseek-flash"))
    import project_ensemble.runtime.research_fallbacks as fallback_module
    monkeypatch.setattr(fallback_module, "ResearchFallbacks", Service)
    monkeypatch.setattr(cli, "discover_models", lambda _cfg, _providers: [
        ModelDescriptor(provider_id="deepseek", model_id="deepseek-flash"),
        ModelDescriptor(provider_id="codex", model_id="gpt-6-sol"),
    ])
    cfg = SimpleNamespace(providers={
        "deepseek": SimpleNamespace(enabled=True),
        "codex": SimpleNamespace(enabled=True),
    })
    error = ProviderContentRejectedError(
        "provider returned HTTP 400: Content Exists Risk",
        status_code=400, provider_request_id="abc-123",
    )
    assert cli._interactive_research_desk_fallback(repo=Repo(), cfg=cfg, error=error)
    assert len(records) == 1
    assert records[0]["source"] == ("deepseek", "deepseek-flash")
    assert records[0]["target"] == ("codex", "gpt-6-sol")
    assert records[0]["scope"] == scope
    assert "未完成项没有证据结论" in capsys.readouterr().err


def test_fast_research_failure_offers_retry_without_replacing_model(monkeypatch, capsys):
    from project_ensemble.errors import PermanentProviderError
    from project_ensemble.orchestration.literature_fast import FastResearchDeskFailure

    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: "1")
    monkeypatch.setattr(cli, "current_runtime_for", lambda _repo, _rid: ("deepseek", "deepseek-flash"))
    error = FastResearchDeskFailure("RM-02", 2, 1, PermanentProviderError("temporary policy error"))
    assert cli._interactive_research_desk_fallback(repo=object(), cfg=object(), error=error)
    output = capsys.readouterr().err
    assert "RM-02 · 第 2 轮 · 问题 1" in output
    assert "用当前模型重试未落盘项" in output


def test_openalex_search_failure_does_not_offer_model_replacement(monkeypatch, capsys):
    from project_ensemble.errors import OpenAlexQueryRejected
    from project_ensemble.orchestration.literature_fast import FastResearchDeskFailure

    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: "1")
    monkeypatch.setattr(cli, "current_runtime_for", lambda _repo, _rid: ("deepseek", "flash"))
    error = FastResearchDeskFailure(
        "RM-03", 1, 6, OpenAlexQueryRejected("OpenAlex search HTTP 400; invalid query"),
    )
    assert cli._interactive_research_desk_fallback(repo=object(), cfg=object(), error=error)
    output = capsys.readouterr().err
    assert "OpenAlex 检索后端" in output
    assert "只重新提交未落盘的检索" in output
    assert "备用模型" not in output


def test_openalex_failure_menu_can_record_human_search_rollback(monkeypatch, tmp_path):
    from project_ensemble.errors import OpenAlexQueryRejected
    from project_ensemble.orchestration.literature_fast import FastResearchDeskFailure
    from project_ensemble.storage.documents import ImmutableDocumentStore

    answers = iter(["r", "c", "use shorter topical searches"])
    monkeypatch.setattr(cli, "terminal_input", lambda _prompt: next(answers))
    monkeypatch.setattr(cli, "current_runtime_for", lambda _repo, _rid: ("deepseek", "flash"))
    repo = SimpleNamespace(
        root=tmp_path, meeting_id="LR-TEST", docs=ImmutableDocumentStore(tmp_path),
        events=SimpleNamespace(append=lambda *_args, **_kwargs: None),
    )
    error = FastResearchDeskFailure(
        "RM-03", 1, 6, OpenAlexQueryRejected("OpenAlex search HTTP 400"),
    )
    assert cli._interactive_research_desk_fallback(
        repo=repo, cfg=object(), error=error, run_lock_held=True,
    )
    record = tmp_path / "audit_private/research/fast_stages/FAST-RM-03-1-06-rollback-0001.json"
    assert json.loads(record.read_text())["direction"] == "use shorter topical searches"


def test_fast_report_retries_after_human_backup_choice(monkeypatch, tmp_path):
    from project_ensemble.errors import ProviderContentRejectedError

    class Repo:
        root = tmp_path

    manifest = tmp_path / "identity_private/meeting_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text('{"literature_writing_policy":"fast"}', encoding="utf-8")
    cfg = SimpleNamespace(project=SimpleNamespace(governance_docs="docs/governance"))
    calls = []

    def run_once_then_succeed(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ProviderContentRejectedError(
                "Content Exists Risk", status_code=400,
                provider_request_id="abc-123",
            )
        return "done"

    monkeypatch.setattr(cli, "_meeting_governance_docs", lambda _repo, value: value)
    monkeypatch.setattr(cli, "_assert_meeting_runtime_compatible", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_run_literature_report_locked", run_once_then_succeed)
    monkeypatch.setattr(cli, "_interactive_research_desk_fallback", lambda **_kwargs: True)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))

    assert cli._run_literature_report(repo=Repo(), cfg=cfg) == "done"
    assert len(calls) == 2


def test_legacy_general_entry_maps_to_frozen_report_runner(monkeypatch, tmp_path, capsys):
    meeting = tmp_path / "M-LEGACY"
    (meeting / "public").mkdir(parents=True)
    (meeting / "public" / "meeting_manifest.json").write_text(
        json.dumps(
            {
                "meeting_id": "M-LEGACY",
                "meeting_type": "deliberation",
                "deliverable_type": "literature_review",
            }
        ),
        encoding="utf-8",
    )
    cfg = SimpleNamespace(source_path=tmp_path / "ensemble.toml")
    observed = []
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    monkeypatch.setattr(cli, "register_meeting", lambda *_args: None)
    monkeypatch.setattr(
        cli,
        "_run_literature_report",
        lambda **kwargs: observed.append(kwargs),
    )
    args = SimpleNamespace(
        meeting=str(meeting),
        config=str(cfg.source_path),
        governance_docs=None,
        max_output_tokens=1234,
        no_progress=True,
        cmd="run-general",
    )

    assert cli.cmd_run_general(args) == 0
    assert len(observed) == 1
    assert observed[0]["repo"].root == meeting
    assert observed[0]["max_output_tokens"] == 1234
    assert observed[0]["show_progress"] is False
    assert "run-general → run-report" in capsys.readouterr().err


def test_interactive_consultation_prompts_once_records_ruling_and_threshold(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="task",
    )
    service = HumanConsultationService(repo)
    service.open_issue(
        HumanConsultationIssue(
            issue_id="HC-W001-STEP001-CONFLICT",
            meeting_id=repo.meeting_id,
            reason_code="OPERATIVE_AMENDMENT_CONFLICT",
            stage="GENERAL_PRINCIPLE_SEQUENTIAL_AMENDMENT",
            question="顺序二元表决还是延期？",
            options=["SEQUENTIAL_BINARY_WITH_CURRENT_TEXT", "DEFER_CURRENT_AMENDMENT"],
            affected_items=["A-1", "A-2"],
            thresholds=[
                ThresholdRecord(
                    name="supermajority",
                    formula="ceil(3N/4)",
                    comparison=">=",
                    eligible_count=12,
                    required_votes=9,
                )
            ],
        )
    )
    answers = iter(["1", "仅适用于本次冲突。"])
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(
        repo,
        input_fn=lambda _: next(answers),
        output=output,
    )
    resolution = service.resolution("HC-W001-STEP001-CONFLICT")
    assert resolution.decision == "SEQUENTIAL_BINARY_WITH_CURRENT_TEXT"
    assert resolution.thresholds[0].required_votes == 9
    assert "9/12" in output.getvalue()
    assert "先按当前文本表决第一项" in output.getvalue()
    assert "SEQUENTIAL_BINARY_WITH_CURRENT_TEXT" not in output.getvalue()
    assert not service.open_issues()


@pytest.mark.parametrize("reason_code", [
    "LITERATURE_LOCAL_SCIENCE_CHECK_REQUIRED",
    "LITERATURE_LOCAL_SCIENCE_CHECK_REOPENED",
])
def test_local_science_consultation_shows_frozen_problem_and_changed_text(tmp_path, reason_code):
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Review a module.",
    )
    relative = "public/literature_report/modules/RM-10/writing_v071"
    repo.docs.write_once(
        f"{relative}/writer_v2_validated.json",
        json.dumps({"draft": {"body_markdown": "能量零点对照旧表述。"}}, ensure_ascii=False),
    )
    repo.docs.write_once(
        f"{relative}/writer_v3_validated.json",
        json.dumps({"draft": {"body_markdown": "能量零点平移后总是不变。"}}, ensure_ascii=False),
    )
    repo.docs.write_once(
        f"{relative}/final_local_science_check_1.json",
        json.dumps({"status": "MATERIAL_PROBLEM", "checks": [
            {"status": "MATERIAL_PROBLEM", "problem": "能量零点平移只有粒子数配平才保持不变。"}
        ]}, ensure_ascii=False),
    )
    HumanConsultationService(repo).open_issue(HumanConsultationIssue(
        issue_id="HC-LW-RM-10-LOCAL-01", meeting_id=repo.meeting_id,
        reason_code=reason_code,
        stage="LITERATURE_MODULE_REVIEW", question="是否修复？",
        options=["RETURN_TO_WRITER_LOCAL_REPAIR", "PUBLISH_WITH_DISCLOSED_LIMITATION"],
        affected_items=["RM-10"],
        context={"check_path": f"{relative}/final_local_science_check_1.json"},
    ))
    output = io.StringIO()
    assert not cli._prompt_for_one_consultation(
        repo, input_fn=lambda _prompt: "", output=output,
    )
    shown = output.getvalue()
    assert "粒子数配平" in shown
    assert "能量零点对照旧表述" in shown
    assert "能量零点平移后总是不变" in shown
    assert "按 v 可展开" in shown


def test_local_science_stay_paused_does_not_freeze_an_unrecoverable_resolution(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Review a module.",
    )
    issue_id = "HC-LW-RM-10-LOCAL-02"
    service = HumanConsultationService(repo)
    service.open_issue(HumanConsultationIssue(
        issue_id=issue_id, meeting_id=repo.meeting_id,
        reason_code="LITERATURE_LOCAL_SCIENCE_CHECK_REQUIRED",
        stage="LITERATURE_MODULE_REVIEW", question="第二次复核仍有疑点",
        options=["PAUSE_FOR_MANUAL_REVIEW", "PUBLISH_WITH_DISCLOSED_LIMITATION"],
        affected_items=["RM-10"],
    ))
    output = io.StringIO()
    assert not cli._prompt_for_one_consultation(
        repo, input_fn=lambda _prompt: "1", output=output,
    )
    assert service.resolution(issue_id) is None
    assert service.open_issues()[0].issue_id == issue_id
    assert "下次恢复可重新选择" in output.getvalue()


def test_interactive_consultation_allows_one_question_to_chair_before_choice(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="task",
    )
    service = HumanConsultationService(repo)
    service.open_issue(
        HumanConsultationIssue(
            issue_id="HC-W001-STEP001-CONFLICT",
            meeting_id=repo.meeting_id,
            reason_code="CONFLICT",
            stage="GENERAL_PRINCIPLE_SEQUENTIAL_AMENDMENT",
            question="两项是否真正互斥？",
            options=[
                "SEQUENTIAL_BINARY_WITH_CURRENT_TEXT",
                "DEFER_CURRENT_AMENDMENT",
                "REQUEST_CHAIR_RECLASSIFICATION",
            ],
            affected_items=["A-1", "A-2"],
        )
    )

    class Notifier:
        enabled = False

    engine = MeetingEngine(
        repo=repo,
        adapters={
            "fake": ScriptedProviderAdapter(
                "fake",
                ["m"],
                ["这里的互斥指条文竞争，不一定表示科学命题相反。"],
            )
        },
        notifier=Notifier(),
    )
    answers = iter(["c", "这里的互斥是否表示科学结论相反？", "3", "请重新检查分类。"])
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(
        repo,
        input_fn=lambda _: next(answers),
        output=output,
        engine=engine,
        governance_docs="docs/governance",
        max_output_tokens=512,
    )
    assert "主席（第 1 次解释）" in output.getvalue()
    assert "科学命题相反" in output.getvalue()
    assert service.resolution("HC-W001-STEP001-CONFLICT").decision == "REQUEST_CHAIR_RECLASSIFICATION"


def test_science_objection_choice_needs_no_markdown_or_written_rationale(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Render a review.",
    )
    service = HumanConsultationService(repo)
    issue = HumanConsultationIssue(
        issue_id="HC-SR-SR-001-C001-O001",
        meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
        stage="SCHOLARLY_SCIENCE_REVIEW",
        question="第一条异议：结论强度过高。",
        options=[
            "ACCEPT_SCIENCE_OBJECTION", "REJECT_SCIENCE_OBJECTION",
            "DIRECT_CHAIR_SCIENCE_REVISION",
        ],
    )
    service.open_issue(issue)
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(
        repo, input_fn=lambda _prompt: "1", output=output,
    )
    resolution = service.resolution(issue.issue_id)
    assert resolution.decision == "ACCEPT_SCIENCE_OBJECTION"
    assert resolution.human_wording is None
    assert "逐条咨询" in output.getvalue()
    assert "查看完整原文" in output.getvalue()


def test_english_consultation_translates_controls_not_frozen_question(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "interface_language", lambda: "en")
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="A bounded task.",
    )
    issue = HumanConsultationIssue(
        issue_id="HC-EN-1", meeting_id=repo.meeting_id,
        reason_code="TEST", stage="GENERAL_PRINCIPLE_SEQUENTIAL_AMENDMENT",
        question="这段冻结问题保留中文。",
        options=["SEQUENTIAL_BINARY_WITH_CURRENT_TEXT", "DEFER_CURRENT_AMENDMENT"],
    )
    HumanConsultationService(repo).open_issue(issue)
    output = io.StringIO()
    prompts = []
    def pause(prompt):
        prompts.append(prompt)
        return ""
    assert not cli._prompt_for_one_consultation(
        repo, input_fn=pause, output=output,
    )
    rendered = output.getvalue()
    assert "Vote on the first item" in rendered
    assert "Choose a number" in prompts[0]
    assert "这段冻结问题保留中文。" in rendered


def test_science_consultation_uses_comparison_view_when_context_exists(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Render a review.",
    )
    service = HumanConsultationService(repo)
    issue_id = "HC-SR-SR-001-C001-O001"
    service.open_issue(HumanConsultationIssue(
        issue_id=issue_id,
        meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
        stage="SCHOLARLY_SCIENCE_REVIEW",
        question="旧格式长问题：不应直接打印在对照界面。",
        options=["ACCEPT_SCIENCE_OBJECTION", "REJECT_SCIENCE_OBJECTION"],
    ))
    repo.docs.write_once(
        "public/scholarly_rendering/sections/SR-001/science_human_context_c001_o001.json",
        json.dumps({
            "cycle": 1, "objection_number": 1, "objection_count": 1,
            "source_markdown": "完整原文不在预览中。",
            "current_redraw": "当前稿完整正文不在预览中。",
            "objection": {
                "issue": "此处结论太强。",
                "current_excerpt": "结论十分确定。",
                "proposed_wording": "结论仍有不确定性。",
            },
            "chair_advice": {"suggestion": "建议保留不确定性。"},
        }, ensure_ascii=False),
    )
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(
        repo, input_fn=lambda _prompt: "2", output=output,
    )
    shown = output.getvalue()
    assert shown.index("当前稿") < shown.index("修正意见") < shown.index("主席建议")
    assert "结论十分确定。" in shown
    assert "结论仍有不确定性。" in shown
    assert "旧格式长问题" not in shown
    assert "完整原文不在预览中" not in shown
    assert service.resolution(issue_id).decision == "REJECT_SCIENCE_OBJECTION"


def test_science_consultation_view_full_includes_unabridged_chair_advice(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Render a review.",
    )
    issue_id = "HC-SR-SR-001-C001-O001"
    HumanConsultationService(repo).open_issue(HumanConsultationIssue(
        issue_id=issue_id, meeting_id=repo.meeting_id,
        reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
        stage="SCHOLARLY_SCIENCE_REVIEW", question="Read this advice.",
        options=["ACCEPT_SCIENCE_OBJECTION", "REJECT_SCIENCE_OBJECTION"],
    ))
    long_advice = "建议" * 900
    repo.docs.write_once(
        "public/scholarly_rendering/sections/SR-001/science_human_context_c001_o001.json",
        json.dumps({
            "source_markdown": "原文。", "current_redraw": "当前稿。",
            "objection": {"issue": "待核对。", "current_excerpt": "当前稿。"},
            "chair_advice": {"suggestion": long_advice},
        }, ensure_ascii=False),
    )
    answers = iter(["v", "2"])
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(
        repo, input_fn=lambda _prompt: next(answers), output=output,
    )
    assert "修正意见或主席建议已折叠" in output.getvalue()
    assert long_advice in output.getvalue()


def test_science_objections_are_decided_as_one_batch_before_resuming(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs="docs/governance",
        task_description="Render a review.",
    )
    service = HumanConsultationService(repo)
    issue_ids = [f"HC-SR-SR-001-C001-O{index:03d}" for index in (1, 2)]
    for issue_id in issue_ids:
        service.open_issue(HumanConsultationIssue(
            issue_id=issue_id,
            meeting_id=repo.meeting_id,
            reason_code="SCHOLARLY_RENDERING_SCIENCE_MAJORITY_NOT_REACHED",
            stage="SCHOLARLY_SCIENCE_REVIEW",
            question=f"异议 {issue_id}",
            options=["ACCEPT_SCIENCE_OBJECTION", "REJECT_SCIENCE_OBJECTION"],
        ))
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(
        repo, input_fn=lambda _prompt: "1", output=output,
    )
    assert [issue.issue_id for issue in service.open_issues()] == issue_ids[1:]
    assert "全部处理后主席才会修稿" in output.getvalue()
    assert cli._prompt_for_one_consultation(
        repo, input_fn=lambda _prompt: "2", output=output,
    )
    assert not service.open_issues()
    assert service.resolution(issue_ids[0]).decision == "ACCEPT_SCIENCE_OBJECTION"
    assert service.resolution(issue_ids[1]).decision == "REJECT_SCIENCE_OBJECTION"


def test_outline_review_allows_scope_dialogue_before_human_replanning(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="Review a broad literature topic.",
    )
    service = HumanConsultationService(repo)
    service.open_issue(
        HumanConsultationIssue(
            issue_id="HC-LROUTLINE-C001",
            meeting_id=repo.meeting_id,
            reason_code="HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED",
            stage="LITERATURE_RESEARCH_OUTLINE",
            question="主席转呈：规划代表认为原始范围过宽；请决定是否调整。",
            options=["APPROVE_OUTLINE", "REJECT_AND_REPLAN"],
        )
    )

    class Notifier:
        enabled = False

    engine = MeetingEngine(
        repo=repo,
        adapters={
            "fake": ScriptedProviderAdapter(
                "fake", ["m"], ["代表只能提出异议；是否缩小范围由你决定。"]
            )
        },
        notifier=Notifier(),
    )
    answers = iter([
        "c",
        "如果范围缩小，哪些模块需要重做？",
        "2",
        "将任务缩小为可核查的两个研究方向。",
    ])
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(
        repo,
        input_fn=lambda _: next(answers),
        output=output,
        engine=engine,
        governance_docs="docs/governance",
    )
    assert "研究总纲 · 人工审阅" in output.getvalue()
    assert "与主席讨论研究范围或模块划分" in output.getvalue()
    assert "代表只能提出异议" in output.getvalue()
    assert service.resolution("HC-LROUTLINE-C001").decision == "REJECT_AND_REPLAN"
    exchanges = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (repo.root / "governance_private/provider_exchanges").glob("X-*.json")
    ]
    dialogue = next(
        item for item in exchanges if item["stage"] == "human_consultation_explanation"
    )
    assert "Neither the Representatives nor Chair may narrow" in dialogue["request"]["user_text"]


def test_outline_review_displays_structured_map_and_expands_details_on_demand(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "m")],
        chair_model=("fake", "m"),
        governance_docs="docs/governance",
        task_description="Review a literature topic.",
    )
    relative = "public/literature_report/frozen_research_outline.json"
    path = repo.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "report_title": "非平衡界面方法综述",
        "modules": [
            {"module_id": "RM-01", "title": "测量对象", "research_questions": ["什么量可比？"]},
            {"module_id": "RM-02", "title": "力学路线", "required_evidence": ["原始论文"]},
        ],
        "scope_concern_notice": "前言：(1) RM-02 需控制范围。(2) 待核文献保留现状标注。",
    }, ensure_ascii=False), encoding="utf-8")
    HumanConsultationService(repo).open_issue(HumanConsultationIssue(
        issue_id="HC-LROUTLINE-C001", meeting_id=repo.meeting_id,
        reason_code="HUMAN_RESEARCH_OUTLINE_REVIEW_REQUIRED",
        stage="LITERATURE_RESEARCH_OUTLINE",
        question="LONG_BODY_UNIQUE：原始冻结咨询文书很长。",
        options=["APPROVE_OUTLINE", "REJECT_AND_REPLAN", "REVISE_SKELETON_ONLY"],
        context={
            "outline_path": relative,
            "article_skeleton": ["摘要", "第 1 章", "结论"],
            "proposed_disciplines": ["非平衡统计物理"],
        },
    ))
    answers = iter(("m", "2", "s", "a", "d", "v", ""))
    output = io.StringIO()
    assert not cli._prompt_for_one_consultation(
        repo, input_fn=lambda _prompt: next(answers), output=output,
    )
    rendered = output.getvalue()
    assert "RM-01  测量对象" in rendered
    assert "RM-02  力学路线" in rendered
    assert "范围意见 2 项" in rendered
    assert "文章骨架 3 项" in rendered
    assert "所需证据" in rendered and "原始论文" in rendered
    assert "完整范围意见" in rendered
    assert "建议学科" in rendered
    assert "冻结的原始咨询问题" in rendered and "LONG_BODY_UNIQUE" in rendered
    assert "保持 PAUSED" in rendered


def test_fast_taskbook_approval_confirms_disciplines_and_reader_proficiency(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")], chair_model=("fake", "m"),
        governance_docs="docs/governance", task_description="研究界面",
    )
    candidate = repo.root / "public/literature_report/fast/candidate_01.json"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(json.dumps({
        "outline": {"report_title": "界面综述", "scope_note": "二维界面",
                    "proposed_disciplines": ["统计物理"], "modules": []},
    }, ensure_ascii=False), encoding="utf-8")
    service = HumanConsultationService(repo)
    service.open_issue(HumanConsultationIssue(
        issue_id="HC-FAST-APPROVE-01", meeting_id=repo.meeting_id,
        reason_code="FAST_TASKBOOK_APPROVAL_HUMAN_REQUIRED",
        stage="FAST_TASKBOOK_APPROVAL", question="请确认任务书",
        options=["APPROVE_TASKBOOK", "REVISE_TASKBOOK"],
        context={"cycle": 1, "taskbook_path": str(candidate.relative_to(repo.root)),
                 "proposed_disciplines": ["统计物理"]},
    ))
    answers = iter(["1", "", "2", "", ""])
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(
        repo, input_fn=lambda _: next(answers), output=output,
    )
    profile = json.loads((repo.root / "public/literature_report/audience_profile-FAST-001.json")
                         .read_text(encoding="utf-8"))
    assert profile["disciplines"] == {"统计物理": 2}
    assert profile["glossary_appendix"] is True
    assert service.resolution("HC-FAST-APPROVE-01").decision == "APPROVE_TASKBOOK"
    assert service.resolution("HC-FAST-APPROVE-01").rationale.endswith("no additional note.")


def test_fast_taskbook_approval_accepts_desired_module_count(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")], chair_model=("fake", "m"),
        governance_docs="docs/governance", task_description="研究界面",
    )
    candidate = repo.root / "public/literature_report/fast/candidate_01.json"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(json.dumps({
        "outline": {"report_title": "界面综述", "scope_note": "二维界面",
                    "modules": [{"title": "问题一"}, {"title": "问题二"}]},
    }, ensure_ascii=False), encoding="utf-8")
    service = HumanConsultationService(repo)
    service.open_issue(HumanConsultationIssue(
        issue_id="HC-FAST-APPROVE-01", meeting_id=repo.meeting_id,
        reason_code="FAST_TASKBOOK_APPROVAL_HUMAN_REQUIRED",
        stage="FAST_TASKBOOK_APPROVAL", question="请确认任务书",
        options=["APPROVE_TASKBOOK", "REVISE_TASKBOOK"],
        context={"cycle": 1, "taskbook_path": str(candidate.relative_to(repo.root))},
    ))
    answers = iter(["2", "13", "9"])
    output = io.StringIO()
    assert cli._prompt_for_one_consultation(
        repo, input_fn=lambda _: next(answers), output=output,
    )
    resolution = service.resolution("HC-FAST-APPROVE-01")
    assert resolution.decision == "REVISE_TASKBOOK"
    assert "恰好 9 个" in resolution.rationale
    assert "模块总数须在 1–12 之间" in output.getvalue()


def test_writer_default_delegation_needs_no_second_human_prompt(tmp_path):
    repo = MeetingRepository.create(
        tmp_path / "ws", selected_models=[("fake", "m")], chair_model=("fake", "m"),
        governance_docs="docs/governance", task_description="研究界面",
    )
    service = HumanConsultationService(repo)
    service.open_issue(HumanConsultationIssue(
        issue_id="HC-FAST-PLAN-01", meeting_id=repo.meeting_id,
        reason_code="FAST_TASKBOOK_QUESTION_HUMAN_REQUIRED",
        stage="FAST_TASKBOOK_QUESTION", question="如何设定读者范围？",
        options=["USE_HUMAN_WORDING", "USE_WRITER_DEFAULT"],
        context={"cycle": 1},
    ))
    answers = iter(["2"])
    assert cli._prompt_for_one_consultation(
        repo, input_fn=lambda _: next(answers), output=io.StringIO(),
    )
    assert service.resolution("HC-FAST-PLAN-01").decision == "USE_WRITER_DEFAULT"
