from pathlib import Path
import tomllib

from project_ensemble import __version__


def test_release_has_canonical_and_compatibility_console_entry_points():
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == metadata["project"]["version"] == "0.7.3"
    assert metadata["project"]["scripts"] == {
        "ensemble": "project_ensemble.cli:main",
        "ensemble-v071": "project_ensemble.cli:main",
    }


def test_new_governance_is_packaged_without_replacing_old_archive():
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    data_files = metadata["tool"]["setuptools"]["data-files"]
    assert "share/project-ensemble-v071/governance/02_deliberation" in data_files
    assert (
        root / "docs/governance/02_deliberation/literature_writing_v071.md"
    ).is_file()
    assert all(not key.startswith("share/project-ensemble-v07/") for key in data_files)
