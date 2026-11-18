"""deploy/systemd/*: the unit files the server guide installs. systemd ignores a value that has a trailing
comment (this once silently disabled Restart=on-failure), so comments must sit on their own lines."""
from pathlib import Path

UNITS = Path(__file__).resolve().parents[2] / "deploy" / "systemd"


def _read(name: str) -> list[str]:
    return (UNITS / name).read_text(encoding="utf-8").splitlines()


def test_no_unit_has_a_comment_after_a_value():
    for path in sorted(UNITS.iterdir()):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                assert " #" not in line and "\t#" not in line, f"{path.name}: {line}"


def test_the_runner_restarts_after_a_failure_but_not_after_a_refusal_and_runs_as_the_service_user():
    text = "\n".join(_read("quant-runner.service"))
    for must in ("Restart=on-failure", "RestartPreventExitStatus=2", "User=quant", "--resume-if-exists",
                 "StartLimitIntervalSec=600"):
        assert must in text
    unit, service = text.split("[Service]")
    assert "StartLimitIntervalSec" in unit and "StartLimitIntervalSec" not in service      # it belongs in [Unit]


def test_the_dashboard_listens_on_localhost_only():
    text = "\n".join(_read("quant-dashboard.service"))
    assert "--host 127.0.0.1" in text and "0.0.0.0" not in text


def test_every_unit_points_at_scripts_that_exist():
    repo = UNITS.parents[1]
    for path in UNITS.glob("*.service"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart="):
                script = next(tok for tok in line.split() if tok.startswith("scripts/"))
                assert (repo / script).is_file(), f"{path.name}: {script}"
