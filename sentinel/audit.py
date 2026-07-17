"""Append-only audit log (JSONL). Every dispatch, reclaim, escalation and transition
is recorded here; the significant ones are additionally mirrored as Jira comments
by their call sites (00-overview: 'Jira comment + external log').

The log lives on the same fixed `/data` volume as the agent workspaces and the
pause-state file, and it is written from the orchestrator's and agents' hot paths.
Left unbounded it would eventually fill that volume and make `record()` raise —
inside dispatch/escalation/transition code. So the file is size-rotated with a
bounded number of retained generations (`audit.jsonl`, `audit.jsonl.1`, …), the
same scheme as logging.handlers.RotatingFileHandler. Set `max_bytes=0` to disable
rotation and keep a single unbounded file (the historical behavior).
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("sentinel.audit")

DEFAULT_MAX_BYTES = 50_000_000   # 50 MB per generation
DEFAULT_BACKUP_COUNT = 5          # ...times (5 + 1) generations ≈ 300 MB cap


class AuditLog:
    def __init__(self, path: Path, max_bytes: int = DEFAULT_MAX_BYTES,
                 backup_count: int = DEFAULT_BACKUP_COUNT):
        self.path = path
        self.max_bytes = max(0, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, event: str, **fields) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "event": event, **fields}
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            if self._should_rotate(len(line.encode("utf-8"))):
                self._rotate()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
        log.info("audit: %s", line.rstrip("\n"))

    # -- querying ------------------------------------------------------------

    def _generations(self) -> list[Path]:
        """Retained files oldest-first: audit.jsonl.N … audit.jsonl.1, then the
        live file — so records come out in chronological order."""
        files = [self._backup(i) for i in range(self.backup_count, 0, -1)]
        files.append(self.path)
        return [f for f in files if f.exists()]

    def read_records(self, limit: int = 100, ticket: str | None = None,
                     event: str | None = None) -> list[dict]:
        """The newest `limit` records matching the filters, oldest-first.

        Reads across all retained generations under the write lock, so a
        concurrent rotation can't tear the view. Malformed lines (e.g. a write
        interrupted by a crash) are skipped, never raised — the query path must
        stay as unbreakable as the record path."""
        limit = max(1, int(limit))
        out: deque[dict] = deque(maxlen=limit)
        with self._lock:
            for f in self._generations():
                try:
                    lines = f.read_text(encoding="utf-8").splitlines()
                except OSError as e:
                    log.warning("could not read audit generation %s: %s", f, e)
                    continue
                for line in lines:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if ticket and rec.get("ticket") != ticket:
                        continue
                    if event and rec.get("event") != event:
                        continue
                    out.append(rec)
        return list(out)

    # -- rotation ------------------------------------------------------------

    def _should_rotate(self, incoming_bytes: int) -> bool:
        # Rotation needs a size limit and somewhere to rotate into.
        if self.max_bytes <= 0 or self.backup_count <= 0:
            return False
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return False
        # Rotate before a write would push an existing, non-empty file over the
        # limit (a single oversized line still gets its own fresh file).
        return size > 0 and size + incoming_bytes > self.max_bytes

    def _backup(self, i: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{i}")

    def _rotate(self) -> None:
        """audit.jsonl -> .1, .1 -> .2, …, dropping the generation beyond
        backup_count. Best-effort: a rotation failure is logged, not raised, so a
        transient FS hiccup never takes down the pipeline mid-dispatch."""
        try:
            oldest = self._backup(self.backup_count)
            if oldest.exists():
                oldest.unlink()
            for i in range(self.backup_count - 1, 0, -1):
                src = self._backup(i)
                if src.exists():
                    src.rename(self._backup(i + 1))
            if self.path.exists():
                self.path.rename(self._backup(1))
        except OSError as e:
            log.warning("audit log rotation failed (%s); continuing to append", e)
