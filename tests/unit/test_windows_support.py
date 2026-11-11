"""Windows has no system tz database: without the `tzdata` package the trading calendar cannot even be imported
(found by the Windows CI job; the operator's PC runs Windows). This guards the declaration on any OS."""
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_tzdata_is_a_dependency_on_windows_only():
    deps = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    tz = [d for d in deps if d.startswith("tzdata")]
    assert len(tz) == 1 and "win32" in tz[0], deps
