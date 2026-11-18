"""scripts/backup_state.py: a consistent copy of the runner's state, newest N kept, failures are loud."""
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup_state.py"
spec = importlib.util.spec_from_file_location("backup_state", SCRIPT)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def make_state(path: Path) -> Path:
    path.mkdir(parents=True)
    for name, rows in (("checkpoints.db", [("cash", 670.0)]), ("sessions.db", [("2026-11-18", 1.0)])):
        con = sqlite3.connect(path / name)
        con.execute("create table t (k text, v real)")
        con.executemany("insert into t values (?, ?)", rows)
        con.commit()
        con.close()
    (path / "runner_status.json").write_text(json.dumps({"state": "WAITING"}), encoding="utf-8")
    return path


def test_databases_and_small_files_are_copied_and_readable(tmp_path):
    state = make_state(tmp_path / "state")
    dest = backup.backup_state(state, tmp_path / "bk", keep=14, now=datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc))
    assert dest.name == "state-20261118-030000"
    con = sqlite3.connect(dest / "checkpoints.db")
    assert con.execute("select k, v from t").fetchall() == [("cash", 670.0)]
    con.close()
    assert json.loads((dest / "runner_status.json").read_text(encoding="utf-8")) == {"state": "WAITING"}


def test_a_database_that_is_being_written_is_still_copied_consistently(tmp_path):
    state = make_state(tmp_path / "state")
    writer = sqlite3.connect(state / "checkpoints.db")
    writer.execute("begin")
    writer.execute("insert into t values ('uncommitted', 1.0)")        # an open transaction on the live database
    dest = backup.backup_state(state, tmp_path / "bk", keep=14)
    writer.rollback()
    writer.close()
    con = sqlite3.connect(dest / "checkpoints.db")
    assert con.execute("select k from t").fetchall() == [("cash",)]    # only committed data, and the file is not torn
    assert con.execute("pragma integrity_check").fetchone() == ("ok",)
    con.close()


def test_only_the_newest_backups_are_kept(tmp_path):
    state = make_state(tmp_path / "state")
    for day in (16, 17, 18, 19):
        backup.backup_state(state, tmp_path / "bk", keep=2, now=datetime(2026, 11, day, 3, 0, tzinfo=timezone.utc))
    assert sorted(p.name for p in (tmp_path / "bk").iterdir()) == ["state-20261118-030000", "state-20261119-030000"]


def test_a_missing_state_directory_fails_loudly(tmp_path, capsys):
    assert backup.main(["--state-dir", str(tmp_path / "nope"), "--backup-dir", str(tmp_path / "bk")]) == 2
    assert "backup failed" in capsys.readouterr().err
