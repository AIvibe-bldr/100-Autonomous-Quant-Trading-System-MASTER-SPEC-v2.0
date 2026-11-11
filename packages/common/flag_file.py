"""Audited atomic flag-file write, shared by the operator kill switch and the LIVE gate.

Both are human-controlled files read by a running system, so they need the same two
properties: the audit record is appended (and fsynced) BEFORE the flag changes, so a
change can never exist without its record; and the flag is replaced atomically, so a
reader never sees a half-written file."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_with_audit(path: Path, entry: dict[str, Any]) -> None:
    log_path = path.with_name(path.name + ".log")
    path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")
        log.flush()
        os.fsync(log.fileno())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(tmp, path)
