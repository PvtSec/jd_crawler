"""Local persistent tracker file for applied / hidden jobs.

The JSON file under ./data is the source of truth for user decisions. It
survives database resets: on startup its entries are pushed back into the
database, and applied jobs that no longer exist in any feed are restored as
"closed" rows so the application history is never lost.

Writes are atomic (temp file + fsync + rename) and serialised by a lock.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TRACKER_PATH = Path(os.getenv("JOBHUNT_TRACKER", "/app/data/tracker.json"))
STATES = ("applied", "hidden")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Tracker:
    def __init__(self, path: Path = TRACKER_PATH):
        self.path = path
        self._lock = asyncio.Lock()
        self.data: dict[str, dict[str, dict[str, Any]]] = {"applied": {}, "hidden": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            backup = self.path.with_suffix(f".corrupt-{int(datetime.now().timestamp())}.json")
            log.error("tracker file unreadable (%s); moving it to %s and starting fresh", e, backup)
            try:
                self.path.rename(backup)
            except OSError:
                pass
            return
        for state in STATES:
            entries = raw.get(state, {})
            if isinstance(entries, dict):
                self.data[state] = {str(k): v for k, v in entries.items() if isinstance(v, dict)}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_comment": "Managed by JobHunt. Safe to back up; edit only while the backend is stopped.",
            "updated_at": _now(),
            **self.data,
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tracker-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def snapshot(job: Any) -> dict[str, Any]:
        return {
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "url": job.url,
            "apply_url": job.apply_url,
            "source": job.source,
            "dedup_key": job.dedup_key,
            "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        }

    def state_of(self, uid: str) -> tuple[str, dict[str, Any]] | None:
        for state in STATES:
            if uid in self.data[state]:
                return state, self.data[state][uid]
        return None

    def by_dedup_key(self) -> dict[str, tuple[str, dict[str, Any]]]:
        out = {}
        for state in STATES:
            for entry in self.data[state].values():
                if entry.get("dedup_key"):
                    out[entry["dedup_key"]] = (state, entry)
        return out

    async def set_state(self, job: Any, state: str | None, notes: str | None = None) -> dict[str, Any] | None:
        """Move a job to `state` ("applied", "hidden" or None to clear)."""
        async with self._lock:
            previous = None
            for s in STATES:
                previous = self.data[s].pop(job.uid, None) or previous
            entry = None
            if state:
                entry = {**self.snapshot(job), "at": _now()}
                if notes is not None:
                    entry["notes"] = notes
                elif previous and previous.get("notes"):
                    entry["notes"] = previous["notes"]
                self.data[state][job.uid] = entry
            await asyncio.to_thread(self._write)
            return entry

    async def set_notes(self, uid: str, notes: str) -> bool:
        async with self._lock:
            hit = self.state_of(uid)
            if not hit:
                return False
            hit[1]["notes"] = notes
            await asyncio.to_thread(self._write)
            return True

    def counts(self) -> dict[str, int]:
        return {s: len(self.data[s]) for s in STATES}


tracker = Tracker()
