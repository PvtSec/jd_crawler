"""Read-only REST API consumed by the dashboard.

This public build lists every job found. Personal tracking (applied / hidden,
notes, export) and manual triggers (refresh, verify, liveness check) are not
exposed; the background engine keeps the data fresh on its own.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Select, and_, func, literal_column, nulls_last, or_, select, text

from .config import config_store
from .db import Job, Session
from .engine import engine
from .skills import SENIORITY_LEVELS

router = APIRouter(prefix="/api")

View = Literal["matches", "applied", "hidden", "all"]
SORTS = {
    "posted": Job.posted_at, "first_seen": Job.first_seen_at, "title": Job.title,
    "company": Job.company, "location": Job.location, "source": Job.source, "seniority": Job.seniority,
    "years": Job.years_required, "salary": Job.salary_max,
}
LIST_COLS = (
    Job.id, Job.uid, Job.source, Job.company, Job.title, Job.location, Job.remote, Job.department,
    Job.employment_type, Job.url, Job.apply_url, Job.posted_at, Job.first_seen_at, Job.last_seen_at,
    Job.last_checked_at, Job.check_result, Job.status, Job.skills, Job.seniority, Job.years_required,
    Job.salary_min, Job.salary_max, Job.salary_currency, Job.duplicate_of, Job.authoritative,
    Job.category, Job.is_tech, Job.languages, Job.text_lang, Job.lang_ok, Job.trust_flags, Job.trusted, Job.link_rank,
)


class Filters(BaseModel):
    view: View = "all"
    q: str | None = None
    source: list[str] = []
    company: list[str] = []
    location: str | None = None
    remote: bool | None = None
    seniority: list[str] = []
    skills: list[str] = []
    skills_mode: Literal["any", "all"] = "all"
    exclude_skills: list[str] = []
    max_years: int | None = None
    min_score: int | None = None
    posted_days: int | None = None
    status: Literal["active", "closed", "any"] = "active"
    include_unmatched: bool = True
    include_duplicates: bool = False
    has_salary: bool = False
    tech_only: bool = False
    include_flagged: bool = True
    include_lang_restricted: bool = True
    include_indirect: bool = True
    category: list[str] = []


def _filters(
    view: View = "all",
    q: str | None = Query(None, max_length=200),
    source: list[str] = Query([]),
    company: list[str] = Query([]),
    location: str | None = Query(None, max_length=100),
    remote: bool | None = None,
    seniority: list[str] = Query([]),
    skills: list[str] = Query([]),
    skills_mode: Literal["any", "all"] = "all",
    exclude_skills: list[str] = Query([]),
    max_years: int | None = Query(None, ge=0, le=40),
    min_score: int | None = Query(None, ge=0, le=100),
    posted_days: int | None = Query(None, ge=1, le=3650),
    status: Literal["active", "closed", "any"] = "active",
    include_unmatched: bool = True,
    include_duplicates: bool = False,
    has_salary: bool = False,
    tech_only: bool = False,
    include_flagged: bool = True,
    include_lang_restricted: bool = True,
    include_indirect: bool = True,
    category: list[str] = Query([]),
) -> Filters:
    return Filters(**{k: v for k, v in locals().items()})


def _apply(stmt: Select, f: Filters) -> Select:
    conds: list[Any] = []
    if f.view == "matches":
        conds.append(Job.user_state == "new")
    elif f.view in ("applied", "hidden"):
        conds.append(Job.user_state == f.view)
    if f.view == "matches" or f.view == "all":
        if not f.include_unmatched:
            conds.append(Job.matched.is_(True))
        if f.tech_only:
            conds.append(Job.is_tech.is_(True))
        if not f.include_flagged:
            conds.append(Job.trusted.is_(True))
        if not f.include_lang_restricted:
            conds.append(Job.lang_ok.is_(True))
        if not f.include_indirect:  # only listings whose apply link reaches the employer directly
            conds.append(Job.link_rank < 3)
    if not f.include_duplicates:  # every view shows the best copy of a role
        conds.append(Job.duplicate_of.is_(None))
    if f.category:
        conds.append(Job.category.in_(f.category))
    if f.status != "any":
        conds.append(Job.status == f.status)
    if f.q and f.q.strip():
        q = f.q.strip()
        tsq = func.websearch_to_tsquery(literal_column("'simple'"), q).op("||")(
            func.websearch_to_tsquery(literal_column("'english'"), q))
        like = f"%{q}%"
        conds.append(or_(Job.search.op("@@")(tsq), Job.title.ilike(like), Job.company.ilike(like)))
    if f.source:
        conds.append(Job.source.in_(f.source))
    if f.company:
        conds.append(Job.company.in_(f.company))
    if f.location:
        conds.append(Job.location.ilike(f"%{f.location}%"))
    if f.remote is not None:
        conds.append(Job.remote.is_(f.remote))
    if f.seniority:
        conds.append(Job.seniority.in_([s.lower() for s in f.seniority]))
    if f.skills:
        arr = func.cast(f.skills, Job.skills.type)
        conds.append(Job.skills.op("@>")(arr) if f.skills_mode == "all" else Job.skills.op("&&")(arr))
    if f.exclude_skills:
        conds.append(~Job.skills.op("&&")(func.cast(f.exclude_skills, Job.skills.type)))
    if f.max_years is not None:
        conds.append(or_(Job.years_required.is_(None), Job.years_required <= f.max_years))
    if f.min_score is not None:
        conds.append(Job.match_score >= f.min_score)
    if f.posted_days:
        since = datetime.now(timezone.utc) - timedelta(days=f.posted_days)
        conds.append(func.coalesce(Job.posted_at, Job.first_seen_at) >= since)
    if f.has_salary:
        conds.append(or_(Job.salary_min.is_not(None), Job.salary_max.is_not(None)))
    return stmt.where(and_(*conds)) if conds else stmt


def _row(r: Any) -> dict[str, Any]:
    return dict(r._mapping)


@router.get("/health")
async def health() -> dict[str, Any]:
    async with Session() as s:
        await s.execute(text("SELECT 1"))
    return {"ok": True}


@router.get("/jobs")
async def list_jobs(
    sort: str = "posted",
    order: Literal["asc", "desc"] = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    f: Filters = Depends(_filters),
) -> dict[str, Any]:
    col = SORTS.get(sort)
    if col is None:
        raise HTTPException(400, f"sort must be one of {sorted(SORTS)}")
    if sort == "posted":
        col = func.coalesce(Job.posted_at, Job.first_seen_at)
    ordering = [nulls_last(col.desc() if order == "desc" else col.asc()), Job.id.desc()]
    stmt = _apply(select(*LIST_COLS), f).order_by(*ordering).offset((page - 1) * page_size).limit(page_size)
    count = _apply(select(func.count()).select_from(Job), f)
    async with Session() as s:
        total = (await s.execute(count)).scalar_one()
        items = [_row(r) for r in (await s.execute(stmt)).all()]
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/jobs/{job_id}")
async def get_job(job_id: int) -> dict[str, Any]:
    async with Session() as s:
        row = (await s.execute(select(*LIST_COLS, Job.description).where(Job.id == job_id))).first()
        if not row:
            raise HTTPException(404, "job not found")
        dups = (await s.execute(
            select(Job.id, Job.source, Job.url).where(Job.duplicate_of == job_id)
        )).all()
    return {**_row(row), "duplicates": [_row(d) for d in dups]}


_facet_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@router.get("/facets")
async def facets(view: View = "all") -> dict[str, Any]:
    """Counts for filter widgets (cached briefly; cheap enough to recompute)."""
    hit = _facet_cache.get(view)
    if hit and time.monotonic() - hit[0] < 20:
        return hit[1]
    base = Filters(view=view, status="active" if view in ("matches", "all") else "any")
    async with Session() as s:
        skills_sub = _apply(select(func.unnest(Job.skills).label("skill")), base).subquery()
        skills = (await s.execute(
            select(skills_sub.c.skill, func.count()).group_by(skills_sub.c.skill).order_by(func.count().desc())
        )).all()

        async def counts(col):
            rows = (await s.execute(_apply(select(col, func.count()), base).group_by(col).order_by(func.count().desc()))).all()
            return [{"value": v, "count": c} for v, c in rows if v is not None]

        out = {
            "skills": [{"value": k, "count": c} for k, c in skills],
            "sources": await counts(Job.source),
            "seniority": await counts(Job.seniority),
            "categories": [{"value": v, "count": c} for v, c in (await s.execute(
                _apply(select(Job.category, func.count()), base.model_copy(update={"tech_only": False, "include_unmatched": True}))
                .group_by(Job.category).order_by(func.count().desc()))).all()],
            "companies": (await counts(Job.company))[:400],
            "seniority_levels": SENIORITY_LEVELS + ["unspecified"],
        }
    _facet_cache[view] = (time.monotonic(), out)
    return out


_stats_cache: tuple[float, dict[str, Any]] | None = None


@router.get("/stats")
async def stats() -> dict[str, Any]:
    global _stats_cache
    if _stats_cache and time.monotonic() - _stats_cache[0] < 10:
        return _stats_cache[1]
    async with Session() as s:
        row = (await s.execute(text("""
            SELECT
              count(*) FILTER (WHERE status='active' AND duplicate_of IS NULL) AS open_jobs,
              count(DISTINCT company) FILTER (WHERE status='active' AND duplicate_of IS NULL) AS open_companies,
              count(*) FILTER (WHERE status='active' AND duplicate_of IS NULL
                               AND first_seen_at > now() - interval '24 hours') AS new_24h,
              count(*) FILTER (WHERE status='active' AND duplicate_of IS NULL AND is_tech) AS open_tech_jobs,
              count(*) FILTER (WHERE status='active' AND duplicate_of IS NULL AND remote) AS open_remote_jobs,
              count(*) FILTER (WHERE status='closed') AS closed,
              count(*) AS total
            FROM jobs
        """))).mappings().one()
        reg = (await s.execute(text("""
            SELECT count(*) AS companies,
                   count(*) FILTER (WHERE status = 'ok') AS companies_hiring,
                   count(*) FILTER (WHERE tech_count > 0) AS companies_tech,
                   count(*) FILTER (WHERE status = 'new') AS companies_pending
              FROM companies WHERE enabled
        """))).mappings().one()
    out = {**dict(row), **dict(reg)}
    _stats_cache = (time.monotonic(), out)
    return out


@router.get("/companies")
async def companies(
    q: str | None = Query(None, max_length=100),
    ats: str | None = None,
    status: str | None = None,
    sort: Literal["name", "jobs", "tech", "matched", "next"] = "matched",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    from .db import Company

    conds = [Company.enabled.is_(True)]
    if q:
        conds.append(or_(Company.name.ilike(f"%{q}%"), Company.token.ilike(f"%{q}%")))
    if ats:
        conds.append(Company.ats == ats)
    if status:
        conds.append(Company.status == status)
    order = {"name": Company.name.asc(), "jobs": Company.jobs_count.desc(), "tech": Company.tech_count.desc(),
             "matched": Company.matched_count.desc(), "next": Company.next_fetch_at.asc()}[sort]
    cols = (Company.id, Company.ats, Company.token, Company.name, Company.website, Company.status, Company.pinned,
            Company.jobs_count, Company.tech_count, Company.matched_count, Company.last_fetched_at, Company.next_fetch_at,
            Company.origin)
    async with Session() as s:
        total = (await s.execute(select(func.count()).select_from(Company).where(*conds))).scalar_one()
        rows = (await s.execute(select(*cols).where(*conds).order_by(order, Company.id)
                                .offset((page - 1) * page_size).limit(page_size))).all()
    return {"total": total, "items": [_row(r) for r in rows]}


@router.get("/status")
async def status() -> dict[str, Any]:
    return await engine.status()


@router.get("/config")
async def get_config() -> dict[str, Any]:
    from .skills import get_extractor

    cfg = config_store.current
    return {  # the full config is not published (server settings may hold credentials)
        "error": config_store.error,
        "version": config_store.version,
        "known_skills": get_extractor(cfg).names,
    }
