import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_ensemble import cli
from project_ensemble.config import EnsembleConfig
from project_ensemble.domain import MeetingType
from project_ensemble.runtime import slurm_launch as launch
from project_ensemble.startup import StartupSelection
from project_ensemble.storage.documents import ImmutableDocumentStore


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "a meeting's folder" / "LR-TEST"
    root.mkdir(parents=True)
    return SimpleNamespace(root=root, meeting_id="LR-TEST", docs=ImmutableDocumentStore(root))


@pytest.mark.parametrize("command", ["run-report", "run-render", "run-general", "run-research"])
def test_portable_resume_script_is_scheduler_independent_and_safe(repo, tmp_path, monkeypatch, command):
    config = tmp_path / "config with ' quote.toml"
    monkeypatch.setenv("PRIVATE_API_KEY", "secret-not-in-script")
    script = launch.ensure_resume_script(repo, config_path=config, command=command)
    body = script.read_text()
    subprocess.run(["bash", "-n", str(script)], check=True)
    assert "#SBATCH" not in body
    assert "secret-not-in-script" not in body
    assert str(repo.root) not in body
    assert "ensemble resume" not in body
    assert command in body
    assert ("--no-progress" in body) == (command != "run-research")
    assert script.stat().st_mode & 0o777 == 0o700
    assert launch.ensure_resume_script(repo, config_path=config, command=command) == script
    script.write_text("manually changed")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        launch.ensure_resume_script(repo, config_path=config, command=command)


def test_resume_script_uses_its_moved_directory(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(launch.sys, "executable", "/bin/echo")
    launch.ensure_resume_script(repo, config_path=tmp_path / "ensemble.toml", command="run-report")
    moved = tmp_path / "moved with spaces"
    repo.root.rename(moved)
    result = subprocess.run(["bash", str(moved / "resume_backstage.sh")], capture_output=True, text=True, check=True)
    assert f"--meeting {moved}" in result.stdout
    assert str(repo.root) not in result.stdout


def test_slurm_plan_uses_common_script_and_correct_agent_limits(repo, tmp_path):
    plan = launch.prepare_slurm_launch(repo, config_path=tmp_path / "config.toml", command="run-report")
    assert plan.script == repo.root / "resume_backstage.sh"
    assert plan.argv[-1] == str(plan.script)
    assert "--partition=agent" in plan.argv
    assert "--cpus-per-task=1" in plan.argv
    assert "--mem=8G" in plan.argv
    assert "--time=7-00:00:00" in plan.argv
    assert "--input=/dev/null" in plan.argv
    assert shlex.split(plan.shell_command) == list(plan.argv)


@pytest.mark.parametrize("choice, expected", [("", "local"), ("1", "local"), ("3", "later"), ("q", "later")])
def test_menu_does_not_launch_without_explicit_choice(repo, tmp_path, monkeypatch, choice, expected):
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: pytest.fail("must not submit"))
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start"))
    assert launch.offer_initialized_execution(repo, config_path=tmp_path / "c.toml", command="run-report",
        input_fn=lambda _: choice, output=lambda _: None) == expected


def test_empty_confirmation_does_not_submit(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(launch.shutil, "which", lambda _: "/opt/slurm/sbatch")
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: pytest.fail("must not submit"))
    answers = iter(["2", "", "3"])
    assert launch.offer_initialized_execution(repo, config_path=tmp_path / "c.toml", command="run-report",
        input_fn=lambda _: next(answers), output=lambda _: None) == "later"


def test_slurm_submission_is_once_and_receipt_is_private(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(launch.shutil, "which", lambda _: "/opt/slurm/sbatch")
    calls = []
    def submit(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="12345;cluster\n", stderr="")
    monkeypatch.setattr(launch.subprocess, "run", submit)
    answers = iter(["2", "y"])
    output = []
    assert launch.offer_initialized_execution(repo, config_path=tmp_path / "c.toml", command="run-report",
        input_fn=lambda _: next(answers), output=output.append) == "slurm"
    assert len(calls) == 1
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    receipts = list((repo.root / "human_private/slurm").glob("*.submitted.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["job_id"] == "12345"
    assert any("slurm-12345.log" in line for line in output)


@pytest.mark.parametrize("failure", ["denied", "timeout", "invalid_stdout", "oserror"])
def test_submit_failure_does_not_retry_or_fall_back_to_local_run(repo, tmp_path, monkeypatch, failure):
    monkeypatch.setattr(launch.shutil, "which", lambda _: "/opt/slurm/sbatch")
    calls = []
    def submit(argv, **kwargs):
        calls.append(argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        if failure == "oserror":
            raise OSError("unavailable")
        return SimpleNamespace(returncode=1 if failure == "denied" else 0,
                               stdout="unrecognized" if failure == "invalid_stdout" else "",
                               stderr="QoS refused")
    monkeypatch.setattr(launch.subprocess, "run", submit)
    answers = iter(["2", "y"])
    assert launch.offer_initialized_execution(repo, config_path=tmp_path / "c.toml", command="run-report",
        input_fn=lambda _: next(answers), output=lambda _: None) == "later"
    assert len(calls) == 1


def test_no_slurm_keeps_script_for_manual_submission(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(launch.shutil, "which", lambda _: None)
    assert launch.offer_initialized_execution(repo, config_path=tmp_path / "c.toml", command="run-report",
        input_fn=lambda _: "2", output=lambda _: None) == "later"
    assert (repo.root / "resume_backstage.sh").is_file()


def test_local_background_is_detached_and_logged(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(launch.shutil, "which", lambda _: "/usr/bin/nohup")
    calls = []
    def start(argv, **kwargs):
        calls.append((argv, kwargs))
        kwargs["stdout"].write("test output")
        return SimpleNamespace(pid=54321)
    monkeypatch.setattr(launch.subprocess, "Popen", start)
    answers = iter(["4", "y"])
    assert launch.offer_initialized_execution(repo, config_path=tmp_path / "c.toml", command="run-report",
        input_fn=lambda _: next(answers), output=lambda _: None) == "background"
    assert len(calls) == 1
    assert calls[0][0][0:2] == ["/usr/bin/nohup", "bash"]
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    records = list((repo.root / "human_private/background").glob("*.started.json"))
    assert json.loads(records[0].read_text())["pid"] == 54321
    log = next((repo.root / "human_private/background").glob("*.log"))
    assert log.read_text() == "test output"
    assert log.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", ["local", "later", "slurm", "background"])
def test_interactive_initialization_dispatches_only_selected_local_run(repo, tmp_path, monkeypatch, mode):
    config = EnsembleConfig()
    config._source_path = tmp_path / "ensemble.toml"
    selection = StartupSelection(
        meeting_type=MeetingType.DELIBERATION, providers=("fake",),
        models=(("fake", "m"),), chair_model=("fake", "m"),
        task_description="task", escalation_email=None,
    )
    repo.docs.write_once("identity_private/meeting_manifest.json", "{}")
    monkeypatch.setattr(cli, "_load_config", lambda _: config)
    monkeypatch.setattr(cli, "TerminalWizard", lambda: SimpleNamespace(
        collect=lambda *a, **k: selection, discovered_catalog=[],
    ))
    monkeypatch.setattr(cli, "start_meeting", lambda *a, **k: repo)
    monkeypatch.setattr(cli, "register_meeting", lambda *a: None)
    monkeypatch.setattr(launch, "offer_initialized_execution", lambda *a, **k: mode)
    started = []
    monkeypatch.setattr(cli, "_run_general", lambda **k: started.append(repo.meeting_id))
    monkeypatch.setattr(cli, "_offer_compaction_after_completion", lambda *a: None)
    args = cli._interactive_start_arguments(SimpleNamespace(config=str(config.source_path)))
    assert cli.cmd_start(args) == 0
    assert started == ([repo.meeting_id] if mode == "local" else [])
    assert (repo.root / "resume_backstage.sh").is_file()
