import json

from project_ensemble import cli
from project_ensemble.domain import ReasoningEffort
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.runtime.run_controls import (
    effective_openalex_quota_policy, effective_reasoning_effort, effective_research_parallelism,
    record_run_control,
)
from project_ensemble.storage.meeting import MeetingRepository


def test_human_run_controls_are_append_only_and_do_not_rewrite_initialization(tmp_path):
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rule.md").write_text("rule", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "meeting", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs=governance,
        task_description="task",
    )
    manifest = repo.root / "identity_private/meeting_manifest.json"
    initial_bytes = manifest.read_bytes()
    record_run_control(repo, kind="model_concurrency", target="fake:m", value=4,
                       reason="人类允许并行模型调用")
    record_run_control(repo, kind="research_parallelism", target=None, value=8,
                       reason="人类允许独立问题并行")
    record_run_control(repo, kind="reasoning_effort", target="CHAIR", value="low",
                       reason="降低后续调用延迟")
    assert manifest.read_bytes() == initial_bytes
    assert cli._meeting_concurrency_limits(
        repo, type("Config", (), {"providers": {}})()
    )[("fake", "m")] == 4
    assert effective_research_parallelism(repo, None) == 8
    assert effective_reasoning_effort(repo, "CHAIR", ReasoningEffort.HIGH) == ReasoningEffort.LOW
    engine = MeetingEngine(repo=repo, adapters={}, notifier=object())
    assert engine._reasoning_effort_for("CHAIR") == ReasoningEffort.LOW
    records = sorted((repo.root / "human_private/runtime_controls").glob("control-*.json"))
    assert len(records) == 3
    assert [json.loads(path.read_text())["sequence"] for path in records] == [1, 2, 3]
    assert "MEETING_RUNTIME_CONTROL_CHANGED" in repo.events.path.read_text()


def test_openalex_quota_policy_can_override_an_existing_meeting_without_rewriting_manifest(tmp_path):
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rule.md").write_text("rule", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "meeting", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs=governance,
        task_description="task",
    )
    manifest = repo.root / "identity_private/meeting_manifest.json"
    original = manifest.read_bytes()
    assert effective_openalex_quota_policy(repo, "legacy_parallel") == "legacy_parallel"
    record_run_control(
        repo, kind="openalex_quota_policy", target=None, value="tavily",
        reason="人类在会议期间允许受影响的学术检索使用 Tavily",
    )
    assert effective_openalex_quota_policy(repo, "legacy_parallel") == "tavily"
    assert manifest.read_bytes() == original
    record_run_control(
        repo, kind="openalex_quota_policy", target=None, value="wait",
        reason="人类恢复等待 OpenAlex 重置",
    )
    assert effective_openalex_quota_policy(repo, "legacy_parallel") == "wait"


def test_runtime_control_does_not_require_human_reason(tmp_path):
    governance = tmp_path / "governance"
    governance.mkdir()
    (governance / "rule.md").write_text("rule", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "meeting", selected_models=[("fake", "m")],
        chair_model=("fake", "m"), governance_docs=governance,
    )
    record = record_run_control(
        repo, kind="research_parallelism", target=None, value=16,
    )
    assert record["reason"] is None
    assert record["authority"] == "HUMAN"
    assert effective_research_parallelism(repo, None) == 16
    assert repo.events.verify()
