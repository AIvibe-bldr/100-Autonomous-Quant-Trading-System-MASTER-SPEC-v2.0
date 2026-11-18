"""Back up a runner's state directory safely, while the runner may be running.

  python3 scripts/backup_state.py --state-dir /var/lib/quant/paper-1 --backup-dir /var/backups/quant [--keep 14]

Every SQLite database (checkpoints.db is the account's only record) is copied with SQLite's own online-backup
API, which gives a consistent copy even if the runner writes at that moment — a plain file copy of a live
database can be torn. The other small files (status, alert history) are copied as they are. Each run makes
one dated folder under --backup-dir and keeps the newest --keep of them.

A backup on the same disk does not survive losing the server: also copy the newest folder somewhere else
(a provider snapshot, or `scp` to another machine) — see docs/operations.md §11."""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def backup_state(state_dir: Path, backup_dir: Path, keep: int, now: datetime | None = None) -> Path:
    if not state_dir.is_dir():
        raise FileNotFoundError(f"state directory not found: {state_dir}")
    now = now or datetime.now(timezone.utc)
    dest = backup_dir / f"state-{now:%Y%m%d-%H%M%S}"
    dest.mkdir(parents=True, exist_ok=False)
    for src in sorted(state_dir.iterdir()):
        if not src.is_file() or src.name.endswith(("-wal", "-shm", "-journal")):
            continue
        if src.suffix == ".db":
            source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
            target = sqlite3.connect(dest / src.name)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
        else:
            shutil.copy2(src, dest / src.name)
    folders = sorted(p for p in backup_dir.iterdir() if p.is_dir() and p.name.startswith("state-"))
    for old in folders[:-keep] if keep > 0 else []:
        shutil.rmtree(old)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--keep", type=int, default=14, help="how many dated backups to keep")
    args = parser.parse_args(argv)
    try:
        dest = backup_state(Path(args.state_dir), Path(args.backup_dir), args.keep)
    except (OSError, sqlite3.Error) as e:
        print(f"backup failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(f"backed up to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
