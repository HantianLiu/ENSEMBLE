import json
from pathlib import Path
from types import SimpleNamespace

from project_ensemble.runtime.research_fallbacks import ResearchFallbacks


def test_next_failure_backup_is_consumed_once_without_changing_primary(tmp_path):
    events = []

    class Docs:
        def write_once(self, relative: Path, value: str):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.exists()
            path.write_text(value, encoding="utf-8")

    repo = SimpleNamespace(
        root=tmp_path, docs=Docs(), meeting_id="LR-TEST",
        events=SimpleNamespace(append=lambda *args, **kwargs: events.append(args[0])),
    )
    fallbacks = ResearchFallbacks(repo)
    choice = fallbacks.choose(
        request_id="PROVIDER-FAILURE-1", source=("deepseek", "flash"),
        target=("codex", "sol"), scope="NEXT_FAILURE_ONLY", reason="Human choice",
    )
    assert fallbacks.take_next_failure_target(("deepseek", "flash")) == ("codex", "sol")
    assert fallbacks.take_next_failure_target(("deepseek", "flash")) is None
    assert fallbacks.failure_target(("deepseek", "flash")) is None
    assert (tmp_path / "governance_private/research_fallbacks"
            / f"{choice['record_id']}.used.json").is_file()
    assert "RESEARCH_DESK_ONCE_FALLBACK_CONSUMED" in events
