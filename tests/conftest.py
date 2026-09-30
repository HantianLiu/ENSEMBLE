import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def isolated_user_settings(tmp_path, monkeypatch):
    """Tests must never register synthetic meetings in the Human's real index."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "settings"))
