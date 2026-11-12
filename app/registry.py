"""Company registry: which employer career boards are polled, and when.

Seeds come from CSV files (config/companies.csv baseline + data/companies.discovered.csv
written by `python -m app.discover`), from the ATS lists in config.yaml (pinned,
always polled at the fast interval) and are learned at runtime from aggregator
postings whose apply link points at a company's ATS board.

Polling is adaptive so tens of thousands of boards stay cheap:
  hot  (pinned / has matching jobs)   -> fetch.interval_minutes
  warm (tech jobs, none matching)     -> fetch.warm_interval_hours
  cool (only non-tech jobs)           -> fetch.cool_interval_hours
  cold (no open jobs)                 -> fetch.cold_interval_hours
  failing                             -> exponential backoff, retired after N consecutive 404s
"""
from __future__ import annotations

import csv
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from .config import AppConfig
from .db import Company, Session
from .sources import ATS_SOURCES
from .sources.ats import split_board, workday_board
from .trust import is_middleman

log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_.%-]{1,200}$|^[A-Za-z0-9-]+\.wd\d+/[A-Za-z0-9_-]{1,150}$")

# apply-link patterns that reveal an employer's ATS board
BOARD_LINK_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/.*[?&]for=([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)/", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)/", re.I)),
    ("recruitee", re.compile(r"//([a-z0-9-]+)\.recruitee\.com/", re.I)),
    ("personio", re.compile(r"//([a-z0-9-]+)\.jobs\.personio\.(?:de|com)/", re.I)),
    ("breezy", re.compile(r"//([a-z0-9-]+)\.breezy\.hr/", re.I)),
    ("bamboohr", re.compile(r"//([a-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)", re.I)),
    ("teamtailor", re.compile(r"//([a-z0-9-]+)\.teamtailor\.com/", re.I)),
    ("rippling", re.compile(r"ats\.rippling\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)),
    ("pinpoint", re.compile(r"//([a-z0-9-]+)\.pinpointhq\.com", re.I)),
    ("jazzhr", re.compile(r"//([a-z0-9-]+)\.applytojob\.com", re.I)),
]
_NOT_TOKENS = {"www", "app", "j", "api", "embed", "jobs", "careers", "search", "oneclick-ui", "sr-jobs", "static"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def board_from_link(url: str) -> tuple[str, str] | None:
    if tok := workday_board(url):
        return "workday", tok
    for ats, pat in BOARD_LINK_PATTERNS:
        if m := pat.search(url or ""):
            tok = m.group(1)
            if tok.lower() not in _NOT_TOKENS:
                return ats, tok
    return None


def _rows_from_csv(path: Path) -> list[dict]:
    rows = []
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                ats, token = (r.get("ats") or "").strip().lower(), (r.get("token") or "").strip()
                if ats in ATS_SOURCES and TOKEN_RE.match(token):
                    rows.append({"ats": ats, "token": token, "name": (r.get("name") or "").strip()[:300],
                                 "website": (r.get("website") or "").strip(), "origin": (r.get("origin") or path.name)[:120]})
    except FileNotFoundError:
        pass
    except (OSError, csv.Error) as e:
        log.error("cannot read company file %s: %s", path, e)
    return rows


async def seed(cfg: AppConfig) -> dict[str, int]:
    """Upsert companies from CSV files and config.yaml. Existing rows keep their
    polling state; new ones are spread over the first hour to avoid a burst."""
    rows: dict[tuple[str, str], dict] = {}
    for f in cfg.sources.company_files:
        for r in _rows_from_csv(Path(f)):
            rows.setdefault((r["ats"], r["token"].lower()), r)
    pinned: set[tuple[str, str]] = set()
    for ats in ATS_SOURCES:
        for entry in getattr(cfg.sources, ats, []):
            token, name = split_board(entry)
            key = (ats, token.lower())
            pinned.add(key)
            rows[key] = {**rows.get(key, {}), "ats": ats, "token": token, "name": name, "origin": "config"}
    stats = {"seen": len(rows), "excluded": 0}
    values = []
    now = utcnow()
    for key, r in rows.items():
        if cfg.trust.exclude_middlemen_from_registry and key not in pinned and is_middleman(r.get("name") or r["token"], "", r.get("website", "")):
            stats["excluded"] += 1
            continue
        values.append({
            "ats": r["ats"], "token": r["token"], "token_key": key[1], "name": r.get("name") or "",
            "website": r.get("website") or "", "origin": r.get("origin") or "", "pinned": key in pinned,
            "next_fetch_at": now if key in pinned else now + timedelta(seconds=random.uniform(0, 3600)),
        })
    async with Session() as s:
        for i in range(0, len(values), 2000):
            stmt = insert(Company).values(values[i:i + 2000])
            stmt = stmt.on_conflict_do_update(
                index_elements=[Company.ats, Company.token_key],
                set_={
                    "pinned": stmt.excluded.pinned,
                    "name": func.coalesce(func.nullif(Company.name, ""), stmt.excluded.name),
                    "website": func.coalesce(func.nullif(Company.website, ""), stmt.excluded.website),
                },
            )
            await s.execute(stmt)
        # Boards removed from config.yaml are no longer pinned.
        if pinned:
            keys = [f"{a}:{t}" for a, t in pinned]
            await s.execute(update(Company).where(Company.pinned.is_(True), (Company.ats + ":" + Company.token_key).not_in(keys)).values(pinned=False))
        else:
            await s.execute(update(Company).where(Company.pinned.is_(True)).values(pinned=False))
        await s.commit()
        stats["total"] = (await s.execute(select(func.count()).select_from(Company))).scalar_one()
    log.info("company registry seeded: %s", stats)
    return stats


async def register_from_links(links: list[tuple[str, str]]) -> int:
    """Learn new boards from aggregator apply links: [(company name, url)]."""
    values = {}
    for name, url in links:
        hit = board_from_link(url)
        if hit and not is_middleman(name):
            ats, tok = hit
            values[(ats, tok.lower())] = {"ats": ats, "token": tok, "token_key": tok.lower(), "name": name[:300],
                                          "origin": "aggregator-link", "next_fetch_at": utcnow()}
    if not values:
        return 0
    async with Session() as s:
        res = await s.execute(insert(Company).values(list(values.values())).on_conflict_do_nothing().returning(Company.id))
        n = len(res.all())
        await s.commit()
    if n:
        log.info("registered %d new boards from aggregator links", n)
    return n


_LOCAL_NET_ERR = re.compile(r"name resolution|Name or service not known|Network is unreachable|No address associated", re.I)


def next_fetch(c: Company, cfg: AppConfig, *, ok: bool, status_code: int | None, jobs: int, tech: int, matched: int,
               error: str = "") -> dict:
    """Compute the post-fetch registry update for a company."""
    f = cfg.fetch
    now = utcnow()
    jitter = random.uniform(0.9, 1.1)
    if not ok and _LOCAL_NET_ERR.search(error):
        # Our own network/DNS hiccup says nothing about the board: retry soon, no penalty.
        return {"status": c.status, "next_fetch_at": now + timedelta(minutes=10 * jitter)}
    if not ok:
        fails = c.fail_count + 1
        gone = status_code in (404, 410) and fails >= f.gone_after_failures
        backoff = min(f.max_backoff_hours, 0.5 * 2 ** fails)
        return {
            "fail_count": fails, "status": "gone" if gone else "error",
            "enabled": not gone or c.pinned,
            "next_fetch_at": now + (timedelta(days=30) if gone else timedelta(hours=backoff * jitter)),
        }
    if c.pinned or matched:
        delay = timedelta(minutes=f.interval_minutes)
    elif tech:
        delay = timedelta(hours=f.warm_interval_hours)
    elif jobs:
        delay = timedelta(hours=f.cool_interval_hours)
    else:
        delay = timedelta(hours=f.cold_interval_hours)
    return {
        "fail_count": 0, "status": "ok" if jobs else "empty", "enabled": True, "last_error": "",
        "jobs_count": jobs, "tech_count": tech, "matched_count": matched,
        "last_fetched_at": now, "next_fetch_at": now + delay * jitter,
    }
