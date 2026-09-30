import json

from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.providers.fake import ScriptedProviderAdapter
from project_ensemble.runtime.model_replacements import ModelReplacementService, current_runtime_for
from project_ensemble.storage.meeting import MeetingRepository


class CapturingNotifier:
    def send_escalation(self, **kwargs):
        return True


def test_replacement_is_audited_and_only_changes_future_runtime(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "old")],
        chair_model=("fake", "old"),
        governance_docs=gov,
    )
    representative_id = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text(encoding="utf-8")
    )[0]["representative_id"]

    assert current_runtime_for(repo, representative_id) == ("fake", "old")
    record = ModelReplacementService(repo).replace(
        participant_id=representative_id,
        provider_id="fake",
        model_id="new",
        reason="原模型在恢复阶段持续失败，改由备用模型接手",
    )

    assert record.participant_id == representative_id
    assert current_runtime_for(repo, representative_id) == ("fake", "new")
    original = json.loads(
        (repo.root / "identity_private/representative_registry.json").read_text(encoding="utf-8")
    )[0]["runtime"]
    assert (original["provider_id"], original["model_id"]) == ("fake", "old")
    assert (repo.root / record.record_path).is_file()
    assert (repo.root / "public/model_replacements" / f"{record.replacement_id}.json").is_file()

    engine = MeetingEngine(
        repo=repo,
        adapters={"fake": ScriptedProviderAdapter("fake", ["old", "new"], [])},
        notifier=CapturingNotifier(),
    )
    assert engine._runtime_for(representative_id) == ("fake", "new")
    assert "MODEL_RUNTIME_REPLACED" in repo.events.path.read_text(encoding="utf-8")
    assert repo.events.verify()


def test_replacement_requires_a_different_runtime(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "old")],
        chair_model=("fake", "old"),
        governance_docs=gov,
    )
    try:
        ModelReplacementService(repo).replace(
            participant_id="CHAIR",
            provider_id="fake",
            model_id="old",
            reason="same",
        )
    except ValueError as exc:
        assert "identical" in str(exc)
    else:
        raise AssertionError("same runtime replacement must be rejected")


def test_replacement_can_switch_all_participants_using_one_model(tmp_path):
    gov = tmp_path / "gov"
    gov.mkdir()
    (gov / "rule.md").write_text("rule", encoding="utf-8")
    repo = MeetingRepository.create(
        tmp_path / "ws",
        selected_models=[("fake", "old"), ("fake", "other")],
        chair_model=("fake", "old"),
        governance_docs=gov,
    )
    records = ModelReplacementService(repo).replace_all_using(
        from_provider_id="fake",
        from_model_id="old",
        provider_id="fake",
        model_id="new",
        reason="批量切换备用模型",
    )
    assert len(records) == 5  # Chair plus four personas for the old model.
    assert all(current_runtime_for(repo, record.participant_id) == ("fake", "new") for record in records)
