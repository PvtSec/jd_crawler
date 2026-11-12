"""Direct-apply-link resolver.

Aggregator pages often funnel applicants into the aggregator's own sign-up /
login flow instead of the employer. For every aggregator-only tech listing we
look for the employer's own ATS board:

  1. derive board-slug candidates from the company name,
  2. probe every supported ATS with them,
  3. accept a board only if it lists the *same role* (normalised title match),
     which rules out a different company that happens to share the slug,
  4. register the board in the company registry and fetch it right away.

Cross-board dedup then collapses the aggregator copy into the employer's
posting, whose apply link goes straight to the company's hiring system.
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from .db import Company, CompanyResolution, Session
from .matching import dedup_key
from .sources import ATS_SOURCES, SOURCES, FetchContext
from .trust import is_middleman

log = logging.getLogger(__name__)

RETRY_AFTER = timedelta(days=7)   # re-try companies we could not resolve weekly
BATCH = 40                        # companies per cycle
_SUFFIX = re.compile(r"\b(inc|llc|ltd|limited|gmbh|ag|sa|sas|bv|plc|corp|corporation|co|company|group|holdings?|technologies|technology|labs?|hq|the)\b\.?", re.I)


def company_key(name: str) -> str:
    # Must equal the SQL expression regexp_replace(lower(company), '[^a-z0-9]+', '', 'g').
    return re.sub(r"[^a-z0-9]+", "", name.lower())[:200]


def slug_candidates(name: str) -> list[str]:
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"\([^)]*\)", " ", base)
    full = re.sub(r"[^a-z0-9]+", " ", base).split()
    core = re.sub(r"[^a-z0-9]+", " ", _SUFFIX.sub(" ", base)).split()
    out = []
    for words in (core, full):
        if words:
            out += ["".join(words), "-".join(words)]
    if core and len(core[0]) >= 4:
        out.append(core[0])
    return [c for c in dict.fromkeys(out) if 2 <= len(c) <= 60]


def _title_key(company: str, title: str) -> str:
    return dedup_key(company, title).split("|", 1)[1]


async def resolve_batch(engine) -> dict[str, int]:
    """One resolver cycle. `engine` provides http, cfg, matcher and _fetch_company."""
    now = datetime.now(timezone.utc)
    async with Session() as s:
        rows = (await s.execute(text("""
            SELECT j.company, array_agg(DISTINCT j.title) AS titles
              FROM jobs j
             WHERE j.status = 'active' AND j.is_tech AND j.link_rank >= 3 AND j.duplicate_of IS NULL
               AND (j.trusted OR j.user_state <> 'new')
               AND NOT EXISTS (SELECT 1 FROM company_resolutions r
                                WHERE r.company_key = regexp_replace(lower(j.company), '[^a-z0-9]+', '', 'g')
                                  AND (r.found <> '' OR r.attempted_at > :retry))
             GROUP BY j.company
             ORDER BY max(j.first_seen_at) DESC
             LIMIT :n
        """), {"retry": now - RETRY_AFTER, "n": BATCH})).all()
    if not rows:
        return {"companies": 0, "found": 0}

    ctx = FetchContext(cfg=engine.cfg, http=engine.http, title_ok=lambda t: True, light=True)
    sem = asyncio.Semaphore(6)
    found_total = 0

    async def resolve(company: str, titles: list[str]) -> None:
        nonlocal found_total
        key = company_key(company)
        wanted = {_title_key(company, t) for t in titles}
        hit, detail = "", ""
        if is_middleman(company):
            detail = "middleman; not resolved"
        else:
            async with sem:
                hit, detail = await _probe(ctx, company, wanted)
        async with Session() as s:
            await s.execute(insert(CompanyResolution).values(
                company_key=key, company=company[:300], attempted_at=now, found=hit, detail=detail[:500],
            ).on_conflict_do_update(index_elements=[CompanyResolution.company_key],
                                    set_={"attempted_at": now, "found": hit, "detail": detail[:500]}))
            await s.commit()
        if hit:
            found_total += 1
            ats, token = hit.split(":", 1)
            c = await _register(ats, token, company)
            if c is not None:
                await engine._fetch_company(c)  # ingest now -> dedup picks the direct posting

    await asyncio.gather(*(resolve(r.company, list(r.titles)) for r in rows))
    await engine._mark_duplicates()
    log.info("resolver: %d companies checked, %d employer boards found", len(rows), found_total)
    return {"companies": len(rows), "found": found_total}


async def _probe(ctx: FetchContext, company: str, wanted: set[str]) -> tuple[str, str]:
    # Already in the registry under a matching name? Then it just needs a fetch.
    async with Session() as s:
        known = (await s.execute(
            select(Company.ats, Company.token).where(
                func.regexp_replace(func.lower(Company.name), "[^a-z0-9]+", "", "g") == company_key(company),
                Company.enabled.is_(True)).limit(1)
        )).first()
    if known:
        return f"{known.ats}:{known.token}", "already in registry"
    tried = 0
    slugs = slug_candidates(company)

    async def matches(ats: str, token: str) -> bool:
        try:
            res = await SOURCES[ats].fetch_board(ctx, token, company)
        except Exception:  # any parser/transport problem = not this board
            return False
        return res.ok and bool(res.jobs) and bool({_title_key(company, j.title) for j in res.jobs} & wanted)

    for slug in slugs:
        for ats in ATS_SOURCES:
            if ats == "workday":
                continue
            tried += 1
            if await matches(ats, slug):
                return f"{ats}:{slug}", f"same role found on {ats}/{slug}"
    # Workday tokens are "<tenant>.<wdN>/<site>": locate the tenant's site first.
    workday = SOURCES["workday"]
    for slug in [s for s in slugs if "-" not in s][:2]:
        tried += 1
        for token in await workday.find_boards(ctx.http, slug):
            if await matches("workday", token):
                return f"workday:{token}", f"same role found on workday/{token}"
    return "", f"no employer board found ({tried} probes)"


async def _register(ats: str, token: str, name: str) -> Company | None:
    async with Session() as s:
        await s.execute(insert(Company).values(
            ats=ats, token=token, token_key=token.lower(), name=name[:300], origin="resolver",
            next_fetch_at=datetime.now(timezone.utc),
        ).on_conflict_do_nothing())
        await s.commit()
        return (await s.execute(select(Company).where(Company.ats == ats, Company.token_key == token.lower()))).scalar_one_or_none()
