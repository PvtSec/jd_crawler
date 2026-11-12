"""Public job aggregators with open JSON APIs (non-authoritative feeds).

These return a recent slice of postings, so a job missing from the feed does
not mean it closed; liveness is checked by the validator instead. Each source
declares a minimum polling interval to respect the provider's fair-use terms.
"""
from __future__ import annotations

from typing import Any

from ..config import AggregatorCfg, AppConfig
from ..http import FetchError
from .base import BoardResult, FetchContext, RawJob, Source, clean, html_to_text, parse_dt, to_int


def fix_mojibake(s: str) -> str:
    """Repair UTF-8 text that was decoded as Latin-1 (common in RemoteOK data)."""
    if "Ã" in s or "â" in s:
        try:
            return s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s
    return s


class _Aggregator(Source):
    authoritative = False
    min_interval_minutes = 60

    def cfg(self, cfg: AppConfig) -> AggregatorCfg:
        return getattr(cfg.sources, self.name)

    def boards(self, cfg: AppConfig) -> list[str]:
        return [self.name] if self.cfg(cfg).enabled else []

    async def fetch_board(self, ctx: FetchContext, board: str, name: str = "") -> BoardResult:
        seen: dict[str, RawJob] = {}
        errors = []
        try:
            async for job in self._iter(ctx, self.cfg(ctx.cfg)):
                if job.external_id and job.external_id not in seen:
                    seen[job.external_id] = job
        except FetchError as e:
            errors.append(str(e))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            errors.append(f"unexpected payload: {type(e).__name__}: {e}")
        # Partial results are still useful for non-authoritative feeds.
        return self.result(board, jobs=list(seen.values()), ok=not errors or bool(seen), error="; ".join(errors))

    async def _iter(self, ctx: FetchContext, c: AggregatorCfg):  # pragma: no cover - interface
        raise NotImplementedError
        yield


class Remotive(_Aggregator):
    name = "remotive"
    min_interval_minutes = 360  # Remotive asks for a handful of requests per day

    async def _iter(self, ctx: FetchContext, c: AggregatorCfg):
        data, _ = await ctx.http.get_json("https://remotive.com/api/remote-jobs")
        for j in data.get("jobs", []):
            yield RawJob(
                external_id=str(j["id"]),
                company=clean(j.get("company_name")),
                title=clean(j.get("title")),
                url=j.get("url") or "",
                location=clean(j.get("candidate_required_location")) or "Remote",
                remote=True,
                description=html_to_text(j.get("description")),
                department=clean(j.get("category"), 200),
                employment_type=clean(j.get("job_type"), 80).replace("_", " "),
                posted_at=parse_dt(j.get("publication_date")),
                tags=list(j.get("tags") or []),
            )


class RemoteOK(_Aggregator):
    name = "remoteok"

    async def _iter(self, ctx: FetchContext, c: AggregatorCfg):
        urls = [("https://remoteok.com/api", {"tag": q}) for q in c.queries] or [("https://remoteok.com/api", None)]
        for url, params in urls:
            data, _ = await ctx.http.get_json(url, params=params)
            for j in data:
                if not isinstance(j, dict) or "id" not in j or "position" not in j:
                    continue  # first element is the legal notice
                yield RawJob(
                    external_id=str(j["id"]),
                    company=fix_mojibake(clean(j.get("company"))),
                    title=fix_mojibake(clean(j.get("position"))),
                    url=j.get("url") or f"https://remoteok.com/remote-jobs/{j.get('slug', j['id'])}",
                    apply_url=j.get("apply_url") or "",
                    location=fix_mojibake(clean(j.get("location"))) or "Remote",
                    remote=True,
                    description=fix_mojibake(html_to_text(j.get("description"))),
                    posted_at=parse_dt(j.get("epoch") or j.get("date")),
                    salary_min=to_int(j.get("salary_min")), salary_max=to_int(j.get("salary_max")),
                    salary_currency="USD" if j.get("salary_min") else "",
                    tags=list(j.get("tags") or []),
                )


class Arbeitnow(_Aggregator):
    name = "arbeitnow"

    async def _iter(self, ctx: FetchContext, c: AggregatorCfg):
        for page in range(1, c.max_pages + 1):
            data, _ = await ctx.http.get_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
            for j in data.get("data", []):
                yield RawJob(
                    external_id=str(j["slug"]),
                    company=clean(j.get("company_name")),
                    title=clean(j.get("title")),
                    url=j.get("url") or "",
                    location=clean(j.get("location")),
                    remote=bool(j.get("remote")) or None,
                    description=html_to_text(j.get("description")),
                    employment_type=clean(", ".join(j.get("job_types") or []), 80),
                    posted_at=parse_dt(j.get("created_at")),
                    tags=list(j.get("tags") or []),
                )
            if not (data.get("links") or {}).get("next"):
                break


class Himalayas(_Aggregator):
    name = "himalayas"
    PAGE = 20

    async def _iter(self, ctx: FetchContext, c: AggregatorCfg):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        for q in c.queries or [None]:
            for page in range(c.max_pages):
                if q:
                    url, params = "https://himalayas.app/jobs/api/search", {"q": q, "limit": self.PAGE, "offset": page * self.PAGE}
                else:
                    url, params = "https://himalayas.app/jobs/api", {"limit": self.PAGE, "offset": page * self.PAGE}
                data, _ = await ctx.http.get_json(url, params=params)
                jobs = data.get("jobs") or []
                for j in jobs:
                    expiry = parse_dt(j.get("expiryDate"))
                    if expiry and expiry < now:
                        continue  # expired posting: never ingest
                    yield self._job(j)
                if len(jobs) < self.PAGE:
                    break

    @staticmethod
    def _job(j: dict[str, Any]) -> RawJob:
        guid = j.get("guid") or j.get("applicationLink") or ""
        locs = j.get("locationRestrictions") or []
        smin, smax = to_int(j.get("minSalary")), to_int(j.get("maxSalary"))
        yearly = (j.get("salaryPeriod") or "annual").lower() in ("annual", "yearly", "year")
        return RawJob(
            external_id=guid.rstrip("/").rsplit("/", 1)[-1] if guid else "",
            company=clean(j.get("companyName")),
            title=clean(j.get("title")),
            url=guid,
            apply_url=j.get("applicationLink") or "",
            location="Remote" + (f" ({', '.join(locs)})" if locs else ""),
            remote=True,
            description=html_to_text(j.get("description") or j.get("excerpt")),
            employment_type=clean(j.get("employmentType"), 80),
            posted_at=parse_dt(j.get("pubDate")),
            salary_min=smin if yearly else None, salary_max=smax if yearly else None,
            salary_currency=clean(j.get("currency"), 10) if yearly and smin else "",
            tags=list(j.get("categories") or []),
        )
