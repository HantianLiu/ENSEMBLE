"""Explicit, post-initialization Slurm launch; never makes scientific decisions."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from project_ensemble.startup import terminal_input


_RUNNERS = {"run-report", "run-render", "run-general", "run-research"}


@dataclass(frozen=True)
class SlurmLaunch:
    script: Path
    log_pattern: Path
    argv: tuple[str, ...]
    attempt_id: str

    @property
    def shell_command(self) -> str:
        return shlex.join(self.argv)


def ensure_resume_script(repo, *, config_path, command: str, governance_docs=None) -> Path:
    """A scheduler-independent entry point that follows the meeting on moves."""
    if command not in _RUNNERS:
        raise ValueError("unsupported Slurm meeting runner")
    root = repo.root.resolve()
    source_root = Path(__file__).resolve().parents[2]
    invocation = [
        sys.executable, "-m", "project_ensemble.cli", command,
    ]
    arguments = ["--config", str(Path(config_path).resolve())]
    if command != "run-research":
        arguments.append("--no-progress")
        if governance_docs is not None:
            arguments.extend(["--governance-docs", str(Path(governance_docs).resolve())])
    # Slurm inherits the submitting environment. Only the import path, not
    # environment values or API credentials, is written into the script.
    body = "\n".join([
        "#!/bin/bash",
        "set -euo pipefail",
        "umask 077",
        'meeting_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"',
        'cd -- "$meeting_dir"',
        "export PYTHONUNBUFFERED=1",
        f'export PYTHONPATH={shlex.quote(str(source_root))}' + '$' + '{PYTHONPATH:+:$PYTHONPATH}',
        "exec " + shlex.join(invocation) + ' --meeting "$meeting_dir" ' + shlex.join(arguments),
        "",
    ])
    script = root / "resume_backstage.sh"
    if script.exists():
        if script.is_symlink() or script.read_text(encoding="utf-8") != body:
            raise ValueError("existing resume_backstage.sh differs; refusing to overwrite")
        return script
    script = repo.docs.write_once("resume_backstage.sh", body)
    if os.name == "posix":
        script.chmod(0o700)
    return script


def prepare_slurm_launch(repo, *, config_path, command: str, governance_docs=None) -> SlurmLaunch:
    root = repo.root.resolve()
    script = ensure_resume_script(
        repo, config_path=config_path, command=command, governance_docs=governance_docs,
    )
    directory = root / "human_private/slurm"
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        directory.chmod(0o700)
    attempt_id = uuid4().hex
    log_pattern = directory / "slurm-%j.log"
    argv = (
        "sbatch", "--parsable", f"--job-name=ensemble-{repo.meeting_id}",
        "--partition=agent", "--nodes=1", "--ntasks=1",
        "--cpus-per-task=1", "--mem=8G", "--time=7-00:00:00",
        "--export=ALL", "--input=/dev/null", f"--chdir={root}",
        f"--output={log_pattern}", str(script),
    )
    return SlurmLaunch(script=script, log_pattern=log_pattern, argv=argv, attempt_id=attempt_id)


def launch_local_background(repo, script: Path) -> tuple[int, Path]:
    """Detach this machine's process, not the lifetime of a Slurm allocation."""
    nohup = shutil.which("nohup")
    if nohup is None:
        raise OSError("nohup is unavailable")
    directory = repo.root.resolve() / "human_private/background"
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        directory.chmod(0o700)
    attempt_id = uuid4().hex
    log = directory / f"background-{attempt_id}.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as outgoing:
        process = subprocess.Popen(
            [nohup, "bash", str(script)], cwd=repo.root.resolve(),
            stdin=subprocess.DEVNULL, stdout=outgoing, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        repo.docs.write_once(
            f"human_private/background/{attempt_id}.started.json",
            json.dumps({"pid": process.pid, "log_path": str(log),
                        "script_path": str(script),
                        "started_at": datetime.now(timezone.utc).isoformat()}) + "\n",
        )
    except OSError:
        # Already started: a missing receipt must not cause duplicate launches.
        pass
    return process.pid, log


def offer_initialized_execution(
    repo, *, config_path, command: str, governance_docs=None,
    input_fn=None, output=print, language="zh",
) -> str:
    """Return local/later/slurm/background; no automatic submission or retry."""
    input_fn = input_fn or terminal_input

    def say(zh, en):
        output(en if language == "en" else zh)

    while True:
        say(
            "\n┌─ 会议已初始化 · 选择运行方式 ──────────────────\n"
            "│ 1. 在当前终端立即运行（默认）\n"
            "│ 2. 提交到 Slurm agent 分区，在后台运行\n"
            "│ 3. 稍后运行；保留已初始化会议\n"
            "│ 4. 在当前机器静默后台运行（nohup；不申请资源）\n"
            "└──────────────────────────────────────────────",
            "\nMeeting initialized · choose how to run\n"
            "  1. Run now in this terminal (default)\n"
            "  2. Submit a background job to the Slurm agent partition\n"
            "  3. Run later; keep the initialized meeting\n"
            "  4. Run silently in the background on this machine (nohup; no allocation)",
        )
        try:
            choice = input_fn("Choose 1–4 [1]: " if language == "en" else "选择编号（回车立即运行）: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "3"
        if choice in {"", "1"}:
            return "local"
        if choice in {"3", "b", "q"}:
            say("会议已保留；稍后可从“接续会议”继续。", "Meeting kept; use Open existing meeting to continue later.")
            return "later"
        if choice == "4":
            say(
                "这只在当前机器启动后台进程，不申请 Slurm 资源；请勿在集群登录节点跑长任务。\n"
                "nohup 不保证 salloc 退出后保活。人工咨询仍会暂停；请勿重复启动同一会议。",
                "This starts a process on this machine without allocating Slurm resources; do not run long jobs on login nodes.\n"
                "nohup does not keep an allocation alive after salloc exits. Human consultations still pause; avoid duplicate launches.",
            )
            try:
                answer = input_fn("Start background process? [y/N]: " if language == "en" else "启动当前机器的后台进程？[y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in {"y", "yes", "是"}:
                continue
            try:
                script = ensure_resume_script(repo, config_path=config_path, command=command, governance_docs=governance_docs)
                pid, log = launch_local_background(repo, script)
            except (OSError, ValueError) as exc:
                say(f"后台启动未完成：{exc}；会议已保留，未自动重试。", f"Background start failed: {exc}; meeting kept, no automatic retry.")
                return "later"
            say(f"后台进程已启动，PID={pid}；这不表示会议已完成。", f"Background process started, PID={pid}; this does not mean the meeting is complete.")
            output("tail -f " + shlex.quote(str(log)))
            return "background"
        if choice != "2":
            say("请输入 1、2、3 或 4。", "Enter 1, 2, 3, or 4.")
            continue

        try:
            plan = prepare_slurm_launch(
                repo, config_path=config_path, command=command,
                governance_docs=governance_docs,
            )
        except (OSError, ValueError) as exc:
            say(f"无法生成提交脚本：{exc}；会议已保留。", f"Could not prepare the submission script: {exc}; meeting kept.")
            return "later"
        say("资源：agent · 1 CPU · 8GB · 最长 7 天。", "Resources: agent · 1 CPU · 8GB · up to 7 days.")
        say(f"日志：{plan.log_pattern}", f"Log: {plan.log_pattern}")
        say(
            "后台没有交互菜单：必须人工处理且未获 AI 代裁授权的咨询会保留进度并暂停。\n"
            "Slurm 的 COMPLETED 不等于报告完成；请查看日志。请勿同时在前台运行同一会议。",
            "No interactive menus in the batch job: consultations requiring human input pause with progress saved.\n"
            "Slurm COMPLETED does not guarantee report completion; check the log. Do not run the same meeting in the foreground.",
        )
        say(f"提交命令：\n{plan.shell_command}", f"Submission command:\n{plan.shell_command}")
        executable = shutil.which("sbatch")
        if executable is None:
            say(
                "当前环境没有 sbatch；未提交任务。可在集群登录节点执行上面的命令，会议已保留。",
                "sbatch is unavailable; no job submitted. Run the command on a cluster login node; meeting kept.",
            )
            return "later"
        try:
            confirmed = input_fn("Submit now? [y/N]: " if language == "en" else "现在提交？[y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirmed = ""
        if confirmed not in {"y", "yes", "是"}:
            say("未提交任务，返回运行方式选择。", "No job submitted; returning to launch choices.")
            continue

        argv = [executable, *plan.argv[1:]]
        try:
            result = subprocess.run(
                argv, check=False, capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=30,
            )
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            # A timed-out sbatch may already have submitted a job. Never retry
            # or start the local runner automatically after an ambiguous result.
            say(
                "提交结果尚未确认；不会自动重试。请先用 squeue -u \"$USER\" 核查，避免重复任务；会议已保留。",
                'Submission status is unknown; no automatic retry. Check squeue -u "$USER" before resubmitting; meeting kept.',
            )
            return "later"
        except OSError as exc:
            say(f"未能执行 sbatch：{exc}；未自动重试，会议已保留。", f"Could not execute sbatch: {exc}; no automatic retry, meeting kept.")
            return "later"

        match = re.fullmatch(r"(\d+)(?:;[A-Za-z0-9_.-]+)?", result.stdout.strip())
        if result.returncode != 0 or match is None:
            detail = result.stderr.strip()[:1000]
            say(
                f"Slurm 提交未确认（退出码 {result.returncode}）：{detail}\n"
                "不会自动重试或启动前台流程；请检查 squeue 和上面的提交命令。会议已保留。",
                f"Slurm submission not confirmed (exit {result.returncode}): {detail}\n"
                "No automatic retry or foreground launch; check squeue and the command above. Meeting kept.",
            )
            return "later"
        job_id = match.group(1)
        receipt = {
            "job_id": job_id, "submitted_at": datetime.now(timezone.utc).isoformat(),
            "meeting_id": repo.meeting_id, "script_path": str(plan.script),
            "log_path": str(plan.log_pattern).replace("%j", job_id),
            "command": argv,
        }
        try:
            repo.docs.write_once(
                f"human_private/slurm/{plan.attempt_id}.submitted.json",
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            )
        except OSError:
            say("任务已提交，但无法写入提交回执；请记下以下任务编号，勿重复提交。", "Job submitted but receipt could not be saved; keep the job ID below and do not resubmit.")
        say(f"已提交 Slurm 任务：{job_id}；可以断开 SSH。", f"Slurm job submitted: {job_id}; you may disconnect SSH.")
        output("squeue -j " + job_id)
        output("tail -f " + shlex.quote(receipt["log_path"]))
        return "slurm"
