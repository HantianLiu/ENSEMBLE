from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "publish_github.template.sh"
URL = "https://github.com/HantianLiu/ENSEMBLE.git"
GIT = shutil.which("git")


def git(repo, *args):
    return subprocess.run([GIT, *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def publish_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / ".gitignore").write_text("/publish_github.local.sh\n/ensemble.toml\n/LR-*/\n")
    (repo / "pyproject.toml").write_text('version = "0.7.5"\n')
    (repo / "src").mkdir()
    (repo / "src" / "example.py").write_text("VERSION = 'baseline'\n")
    (repo / "scripts").mkdir()
    shutil.copyfile(TEMPLATE, repo / "scripts" / TEMPLATE.name)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "push", str(remote), "HEAD:refs/heads/main")
    git(repo, "remote", "add", "origin", URL)
    shutil.copyfile(TEMPLATE, repo / "publish_github.local.sh")
    (repo / "src" / "example.py").write_text("VERSION = '0.7.5'\n")
    (repo / "ensemble.toml").write_text("secret = 'PRIVATE_CONFIGURATION'\n")
    (repo / "LR-PRIVATE").mkdir()
    (repo / "LR-PRIVATE" / "private.json").write_text("{}")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    log = tmp_path / "calls"
    shim = shim_dir / "git"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        f"git = {GIT!r}\nremote = {str(remote)!r}\nurl = {URL!r}\nlog = {str(log)!r}\n"
        "args = sys.argv[1:]\n"
        "if 'fetch' in args or 'push' in args:\n"
        "    helper = os.environ['GIT_ASKPASS']\n"
        "    for prompt, expected in [(\"Username for 'https://github.com':\", 'HantianLiu'), "
        "(\"Password for 'https://HantianLiu@github.com':\", 'TEST_ONLY_FAKE_TOKEN')]:\n"
        "        r = subprocess.run([helper, prompt], capture_output=True, text=True)\n"
        "        assert r.returncode == 0 and r.stdout.strip() == expected\n"
        "    r = subprocess.run([helper, \"Password for 'https://evil.example':\"], capture_output=True)\n"
        "    assert r.returncode != 0 and not r.stdout\n"
        "    with open(log, 'a') as f: f.write('authentication_checked\\n')\n"
        "if 'commit' in args:\n"
        "    assert 'ENSEMBLE_GITHUB_PASSWORD' not in os.environ\n"
        "args = [remote if x == url else x for x in args]\n"
        "sys.exit(subprocess.call([git, *args]))\n"
    )
    shim.chmod(0o700)
    env = os.environ.copy()
    env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
    env["ENSEMBLE_GITHUB_TOKEN"] = "TEST_ONLY_FAKE_TOKEN"
    env.pop("ENSEMBLE_GITHUB_PASSWORD", None)
    return repo, remote, env, log


def run_script(fixture, *args):
    repo, _, env, _ = fixture
    return subprocess.run(
        ["bash", str(repo / "publish_github.local.sh"), *args],
        cwd=repo, env=env, text=True, capture_output=True,
    )


def test_template_has_blank_credentials_and_valid_bash():
    text = TEMPLATE.read_text()
    assert 'GITHUB_USERNAME="HantianLiu"' in text
    assert 'PASSWORD=""  # GitHub personal access token;' in text
    subprocess.run(["bash", "-n", str(TEMPLATE)], check=True)


def test_publish_offline_and_retry_without_leaking_credentials(publish_repo):
    repo, remote, _, log = publish_repo
    result = run_script(publish_repo)
    assert result.returncode == 0, result.stderr
    head = git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "v0.7.5^{commit}") == head
    assert git(remote, "rev-parse", "refs/heads/main") == head
    assert git(remote, "rev-parse", "refs/tags/v0.7.5^{commit}") == head
    files = git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert "publish_github.local.sh" not in files
    assert "ensemble.toml" not in files
    assert not any(x.startswith("LR-") for x in files)
    assert "TEST_ONLY_FAKE_TOKEN" not in git(repo, "show", "HEAD:scripts/publish_github.template.sh")
    assert log.read_text().splitlines() == ["authentication_checked"] * 2
    assert run_script(publish_repo).returncode == 0
    assert git(repo, "rev-parse", "HEAD") == head


def test_dry_run_does_not_change_git_or_contact_remote(publish_repo):
    repo, _, env, log = publish_repo
    env.pop("ENSEMBLE_GITHUB_TOKEN")
    head = git(repo, "rev-parse", "HEAD")
    assert run_script(publish_repo, "--dry-run").returncode == 0
    assert git(repo, "rev-parse", "HEAD") == head
    assert not git(repo, "diff", "--cached", "--name-only")
    assert not git(repo, "tag")
    assert not log.exists()


@pytest.mark.parametrize("unsafe", ["tracked_secret", "public_secret", "staged_changes", "wrong_branch"])
def test_rejects_unsafe_states_before_authentication(publish_repo, unsafe):
    repo, _, _, log = publish_repo
    if unsafe == "tracked_secret":
        git(repo, "add", "-f", "publish_github.local.sh")
    elif unsafe == "public_secret":
        path = repo / "scripts" / TEMPLATE.name
        path.write_text(path.read_text().replace('PASSWORD=""', 'PASSWORD="DO_NOT_PUBLISH"'))
    elif unsafe == "staged_changes":
        git(repo, "add", "src")
    else:
        git(repo, "checkout", "-b", "another")
    head = git(repo, "rev-parse", "HEAD")
    result = run_script(publish_repo)
    assert result.returncode != 0
    assert git(repo, "rev-parse", "HEAD") == head
    assert not git(repo, "tag")
    assert not log.exists()


def test_remote_ahead_does_not_stage_or_commit(publish_repo, tmp_path):
    repo, remote, _, _ = publish_repo
    other = tmp_path / "other"
    git(tmp_path, "clone", "-b", "main", str(remote), str(other))
    git(other, "config", "user.name", "Other")
    git(other, "config", "user.email", "other@example.invalid")
    (other / "README.md").write_text("remote change\n")
    git(other, "add", ".")
    git(other, "commit", "-m", "remote ahead")
    git(other, "push", "origin", "main")
    head = git(repo, "rev-parse", "HEAD")
    assert run_script(publish_repo).returncode != 0
    assert git(repo, "rev-parse", "HEAD") == head
    assert not git(repo, "diff", "--cached", "--name-only")
    assert not git(repo, "tag")

