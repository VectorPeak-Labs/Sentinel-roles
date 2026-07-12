"""Append-only audit log (JSONL). Every dispatch, reclaim, escalation and transition
is recorded here; the significant ones are additionally mirrored as Jira comments
by their call sites (00-overview: 'Jira comment + external log')."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("sentinel.audit")


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, event: str, **fields) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "event": event, **fields}
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        log.info("audit: %s", line)
