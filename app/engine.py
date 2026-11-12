"""Background engine.

* board worker   - continuously polls due company boards from the registry (adaptive tiers)
* aggregator loop - polls public job aggregators on a fixed cycle (fair-use throttled)
* maintenance    - cross-source duplicate resolution (best apply link wins), housekeeping
* validation     - liveness checks of aggregator postings
* config watcher - hot reload: re-score, re-seed registry, refetch
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from . import registry
from .config import AppConfig, config_store
from .db import AppState, Company, FetchRun, Job, Session, engine as db_engine
from .http import FetchError, Http
from .matching import InvalidJob, Matcher, NormalizedJob, enrich, normalize
from .sources import ATS_SOURCES, SOURCES, BoardResult, FetchContext, VerifyResult
from .sources.base import html_to_text
from .tracker import tracker

log = logging.getLogger(__name__)

ADVISORY_LOCK_ID = 7_301_984_221
# "api": serve the API only (fetching runs in the worker container); "worker": run the
# scheduler only; "all": both in one process (the original single-container layout).
ROLE = os.getenv("JOBHUNT_ROLE", "all")
DATA_DIR = Path(os.getenv("JOBHUNT_TRACKER", "/app/data/tracker.json")).parent
STATUS_FILE = DATA_DIR / "engine_status.json"     # worker -> API: live scheduler state
REFRESH_FILE = DATA_DIR / "refresh.request"        # API -> worker: "fetch now" button
STATUS_FIELDS = ("fetching", "validating", "boards_in_flight", "boards_done", "last_fetch_started",
                 "last_fetch_finished", "next_fetch_at", "last_validation_at", "last_maintenance_at",
                 "last_error", "resolver_stats")
UPSERT_CHUNK = 300

CONTENT_COLS = (
    "source", "board", "authoritative", "external_id", "company", "title", "url", "apply_url", "location",
    "remote", "description", "department", "employment_type", "posted_at", "salary_min", "salary_max",
    "salary_currency", "content_hash", "dedup_key", "skills", "seniority", "years_required", "matched",
    "match_score", "match_notes", "category", "is_tech", "languages", "text_lang", "lang_ok", "trust_flags",
    "trusted", "link_rank",
)
ENRICH_COLS = ("skills", "seniority", "years_required", "matched", "match_score", "match_notes", "category",
               "is_tech", "languages", "text_lang", "lang_ok", "trust_flags", "trusted")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Applied / hidden decisions carry over to the cross-board copies of the same posting that
# deduplication linked (not to every role sharing a dedup key: "Engineer (Vision)" and
# "Engineer (Compression)" share one), so the employer's direct posting shows up in Applied.
COPY_STATES_SQL = """
    UPDATE jobs j SET user_state = s.user_state, state_changed_at = s.state_changed_at
      FROM jobs s
     WHERE s.user_state <> 'new' AND j.user_state = 'new' AND j.id <> s.id
       AND (j.duplicate_of = s.id OR s.duplicate_of = j.id
            OR (j.duplicate_of IS NOT NULL AND j.duplicate_of = s.duplicate_of))
"""


class Engine:
    def __init__(self) -> None:
        self.cfg: AppConfig = config_store.current
        self.matcher = Matcher(self.cfg)
        self.http = Http(self.cfg.fetch)
        self._tasks: list[asyncio.Task] = []
        self._agg_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._wake_agg = asyncio.Event()
        self._wake_boards = asyncio.Event()
        self._agg_last_ok: dict[str, float] = {}
        self._leader_conn = None
        self.is_leader = False
        self.fetching = False
        self.validating = False
        self.boards_in_flight = 0
        self.boards_done = 0
        self.last_fetch_started: datetime | None = None
        self.last_fetch_finished: datetime | None = None
        self.next_fetch_at: datetime | None = None
        self.last_validation_at: datetime | None = None
        self.last_maintenance_at: datetime | None = None
        self.last_error: str | None = None
        self.resolver_stats: dict[str, Any] = {}
        # Storage budget (see enforce_storage); persisted in app_state.
        self.job_cap: int | None = None          # max jobs kept once the database got full
        self.cap_size_mb: float | None = None    # database size when the cap was last lowered
        self.evict_before: datetime | None = None  # postings older than this are not (re)stored
        self.last_eviction: dict[str, Any] = {}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if ROLE == "api":
            self._tasks = [asyncio.create_task(self._api_config_loop(), name="config-loop")]
            return
        await self.reconcile_tracker()
        self.is_leader = await self._acquire_leadership()
        if not self.is_leader:
            log.warning("another backend instance holds the scheduler lock; this instance only serves the API")
            return
        await registry.seed(self.cfg)
        async with Session() as s:  # leases from a previous (crashed/restarted) run are void
            await s.execute(update(Company).where(Company.leased.is_(True)).values(leased=False, next_fetch_at=utcnow()))
            await s.commit()
        self._tasks = [
            asyncio.create_task(self._board_loop(), name="board-loop"),
            asyncio.create_task(self._aggregator_loop(), name="aggregator-loop"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance-loop"),
            asyncio.create_task(self._storage_loop(), name="storage-loop"),
            asyncio.create_task(self._validate_loop(), name="validate-loop"),
            asyncio.create_task(self._config_loop(), name="config-loop"),
            asyncio.create_task(self._resolver_loop(), name="resolver-loop"),
        ]
        if ROLE == "worker":
            self._tasks.append(asyncio.create_task(self._publish_loop(), name="publish-loop"))

    # ------------------------------------------------------------ worker <-> API (split mode)

    async def _publish_loop(self) -> None:
        """Worker: share live state with the API process and act on its refresh requests."""
        while True:
            try:
                if REFRESH_FILE.exists():
                    REFRESH_FILE.unlink(missing_ok=True)
                    n = await self.trigger_fetch("hot")
                    log.info("refresh requested from the UI: %d boards queued", n)
                snap = {k: getattr(self, k) for k in STATUS_FIELDS}
                snap["fetching"] = self.fetching or self._agg_lock.locked()
                snap["heartbeat"] = utcnow()
                tmp = STATUS_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(snap, default=str))
                tmp.replace(STATUS_FILE)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("publishing worker status failed")
            await asyncio.sleep(3)

    async def _api_config_loop(self) -> None:
        """API process: follow config.yaml edits (the worker re-scores; we only re-read)."""
        while True:
            await asyncio.sleep(5)
            try:
                if await asyncio.to_thread(config_store.reload):
                    self.cfg = config_store.current
                    self.matcher = Matcher(self.cfg)
            except Exception:
                log.exception("reloading config failed")

    @staticmethod
    def worker_state() -> dict[str, Any] | None:
        """API process: the worker's last published state, or None if it isn't running."""
        try:
            snap = json.loads(STATUS_FILE.read_text())
            beat = datetime.fromisoformat(snap["heartbeat"])
        except (OSError, ValueError, KeyError):
            return None
        return snap if utcnow() - beat < timedelta(seconds=30) else None

    def request_refresh(self) -> None:
        REFRESH_FILE.touch()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.http.aclose()
        if self._leader_conn is not None:
            await self._leader_conn.close()

    async def _acquire_leadership(self) -> bool:
        conn = await db_engine.connect()
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_ID})).scalar()
        await conn.commit()
        if got:
            self._leader_conn = conn
        else:
            await conn.close()
        return bool(got)

    async def trigger_fetch(self, scope: str = "hot") -> int:
        """Make boards due now. scope: hot (pinned / with matches / with tech jobs) or all."""
        async with Session() as s:
            q = update(Company).where(Company.enabled.is_(True))
            if scope != "all":
                q = q.where(or_(Company.pinned.is_(True), Company.matched_count > 0, Company.tech_count > 0))
            n = (await s.execute(q.values(next_fetch_at=utcnow()))).rowcount
            await s.commit()
        self._agg_last_ok.clear()
        self._wake_agg.set()
        self._wake_boards.set()
        return n

    # ------------------------------------------------------------ loops

    async def _board_loop(self) -> None:
        """Continuous worker pool: keeps up to N boards in flight, claiming due
        boards as slots free up (no head-of-line blocking on large feeds)."""
        running: set[asyncio.Task] = set()
        while True:
            slots = max(4, self.cfg.fetch.max_concurrency * 2) - len(running)
            claimed: list[Company] = []
            if slots > 0:
                try:
                    claimed = await self._claim_due(slots)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("claiming boards failed")
                    self.last_error = f"claiming boards failed: {e}"
            for c in claimed:
                t = asyncio.create_task(self._fetch_company(c))
                running.add(t)
                t.add_done_callback(running.discard)
            self.fetching = bool(running)
            if running:
                await asyncio.wait(running, timeout=10, return_when=asyncio.FIRST_COMPLETED)
            else:
                try:
                    await asyncio.wait_for(self._wake_boards.wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass
                self._wake_boards.clear()

    async def _claim_due(self, n: int) -> list[Company]:
        async with Session() as s:
            # Lease: push next_fetch_at forward so a crash cannot cause a hot loop.
            ids = (await s.execute(text("""
                UPDATE companies SET next_fetch_at = now() + interval '20 minutes', leased = true
                 WHERE id IN (SELECT id FROM companies WHERE enabled AND NOT leased AND next_fetch_at <= now()
                              ORDER BY pinned DESC, next_fetch_at LIMIT :n FOR UPDATE SKIP LOCKED)
             RETURNING id
            """), {"n": n})).scalars().all()
            await s.commit()
            if not ids:
                return []
            return list((await s.execute(select(Company).where(Company.id.in_(ids)))).scalars().all())

    async def _fetch_company(self, c: Company) -> None:
        src = SOURCES.get(c.ats)
        cfg, matcher = self.cfg, self.matcher
        if src is None:
            return
        ctx = FetchContext(cfg=cfg, http=self.http, title_ok=matcher.title_ok)
        self.boards_in_flight += 1
        started = utcnow()
        try:
            result = await src.fetch_board(ctx, c.token, c.name)
            status_code = None
            if not result.ok:
                m = re.search(r"HTTP (\d{3})", result.error)
                status_code = int(m.group(1)) if m else None
            await self._ingest_safe(result, matcher, cfg, started)
            counts = await self._board_counts(result.board) if result.ok else {}
            if result.ok and cfg.storage.tech_only:  # non-tech jobs aren't stored: count the listing
                counts["jobs"] = max(counts.get("jobs", 0), len(result.jobs))
            upd = registry.next_fetch(c, cfg, ok=result.ok, status_code=status_code,
                                      jobs=counts.get("jobs", 0), tech=counts.get("tech", 0), matched=counts.get("matched", 0),
                                      error=result.error)
            if not result.ok:
                upd["last_error"] = result.error[:500]
            if result.unchanged:  # keep counts from the last real parse
                for k in ("jobs_count", "tech_count", "matched_count"):
                    upd.pop(k, None)
            async with Session() as s:
                await s.execute(update(Company).where(Company.id == c.id).values(**upd, leased=False))
                await s.commit()
            self.last_fetch_finished = utcnow()
            self.last_fetch_started = self.last_fetch_started or started
        except asyncio.CancelledError:
            raise
        except Exception as e:  # release the lease and back off; never kill the pool
            log.exception("board %s:%s failed", c.ats, c.token)
            async with Session() as s:
                await s.execute(update(Company).where(Company.id == c.id).values(
                    leased=False, last_error=f"internal error: {e}"[:500],
                    next_fetch_at=utcnow() + timedelta(hours=1)))
                await s.commit()
        finally:
            self.boards_in_flight -= 1
            self.boards_done += 1

    async def _board_counts(self, board: str) -> dict[str, int]:
        async with Session() as s:
            row = (await s.execute(text("""
                SELECT count(*) AS jobs, count(*) FILTER (WHERE is_tech) AS tech,
                       count(*) FILTER (WHERE matched AND trusted AND lang_ok) AS matched
                  FROM jobs WHERE board = :b AND status = 'active'
            """), {"b": board})).mappings().one()
        return dict(row)

    async def _aggregator_loop(self) -> None:
        while True:
            try:
                await self.run_aggregators()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("aggregator cycle failed")
                self.last_error = f"aggregator cycle failed: {e}"
            interval = self.cfg.fetch.interval_minutes * 60
            self.next_fetch_at = utcnow() + timedelta(seconds=interval)
            try:
                await asyncio.wait_for(self._wake_agg.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            self._wake_agg.clear()

    def _company_files_mtime(self) -> tuple:
        from pathlib import Path

        out = []
        for f in self.cfg.sources.company_files:
            try:
                out.append((f, Path(f).stat().st_mtime))
            except OSError:
                out.append((f, None))
        return tuple(out)

    async def _maintenance_loop(self) -> None:
        await asyncio.sleep(120)
        while True:
            try:
                await self._mark_duplicates()
                await self._housekeeping()
                self.last_maintenance_at = utcnow()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("maintenance failed")
                self.last_error = f"maintenance failed: {e}"
            await asyncio.sleep(600)

    async def _resolver_loop(self) -> None:
        """Find employers' own boards for aggregator-only listings (direct apply links)."""
        from . import resolver

        await asyncio.sleep(60)
        while True:
            try:
                res = await resolver.resolve_batch(self)
                self.resolver_stats = {**res, "at": utcnow()}
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("resolver cycle failed")
                self.last_error = f"resolver failed: {e}"
                res = {"companies": 0}
            await asyncio.sleep(30 if res.get("companies") else 300)

    async def _validate_loop(self) -> None:
        await asyncio.sleep(180)
        while True:
            try:
                await self.run_validation()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("validation cycle failed")
                self.last_error = f"validation failed: {e}"
            await asyncio.sleep(self.cfg.validation.interval_minutes * 60)

    async def _config_loop(self) -> None:
        files, tick = self._company_files_mtime(), 0
        while True:
            await asyncio.sleep(5)
            tick += 1
            if tick % 12 == 0:  # once a minute: did discovery write new companies?
                now_files = self._company_files_mtime()
                if now_files != files:
                    files = now_files
                    try:
                        await registry.seed(self.cfg)
                        self._wake_boards.set()
                    except Exception as e:
                        log.exception("re-seeding registry failed")
                        self.last_error = f"re-seeding registry failed: {e}"
            if await asyncio.to_thread(config_store.reload):
                try:
                    await self.apply_config()
                except Exception as e:
                    log.exception("applying config failed")
                    self.last_error = f"applying config failed: {e}"

    async def apply_config(self) -> None:
        old = self.cfg
        self.cfg = config_store.current
        self.matcher = Matcher(self.cfg)
        if self.cfg.fetch != old.fetch:
            await self.http.aclose()
            self.http = Http(self.cfg.fetch)
        else:
            self.http.forget_cache()  # matching changed: feeds must be re-parsed
        log.info("config changed: re-seeding registry, re-scoring jobs, refetching")
        await registry.seed(self.cfg)
        await self.rescore_all()
        await self.trigger_fetch("hot")

    # ------------------------------------------------------------ aggregators

    async def run_aggregators(self) -> dict[str, Any]:
        if self._agg_lock.locked():
            return {"started": False}
        async with self._agg_lock:
            cfg, matcher, http = self.cfg, self.matcher, self.http
            ctx = FetchContext(cfg=cfg, http=http, title_ok=matcher.title_ok)
            work = []
            for src in SOURCES.values():
                if src.name in ATS_SOURCES:
                    continue
                gap = getattr(src, "min_interval_minutes", 0) * 60
                for board in src.boards(cfg):
                    key = src.board_key(board)
                    if gap and time.monotonic() - self._agg_last_ok.get(key, -1e12) < gap:
                        continue
                    work.append((src, board))

            async def one(src, board):
                started = utcnow()
                result = await src.fetch_board(ctx, board)
                await self._ingest_safe(result, matcher, cfg, started)
                if result.ok:
                    self._agg_last_ok[result.board] = time.monotonic()
                if cfg.sources.auto_register_from_aggregators and result.jobs:
                    links = [(j.company, j.apply_url or j.url) for j in result.jobs]
                    if await registry.register_from_links(links):
                        self._wake_boards.set()

            await asyncio.gather(*(one(s, b) for s, b in work))
            return {"started": True, "boards": len(work)}

    # ------------------------------------------------------------ ingestion

    async def _ingest_safe(self, result: BoardResult, matcher: Matcher, cfg: AppConfig, started: datetime) -> dict[str, int]:
        try:
            return await self._ingest(result, matcher, cfg, started)
        except Exception as e:
            log.exception("ingest failed for %s", result.board)
            self.http.forget_cache()
            result.ok, result.error = False, f"ingest failed: {e}"[:500]
            await self._record_run(result, started, {})
            return {}

    async def _ingest(self, result: BoardResult, matcher: Matcher, cfg: AppConfig, started: datetime) -> dict[str, int]:
        stats = {"fetched": len(result.jobs), "matched": 0, "new": 0, "updated": 0, "closed": 0, "rejected": 0}
        now = utcnow()
        if not result.ok and not result.jobs:
            await self._record_run(result, started, stats)
            return stats

        if result.unchanged:
            async with self._write_lock, Session() as s:
                await s.execute(update(Job).where(Job.board == result.board, Job.status == "active")
                                .values(last_seen_at=now, miss_count=0))
                await s.commit()
            await self._record_run(result, started, stats)
            return stats

        normalized: list[NormalizedJob] = []
        for raw in result.jobs:
            try:
                normalized.append(normalize(raw, source=result.source, board=result.board,
                                            authoritative=result.authoritative,
                                            max_desc=cfg.fetch.max_description_chars))
            except InvalidJob:
                stats["rejected"] += 1
        by_uid = {n.uid: n for n in normalized}

        existing: dict[str, str] = {}
        async with Session() as s:
            uids = list(by_uid)
            for i in range(0, len(uids), 5000):
                rows = await s.execute(select(Job.uid, Job.content_hash).where(Job.uid.in_(uids[i:i + 5000])))
                existing.update(rows.tuples().all())

        touch = [u for u, n in by_uid.items() if existing.get(u) == n.content_hash]
        changed = [n for u, n in by_uid.items() if existing.get(u) != n.content_hash]
        # CPU-heavy parts (skills for tech jobs) run off the event loop.
        changed = await asyncio.to_thread(lambda: [enrich(n, matcher) for n in changed])
        if cfg.storage.tech_only:
            keep = [n for n in changed if n.is_tech]
        else:
            keep = [n for n in changed if n.matched or n.is_tech or n.uid in existing or cfg.match.store_non_matching]
        if self.evict_before is not None:  # don't re-add postings the size budget already evicted
            cutoff = self.evict_before
            keep = [n for n in keep if n.uid in existing or (n.posted_at or now) >= cutoff]
        stats["matched"] = sum(1 for n in changed if n.matched)

        async with self._write_lock, Session() as s:
            await self._write_board(s, result, cfg, keep, touch, stats, now)
        await self._record_run(result, started, stats)
        return stats

    async def _write_board(self, s, result: BoardResult, cfg: AppConfig, keep: list[NormalizedJob],
                           touch: list[str], stats: dict[str, int], now: datetime) -> None:
        tracker.reload_if_changed()
        state_by_uid = {uid: st for st in ("applied", "hidden") for uid in tracker.data[st]}
        for i in range(0, len(keep), UPSERT_CHUNK):
            rows = []
            for n in keep[i:i + UPSERT_CHUNK]:
                row = {c: getattr(n, c) for c in CONTENT_COLS}
                row.update(uid=n.uid, last_seen_at=now, first_seen_at=now, miss_count=0, status="active",
                           user_state=state_by_uid.get(n.uid) or "new")
                rows.append(row)
            stmt = insert(Job).values(rows)
            ex = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=[Job.uid],
                set_={
                    **{c: getattr(ex, c) for c in CONTENT_COLS},
                    "last_seen_at": ex.last_seen_at,
                    "miss_count": 0,
                    "status": "active" if result.authoritative else Job.status,
                    "closed_at": None if result.authoritative else Job.closed_at,
                },
            ).returning(text("(xmax = 0) AS inserted"))
            for (inserted,) in (await s.execute(stmt)).all():
                stats["new" if inserted else "updated"] += 1

        if touch:
            values: dict[str, Any] = {"last_seen_at": now, "miss_count": 0}
            if result.authoritative:
                values.update(status="active", closed_at=None)
            for i in range(0, len(touch), 5000):
                await s.execute(update(Job).where(Job.uid.in_(touch[i:i + 5000])).values(**values))

        if result.authoritative and result.ok and result.present_ids is not None:
            stats["closed"] = await self._mark_missing(s, result, cfg)
        await s.commit()

    async def _mark_missing(self, s, result: BoardResult, cfg: AppConfig) -> int:
        """Jobs absent from a complete board listing get a miss; after N misses they are closed."""
        active = (await s.execute(
            select(func.count()).select_from(Job).where(Job.board == result.board, Job.status == "active")
        )).scalar_one()
        if not result.present_ids and active > 5:
            log.warning("%s returned an empty listing but %d jobs are active; skipping close-out", result.board, active)
            return 0
        rows = await s.execute(text("""
            UPDATE jobs
               SET miss_count = miss_count + 1,
                   status = CASE WHEN miss_count + 1 >= :n THEN 'closed' ELSE status END,
                   closed_at = CASE WHEN miss_count + 1 >= :n THEN now() ELSE closed_at END,
                   check_result = CASE WHEN miss_count + 1 >= :n THEN 'removed from company board' ELSE check_result END
             WHERE board = :b AND status = 'active' AND NOT (external_id = ANY(:ids))
         RETURNING status
        """), {"n": cfg.validation.closed_after_misses, "b": result.board, "ids": list(result.present_ids)})
        return sum(1 for (st,) in rows.all() if st == "closed")

    async def _record_run(self, result: BoardResult, started: datetime, stats: dict[str, int]) -> None:
        # With tens of thousands of boards, only aggregators and failures are logged per run.
        if result.source in ATS_SOURCES and result.ok:
            return
        async with Session() as s:
            s.add(FetchRun(
                board=result.board, source=result.source, started_at=started, finished_at=utcnow(),
                ok=result.ok, unchanged=result.unchanged, error=result.error[:2000],
                **{k: stats.get(k, 0) for k in ("fetched", "matched", "new", "updated", "closed", "rejected")},
            ))
            await s.commit()

    # ------------------------------------------------------------ maintenance

    async def _mark_duplicates(self) -> None:
        """The same role is often listed on the company's ATS and on aggregators.
        Keep the copy with the best apply link (employer ATS > aggregator linking to
        the ATS > employer site > aggregator page) and flag the others. Copies on the
        same board (e.g. one role in several cities) are left alone."""
        async with self._write_lock, Session() as s:
            await s.execute(text("""
                WITH ranked AS (
                    SELECT id, board,
                           first_value(id)    OVER w AS keep_id,
                           first_value(board) OVER w AS keep_board
                      FROM jobs
                     WHERE is_tech AND status = 'active'
                    WINDOW w AS (PARTITION BY dedup_key ORDER BY link_rank, trusted DESC, posted_at DESC NULLS LAST, id)
                ), target AS (
                    SELECT id, CASE WHEN keep_id <> id AND keep_board <> board THEN keep_id END AS dup FROM ranked
                )
                UPDATE jobs j SET duplicate_of = t.dup
                  FROM target t
                 WHERE j.id = t.id AND j.duplicate_of IS DISTINCT FROM t.dup
            """))
            await s.execute(text("UPDATE jobs SET duplicate_of = NULL WHERE status <> 'active' AND duplicate_of IS NOT NULL"))
            # Carry applied / hidden to the copies just linked above.
            await s.execute(text(COPY_STATES_SQL))
            await s.commit()

    async def _housekeeping(self) -> None:
        v = self.cfg.validation
        cutoff = utcnow() - timedelta(days=v.purge_closed_after_days)
        async with self._write_lock, Session() as s:
            await s.execute(delete(Job).where(Job.status == "closed", Job.closed_at < cutoff, Job.user_state == "new"))
            await s.execute(delete(FetchRun).where(FetchRun.started_at < utcnow() - timedelta(days=7)))
            if self.cfg.storage.tech_only:  # e.g. stored before tech_only was enabled
                await s.execute(delete(Job).where(Job.is_tech.is_(False), Job.user_state == "new"))
            await s.commit()

    # ------------------------------------------------------------ storage budget

    async def _storage_loop(self) -> None:
        await self._load_storage_state()
        while True:
            try:
                await self.enforce_storage()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # e.g. the database refuses writes when full: retry next round
                log.exception("storage check failed")
                self.last_error = f"storage check failed: {e}"
            await asyncio.sleep(60)

    async def _load_storage_state(self) -> None:
        async with Session() as s:
            st = dict((await s.execute(select(AppState.key, AppState.value))).tuples().all())
        self.job_cap = int(st["job_cap"]) if st.get("job_cap") else None
        self.cap_size_mb = float(st["cap_size_mb"]) if st.get("cap_size_mb") else None
        self.evict_before = datetime.fromisoformat(st["evict_before"]) if st.get("evict_before") else None

    async def _save_storage_state(self) -> None:
        values = {"job_cap": str(self.job_cap or ""), "cap_size_mb": str(self.cap_size_mb or ""),
                  "evict_before": self.evict_before.isoformat() if self.evict_before else ""}
        async with Session() as s:
            stmt = insert(AppState).values([{"key": k, "value": v} for k, v in values.items()])
            await s.execute(stmt.on_conflict_do_update(index_elements=[AppState.key], set_={"value": stmt.excluded.value}))
            await s.commit()

    async def enforce_storage(self) -> None:
        """Keep the database under storage.max_db_mb by dropping the oldest jobs.

        Postgres doesn't give deleted space back to the disk; it reuses it for new rows.
        So the size can't tell us when enough was deleted. Instead, crossing the high-water
        mark sets a job cap (jobs now minus evict_pct). The oldest jobs beyond the cap are
        deleted and their space is reused by new ones, so the size levels off. The cap is
        only lowered again if the database keeps growing anyway."""
        st = self.cfg.storage
        if not st.max_db_mb:
            if self.job_cap or self.evict_before:
                self.job_cap = self.cap_size_mb = self.evict_before = None
                await self._save_storage_state()
            return
        async with Session() as s:
            size_mb = (await s.execute(text("SELECT pg_database_size(current_database())"))).scalar_one() / 2**20
            n = (await s.execute(select(func.count()).select_from(Job))).scalar_one()
        high = st.max_db_mb * st.high_water_pct / 100
        changed = False
        if size_mb > high and (self.cap_size_mb is None or size_mb > self.cap_size_mb + st.max_db_mb * 0.05):
            self.job_cap = max(100, int(min(n, self.job_cap or n) * (1 - st.evict_pct / 100)))
            self.cap_size_mb = size_mb
            changed = True
            log.warning("database at %.0f MB of %d MB budget: keeping the newest %d jobs", size_mb, st.max_db_mb, self.job_cap)
        elif self.job_cap and size_mb < high * 0.6:  # budget raised or space freed: lift the cap
            self.job_cap = self.cap_size_mb = self.evict_before = None
            changed = True
            log.info("database at %.0f MB: storage cap lifted", size_mb)
        if self.evict_before and self.job_cap and n < self.job_cap * 0.8:  # room again for older postings
            self.evict_before = None
            changed = True
        if self.job_cap and n > self.job_cap:
            await self._evict(n - self.job_cap)
            changed = True
        if changed:
            await self._save_storage_state()

    async def _evict(self, count: int) -> None:
        """Delete `count` jobs: closed first, then duplicate copies, then the oldest postings."""
        deleted, newest = 0, None
        async with self._write_lock:
            while deleted < count:
                async with Session() as s:
                    row = (await s.execute(text("""
                        WITH victims AS (
                            SELECT id FROM jobs WHERE user_state = 'new'
                             ORDER BY (status = 'active'), (duplicate_of IS NULL),
                                      coalesce(posted_at, first_seen_at), id
                             LIMIT :k)
                        , gone AS (DELETE FROM jobs j USING victims v WHERE j.id = v.id
                                   RETURNING j.status, j.duplicate_of, coalesce(j.posted_at, j.first_seen_at) AS ts)
                        -- the age cutoff comes only from listings evicted for being oldest,
                        -- not from closed postings or duplicate copies (those go first at any age)
                        SELECT count(*), max(ts) FILTER (WHERE status = 'active' AND duplicate_of IS NULL)
                          FROM gone
                    """), {"k": min(5000, count - deleted)})).one()
                    # copies whose primary was deleted become stand-alone listings again
                    await s.execute(text("""
                        UPDATE jobs SET duplicate_of = NULL WHERE duplicate_of IS NOT NULL
                           AND NOT EXISTS (SELECT 1 FROM jobs p WHERE p.id = jobs.duplicate_of)"""))
                    await s.commit()
                if not row[0]:
                    break
                deleted += row[0]
                if row[1] is not None:
                    newest = max(newest, row[1]) if newest else row[1]
        if newest is not None:
            self.evict_before = max(self.evict_before, newest) if self.evict_before else newest
        async with db_engine.connect() as conn:  # make the freed space reusable right away
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.exec_driver_sql("VACUUM jobs")
        self.last_eviction = {"at": utcnow(), "deleted": deleted, "older_than": self.evict_before}
        log.info("storage budget: evicted %d oldest jobs (keeping postings newer than %s)", deleted, self.evict_before)

    # ------------------------------------------------------------ re-scoring

    async def rescore_all(self) -> int:
        """Re-run classification/matching for stored jobs after a config change.
        Non-tech records only get the cheap title-level pass."""
        matcher, count, last_id = self.matcher, 0, 0
        cols = (Job.id, Job.uid, Job.title, Job.company, Job.location, Job.remote, Job.description, Job.url,
                Job.apply_url, Job.authoritative, Job.department, Job.posted_at, Job.is_tech, Job.content_hash)
        while True:
            async with Session() as s:
                rows = (await s.execute(select(*cols).where(Job.id > last_id).order_by(Job.id).limit(2000))).all()
            if not rows:
                break
            last_id = rows[-1].id

            def work():
                out = []
                for r in rows:
                    n = NormalizedJob(
                        uid=r.uid, source="", board="", authoritative=r.authoritative, external_id="", company=r.company,
                        title=r.title, url=r.url, apply_url=r.apply_url, location=r.location, remote=r.remote,
                        description=r.description, department=r.department, employment_type="",
                        posted_at=r.posted_at, salary_min=None, salary_max=None, salary_currency="",
                        content_hash="", dedup_key="",
                    )
                    enrich(n, matcher)
                    # A slim non-tech record that is now tech (classifier change) holds only a
                    # truncated description; clearing the hash makes the next fetch store it in full.
                    content_hash = "" if n.is_tech and not r.is_tech else r.content_hash
                    out.append({"id": r.id, "content_hash": content_hash, **{c: getattr(n, c) for c in ENRICH_COLS}})
                return out

            updates = await asyncio.to_thread(work)
            async with self._write_lock, Session() as s:
                await s.execute(update(Job), updates)
                await s.commit()
            count += len(updates)
        log.info("re-scored %d jobs", count)
        return count

    # ------------------------------------------------------------ validation

    async def run_validation(self) -> dict[str, int]:
        if self.validating:
            return {"checked": 0}
        self.validating = True
        try:
            v = self.cfg.validation
            due = utcnow() - timedelta(hours=v.recheck_after_hours)
            async with Session() as s:
                jobs = (await s.execute(
                    select(Job).where(
                        Job.status == "active", Job.authoritative.is_(False), Job.user_state != "hidden",
                        Job.is_tech.is_(True), Job.duplicate_of.is_(None),
                        or_(Job.last_checked_at.is_(None), Job.last_checked_at < due),
                    ).order_by(Job.last_checked_at.asc().nulls_first()).limit(v.batch_size)
                )).scalars().all()
            sem = asyncio.Semaphore(self.cfg.fetch.max_concurrency)

            async def check(job: Job):
                async with sem:
                    return job.id, await self.verify_job(job)

            results = await asyncio.gather(*(check(j) for j in jobs))
            closed = 0
            async with self._write_lock, Session() as s:
                for job_id, res in results:
                    closed += await self._apply_verify(s, job_id, res)
                await s.commit()
            self.last_validation_at = utcnow()
            log.info("validation: checked %d, closed %d", len(results), closed)
            return {"checked": len(results), "closed": closed}
        finally:
            self.validating = False

    async def verify_job(self, job: Job) -> VerifyResult:
        src = SOURCES.get(job.source)
        board = job.board.split(":", 1)[-1]
        try:
            if src and job.authoritative:
                res = await src.verify(self.http, board, job.external_id, job.url)
                if res is not None:
                    return res
            return await self._page_check(job.url, job.external_id)
        except FetchError as e:
            return VerifyResult(None, f"check failed: {e}"[:200])
        except Exception as e:  # never let one bad page break the batch
            log.warning("verify %s failed: %s", job.uid, e)
            return VerifyResult(None, f"check error: {type(e).__name__}")

    async def _page_check(self, url: str, external_id: str) -> VerifyResult:
        page = await self.http.get_page(url)
        if page.status in (404, 410):
            return VerifyResult(False, f"HTTP {page.status}")
        if page.status >= 400:
            return VerifyResult(None, f"HTTP {page.status} (inconclusive)")
        visible = (await asyncio.to_thread(html_to_text, page.text)).lower()
        for phrase in self.cfg.validation.dead_phrases:
            if phrase.lower() in visible:
                return VerifyResult(False, f"page says '{phrase}'")
        ext = external_id.lower()
        if len(ext) >= 4 and ext in url.lower() and ext not in page.final_url.lower():
            return VerifyResult(False, "redirected away from the posting")
        return VerifyResult(True, f"HTTP {page.status}, posting page live")

    async def _apply_verify(self, s, job_id: int, res: VerifyResult) -> int:
        values: dict[str, Any] = {"last_checked_at": utcnow(), "check_result": res.detail[:200]}
        if res.alive is False:
            values.update(status="closed", closed_at=utcnow())
        elif res.alive is True:
            values.update(status="active", closed_at=None)
        await s.execute(update(Job).where(Job.id == job_id).values(**values))
        return int(res.alive is False)

    async def verify_one(self, job_id: int) -> Job | None:
        async with Session() as s:
            job = await s.get(Job, job_id)
        if not job:
            return None
        res = await self.verify_job(job)
        async with Session() as s:
            await self._apply_verify(s, job_id, res)
            await s.commit()
            return await s.get(Job, job_id)

    # ------------------------------------------------------------ tracker sync

    async def reconcile_tracker(self) -> None:
        """Make the database reflect the tracker file (the source of truth)."""
        tracker.reload_if_changed()
        async with Session() as s:
            tracked_uids: set[str] = set()
            for state in ("applied", "hidden"):
                for uid, entry in tracker.data[state].items():
                    tracked_uids.add(uid)
                    # Match the exact posting only: a dedup key ("company|title") is shared by
                    # distinct roles (other cities / teams), which must not be marked too.
                    res = await s.execute(
                        update(Job).where(Job.uid == uid).values(user_state=state, notes=entry.get("notes", ""))
                    )
                    if res.rowcount == 0 and state == "applied" and entry.get("url"):
                        await s.execute(insert(Job).values(
                            uid=uid, source=entry.get("source") or uid.split(":", 1)[0], board="tracker",
                            authoritative=False, external_id=uid.rsplit(":", 1)[-1][:200],
                            company=entry.get("company") or "?", title=entry.get("title") or "?",
                            location=entry.get("location") or "", url=entry["url"], apply_url=entry.get("apply_url") or "",
                            content_hash="", dedup_key=entry.get("dedup_key") or uid, skills=[], matched=True,
                            is_tech=True, user_state="applied", notes=entry.get("notes", ""), status="active",
                            check_result="restored from tracker file",
                        ).on_conflict_do_nothing())
            # Rows marked in the DB but no longer in the file (file edited/reset) go back to "new".
            stale = select(Job.id).where(Job.user_state != "new", Job.uid.not_in(list(tracked_uids)) if tracked_uids else text("true"))
            await s.execute(update(Job).where(Job.id.in_(stale)).values(user_state="new"))
            # ...then carry each decision to the cross-board copies linked to that posting.
            await s.execute(text(COPY_STATES_SQL))
            await s.commit()
        log.info("tracker reconciled: %s", tracker.counts())

    # ------------------------------------------------------------ status

    async def _resolver_summary(self) -> dict[str, Any]:
        async with Session() as s:
            row = (await s.execute(text("""
                SELECT count(*) AS attempted, count(*) FILTER (WHERE found <> '') AS found FROM company_resolutions
            """))).mappings().one()
            pending = (await s.execute(text("""
                SELECT count(*) FROM jobs WHERE status='active' AND is_tech AND trusted AND link_rank >= 3 AND duplicate_of IS NULL
            """))).scalar_one()
        return {**dict(row), "indirect_listings": pending, "last": self.resolver_stats}

    async def status(self) -> dict[str, Any]:
        async with Session() as s:
            ats_list = ",".join(f"'{a}'" for a in ATS_SOURCES)
            runs = (await s.execute(text(f"""
                SELECT DISTINCT ON (board) board, source, started_at, finished_at, ok, unchanged,
                       fetched, matched, new, updated, closed, rejected, error
                  FROM fetch_runs WHERE source NOT IN ({ats_list})
                 ORDER BY board, started_at DESC
            """))).mappings().all()
            reg = (await s.execute(text("""
                SELECT ats,
                       count(*) AS total,
                       count(*) FILTER (WHERE enabled) AS enabled,
                       count(*) FILTER (WHERE status = 'ok') AS with_jobs,
                       count(*) FILTER (WHERE status = 'empty') AS empty,
                       count(*) FILTER (WHERE status = 'error') AS failing,
                       count(*) FILTER (WHERE status = 'gone') AS gone,
                       count(*) FILTER (WHERE status = 'new') AS pending,
                       count(*) FILTER (WHERE tech_count > 0) AS with_tech,
                       count(*) FILTER (WHERE matched_count > 0) AS with_matches,
                       count(*) FILTER (WHERE enabled AND next_fetch_at <= now()) AS due
                  FROM companies GROUP BY ats ORDER BY ats
            """))).mappings().all()
            failing = (await s.execute(
                select(Company.ats, Company.token, Company.name, Company.last_error, Company.fail_count)
                .where(Company.status == "error").order_by(Company.pinned.desc(), Company.fail_count.desc()).limit(25)
            )).mappings().all()
        live: dict[str, Any] = {}
        if ROLE == "api":
            live = self.worker_state() or {}
        tracker.reload_if_changed()
        out = {
            "leader": self.is_leader or bool(live),
            "fetching": self.fetching or self._agg_lock.locked(),
            "validating": self.validating,
            "boards_in_flight": self.boards_in_flight,
            "boards_done": self.boards_done,
            "last_fetch_started": self.last_fetch_started,
            "last_fetch_finished": self.last_fetch_finished,
            "next_fetch_at": self.next_fetch_at,
            "last_validation_at": self.last_validation_at,
            "last_maintenance_at": self.last_maintenance_at,
            "config_version": config_store.version,
            "config_error": config_store.error,
            "last_error": self.last_error,
            "registry": [dict(r) for r in reg],
            "resolver": await self._resolver_summary(),
            "failing_boards": [dict(r) for r in failing],
            "boards": [dict(r) for r in runs],
        }
        async with Session() as s:
            st = dict((await s.execute(select(AppState.key, AppState.value))).tuples().all())
            size = (await s.execute(text("SELECT pg_database_size(current_database())"))).scalar_one()
        out["storage"] = {
            "db_mb": round(size / 2**20), "budget_mb": self.cfg.storage.max_db_mb or None,
            "tech_only": self.cfg.storage.tech_only, "job_cap": int(st["job_cap"]) if st.get("job_cap") else None,
            "evict_before": st.get("evict_before") or None,
        }
        if live:
            out.update({k: live.get(k) for k in STATUS_FIELDS if k != "resolver_stats"})
            out["resolver"]["last"] = live.get("resolver_stats") or {}
            out["validating"] = out["validating"] or self.validating
        return out


engine = Engine()
