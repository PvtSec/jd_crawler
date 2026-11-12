"""Company career boards on applicant-tracking systems (authoritative feeds).

Each board returns the company's complete live listing, so a job that is no
longer in the feed has been taken down. Board entries in config may be
"token" or "token=Display Name".
"""
from __future__ import annotations

import asyncio
import re
from collections import OrderedDict

from ..config import AppConfig
from ..http import FetchError, Http
from .base import (
    BoardResult, FetchContext, RawJob, Source, VerifyResult, clean, html_to_text, parse_dt, to_int,
)


def split_board(entry: str) -> tuple[str, str]:
    token, _, name = entry.partition("=")
    token = token.strip()
    return token, (name.strip() or token.replace("-", " ").replace("_", " ").title())


class _ATS(Source):
    authoritative = True
    config_field = ""

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        cls._names = {}  # board token -> company name from the registry

    def boards(self, cfg: AppConfig) -> list[str]:
        return [split_board(b)[0] for b in getattr(cfg.sources, self.config_field)]

    _names: dict[str, str] = {}

    def company_name(self, cfg: AppConfig, token: str) -> str:
        if self._names.get(token):
            return self._names[token]
        for b in getattr(cfg.sources, self.config_field):
            t, name = split_board(b)
            if t == token:
                return name
        return token

    async def fetch_board(self, ctx: FetchContext, board: str, name: str = "") -> BoardResult:
        self._names[board] = name or self._names.get(board, "")
        try:
            return await self._fetch(ctx, board)
        except FetchError as e:
            return self.result(board, ok=False, error=str(e))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            return self.result(board, ok=False, error=f"unexpected payload: {type(e).__name__}: {e}")

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        raise NotImplementedError


class Greenhouse(_ATS):
    name = "greenhouse"
    config_field = "greenhouse"
    API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        data, unchanged = await ctx.http.get_json(self.API.format(board=board), params={"content": "true"}, detect_unchanged=True)
        if unchanged:
            return self.result(board, unchanged=True)
        fallback = self.company_name(ctx.cfg, board)
        jobs, ids = [], set()
        for j in data.get("jobs", []):
            ext = str(j["id"])
            ids.add(ext)
            loc = clean((j.get("location") or {}).get("name"))
            depts = [d.get("name") for d in j.get("departments") or [] if d.get("name")]
            jobs.append(RawJob(
                external_id=ext,
                company=clean(j.get("company_name")) or fallback,
                title=clean(j.get("title")),
                url=j.get("absolute_url") or "",
                location=loc,
                description=html_to_text(j.get("content"), escaped=True),
                department=clean(", ".join(depts), 200),
                posted_at=parse_dt(j.get("first_published") or j.get("updated_at")),
            ))
        return self.result(board, jobs=jobs, present_ids=ids)

    async def verify(self, http: Http, board: str, external_id: str, url: str) -> VerifyResult | None:
        status = await http.status_of(f"{self.API.format(board=board)}/{external_id}")
        if status == 200:
            return VerifyResult(True, "listed on Greenhouse board API")
        if status in (404, 410):
            return VerifyResult(False, f"Greenhouse API returned {status}")
        return VerifyResult(None, f"Greenhouse API returned {status}")


class Lever(_ATS):
    name = "lever"
    config_field = "lever"
    API = "https://api.lever.co/v0/postings/{board}"

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        data, unchanged = await ctx.http.get_json(self.API.format(board=board), params={"mode": "json"}, detect_unchanged=True)
        if unchanged:
            return self.result(board, unchanged=True)
        company = self.company_name(ctx.cfg, board)
        jobs, ids = [], set()
        for j in data:
            ext = str(j["id"])
            ids.add(ext)
            cat = j.get("categories") or {}
            locs = cat.get("allLocations") or ([cat["location"]] if cat.get("location") else [])
            parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
            for lst in j.get("lists") or []:
                parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
            parts.append(j.get("additionalPlain") or "")
            sal = j.get("salaryRange") or {}
            jobs.append(RawJob(
                external_id=ext,
                company=company,
                title=clean(j.get("text")),
                url=j.get("hostedUrl") or "",
                apply_url=j.get("applyUrl") or "",
                location=clean(" / ".join(locs)),
                remote=(j.get("workplaceType") == "remote") or None,
                description="\n\n".join(p for p in parts if p).strip(),
                department=clean(" / ".join(x for x in (cat.get("department"), cat.get("team")) if x), 200),
                employment_type=clean(cat.get("commitment"), 80),
                posted_at=parse_dt(j.get("createdAt")),
                salary_min=to_int(sal.get("min")), salary_max=to_int(sal.get("max")),
                salary_currency=clean(sal.get("currency"), 10),
            ))
        return self.result(board, jobs=jobs, present_ids=ids)

    async def verify(self, http: Http, board: str, external_id: str, url: str) -> VerifyResult | None:
        status = await http.status_of(f"{self.API.format(board=board)}/{external_id}")
        if status == 200:
            return VerifyResult(True, "listed on Lever postings API")
        if status in (404, 410):
            return VerifyResult(False, f"Lever API returned {status}")
        return VerifyResult(None, f"Lever API returned {status}")


class Ashby(_ATS):
    name = "ashby"
    config_field = "ashby"
    API = "https://api.ashbyhq.com/posting-api/job-board/{board}"

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        data, unchanged = await ctx.http.get_json(self.API.format(board=board), params={"includeCompensation": "true"}, detect_unchanged=True)
        if unchanged:
            return self.result(board, unchanged=True)
        company = self.company_name(ctx.cfg, board)
        jobs, ids = [], set()
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            ext = str(j["id"])
            ids.add(ext)
            locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
            comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
            desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
            if comp:
                desc = f"{desc}\n\nCompensation: {comp}"
            jobs.append(RawJob(
                external_id=ext,
                company=company,
                title=clean(j.get("title")),
                url=j.get("jobUrl") or "",
                apply_url=j.get("applyUrl") or "",
                location=clean(" / ".join(l for l in locs if l)),
                remote=bool(j.get("isRemote")) or (j.get("workplaceType") == "Remote") or None,
                description=desc,
                department=clean(" / ".join(x for x in (j.get("department"), j.get("team")) if x), 200),
                employment_type=clean(j.get("employmentType"), 80),
                posted_at=parse_dt(j.get("publishedAt")),
            ))
        return self.result(board, jobs=jobs, present_ids=ids)

    async def verify(self, http: Http, board: str, external_id: str, url: str) -> VerifyResult | None:
        data, _ = await http.get_json(self.API.format(board=board))
        if any(str(j.get("id")) == external_id for j in data.get("jobs", [])):
            return VerifyResult(True, "listed on Ashby job board API")
        return VerifyResult(False, "not on Ashby job board anymore")


class SmartRecruiters(_ATS):
    name = "smartrecruiters"
    config_field = "smartrecruiters"
    API = "https://api.smartrecruiters.com/v1/companies/{board}/postings"
    PAGE = 100
    MAX_DETAILS_PER_RUN = 60

    def __init__(self) -> None:
        # The listing endpoint has no descriptions; details are fetched only for
        # jobs that pass the title prefilter and cached across runs.
        self._details: OrderedDict[str, tuple[str, str, str]] = OrderedDict()

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        url = self.API.format(board=board)
        postings, offset = [], 0
        while True:
            data, _ = await ctx.http.get_json(url, params={"limit": self.PAGE, "offset": offset})
            content = data.get("content") or []
            postings.extend(content)
            offset += len(content)
            if not content or offset >= int(data.get("totalFound") or 0) or offset >= 5000:
                break
        ids = {str(p["id"]) for p in postings}
        from ..trust import categorize

        wanted = [] if ctx.light else [p for p in postings
                  if str(p["id"]) not in self._details and ctx.title_ok(clean(p.get("name")))
                  and categorize(clean(p.get("name")))[1]]
        sem = asyncio.Semaphore(4)

        async def detail(p: dict) -> None:
            async with sem:
                try:
                    d, _ = await ctx.http.get_json(f"{url}/{p['id']}")
                except FetchError:
                    return
                sections = (d.get("jobAd") or {}).get("sections") or {}
                text = "\n\n".join(
                    f"{s.get('title', '')}\n{html_to_text(s.get('text'))}"
                    for k in ("jobDescription", "qualifications", "additionalInformation")
                    if (s := sections.get(k))
                )
                self._details[str(p["id"])] = (text, d.get("postingUrl") or "", d.get("applyUrl") or "")
                while len(self._details) > 20000:
                    self._details.popitem(last=False)

        await asyncio.gather(*(detail(p) for p in wanted[: self.MAX_DETAILS_PER_RUN]))
        jobs = []
        for p in postings:
            ext = str(p["id"])
            loc = p.get("location") or {}
            desc, posting_url, apply_url = self._details.get(ext, ("", "", ""))
            jobs.append(RawJob(
                external_id=ext,
                company=clean((p.get("company") or {}).get("name")) or self.company_name(ctx.cfg, board),
                title=clean(p.get("name")),
                url=posting_url or f"https://jobs.smartrecruiters.com/{board}/{ext}",
                apply_url=apply_url,
                location=clean(loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)),
                remote=bool(loc.get("remote")) or None,
                description=desc,
                department=clean((p.get("department") or {}).get("label") or (p.get("function") or {}).get("label"), 200),
                employment_type=clean((p.get("typeOfEmployment") or {}).get("label"), 80),
                posted_at=parse_dt(p.get("releasedDate")),
            ))
        return self.result(board, jobs=jobs, present_ids=ids)

    async def verify(self, http: Http, board: str, external_id: str, url: str) -> VerifyResult | None:
        status = await http.status_of(f"{self.API.format(board=board)}/{external_id}")
        if status == 200:
            return VerifyResult(True, "listed on SmartRecruiters API")
        if status in (404, 410):
            return VerifyResult(False, f"SmartRecruiters API returned {status}")
        return VerifyResult(None, f"SmartRecruiters API returned {status}")


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class Workable(_ATS):
    """Workable boards: either a company id (from Workable's public job search,
    polled via jobs.workable.com) or an account slug (apply.workable.com/<slug>)."""

    name = "workable"
    config_field = "workable"
    SEARCH_API = "https://jobs.workable.com/api/v1/companies/{board}"
    WIDGET_API = "https://apply.workable.com/api/v1/widget/accounts/{board}"

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        if _UUID.match(board):
            return await self._fetch_by_id(ctx, board)
        return await self._fetch_by_slug(ctx, board)

    async def _fetch_by_id(self, ctx: FetchContext, board: str) -> BoardResult:
        url = self.SEARCH_API.format(board=board)
        jobs, ids, token, company = [], set(), None, self.company_name(ctx.cfg, board)
        for _ in range(100):
            data, _ = await ctx.http.get_json(url, params={"pageToken": token} if token else None)
            company = clean(data.get("title")) or company
            for j in data.get("jobs") or []:
                ext = str(j["id"])
                if ext in ids:
                    continue
                ids.add(ext)
                loc = j.get("location") or {}
                parts = [html_to_text(j.get(k)) for k in ("description", "requirementsSection", "benefitsSection")]
                jobs.append(RawJob(
                    external_id=ext,
                    company=company,
                    title=clean(j.get("title")),
                    url=j.get("url") or "",
                    location=clean(", ".join(x for x in (loc.get("city"), loc.get("subregion"), loc.get("countryName")) if x)),
                    remote=(j.get("workplace") == "remote") or None,
                    description="\n\n".join(p for p in parts if p),
                    department=clean(j.get("department"), 200),
                    employment_type=clean(j.get("employmentType"), 80),
                    posted_at=parse_dt(j.get("created")),
                ))
            token = data.get("nextPageToken")
            if not token or not data.get("jobs"):
                break
        return self.result(board, jobs=jobs, present_ids=ids)

    async def _fetch_by_slug(self, ctx: FetchContext, board: str) -> BoardResult:
        data, unchanged = await ctx.http.get_json(self.WIDGET_API.format(board=board), params={"details": "true"}, detect_unchanged=True)
        if unchanged:
            return self.result(board, unchanged=True)
        company = clean(data.get("name")) or self.company_name(ctx.cfg, board)
        jobs, ids = [], set()
        for j in data.get("jobs") or []:
            ext = str(j.get("shortcode") or j.get("id") or "")
            if not ext:
                continue
            ids.add(ext)
            jobs.append(RawJob(
                external_id=ext,
                company=company,
                title=clean(j.get("title")),
                url=j.get("url") or j.get("shortlink") or "",
                apply_url=j.get("application_url") or "",
                location=clean(", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)),
                remote=str(j.get("telecommuting")).lower() == "true" or None,
                description=html_to_text(j.get("description")),
                department=clean(j.get("department"), 200),
                employment_type=clean(j.get("employment_type"), 80),
                posted_at=parse_dt(j.get("published_on") or j.get("created_at")),
            ))
        return self.result(board, jobs=jobs, present_ids=ids)

    async def verify(self, http: Http, board: str, external_id: str, url: str) -> VerifyResult | None:
        from .base import FetchContext as _Ctx
        from ..config import config_store

        res = await self.fetch_board(_Ctx(cfg=config_store.current, http=http, title_ok=lambda t: True), board)
        if not res.ok:
            return VerifyResult(None, f"Workable check failed: {res.error}"[:200])
        if res.unchanged:
            return VerifyResult(None, "Workable board unchanged since last fetch")
        alive = external_id in (res.present_ids or set())
        return VerifyResult(alive, "listed on Workable board" if alive else "not on Workable board anymore")


class Recruitee(_ATS):
    name = "recruitee"
    config_field = "recruitee"
    API = "https://{board}.recruitee.com/api/offers/"

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        data, unchanged = await ctx.http.get_json(self.API.format(board=board), detect_unchanged=True)
        if unchanged:
            return self.result(board, unchanged=True)
        fallback = self.company_name(ctx.cfg, board)
        jobs, ids = [], set()
        for j in data.get("offers") or []:
            ext = str(j.get("id") or "")
            if not ext:
                continue
            ids.add(ext)
            desc = "\n\n".join(x for x in (html_to_text(j.get("description")), html_to_text(j.get("requirements"))) if x)
            jobs.append(RawJob(
                external_id=ext,
                company=clean(j.get("company_name")) or fallback,
                title=clean(j.get("title")),
                url=j.get("careers_url") or f"https://{board}.recruitee.com/o/{j.get('slug', ext)}",
                apply_url=j.get("careers_apply_url") or "",
                location=clean(j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x)),
                remote=bool(j.get("remote")) or None,
                description=desc,
                department=clean(j.get("department"), 200),
                employment_type=clean(j.get("employment_type_code"), 80).replace("_", " "),
                posted_at=parse_dt(j.get("published_at") or j.get("created_at")),
                salary_min=to_int((j.get("salary") or {}).get("min")), salary_max=to_int((j.get("salary") or {}).get("max")),
                salary_currency=clean((j.get("salary") or {}).get("currency"), 10),
            ))
        return self.result(board, jobs=jobs, present_ids=ids)


class Personio(_ATS):
    name = "personio"
    config_field = "personio"
    API = "https://{board}.jobs.personio.de/xml"

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        import xml.etree.ElementTree as ET

        url = self.API.format(board=board)
        resp = await ctx.http._request("GET", url, params={"language": "en"}, headers={"Accept": "application/xml"})
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} for {url}", resp.status_code)
        if len(resp.content) > 30_000_000:
            raise FetchError("Personio feed too large")
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise FetchError(f"invalid XML from {url}: {e}") from e
        fallback = self.company_name(ctx.cfg, board)
        jobs, ids = [], set()
        txt = lambda el, tag: (el.findtext(tag) or "").strip()  # noqa: E731
        for p in root.iter("position"):
            ext = txt(p, "id")
            if not ext:
                continue
            ids.add(ext)
            parts = []
            for d in p.iter("jobDescription"):
                parts.append(f"{txt(d, 'name')}\n{html_to_text(txt(d, 'value'))}")
            offices = [o.text.strip() for o in p.iter("office") if o.text and o.text.strip()]
            jobs.append(RawJob(
                external_id=ext,
                company=txt(p, "subcompany") or fallback,
                title=clean(txt(p, "name")),
                url=f"https://{board}.jobs.personio.de/job/{ext}",
                apply_url=f"https://{board}.jobs.personio.de/job/{ext}#apply",
                location=clean(", ".join(dict.fromkeys(offices))),
                description="\n\n".join(parts).strip(),
                department=clean(txt(p, "department"), 200),
                employment_type=clean(f"{txt(p, 'employmentType')} {txt(p, 'schedule')}".strip(), 80),
                posted_at=parse_dt(txt(p, "createdAt")),
            ))
        return self.result(board, jobs=jobs, present_ids=ids)


class Breezy(_ATS):
    name = "breezy"
    config_field = "breezy"
    API = "https://{board}.breezy.hr/json"

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        data, unchanged = await ctx.http.get_json(self.API.format(board=board), detect_unchanged=True)
        if unchanged:
            return self.result(board, unchanged=True)
        fallback = self.company_name(ctx.cfg, board)
        jobs, ids = [], set()
        for j in data if isinstance(data, list) else []:
            ext = str(j.get("id") or "")
            if not ext:
                continue
            ids.add(ext)
            loc = j.get("location") or {}
            sal = j.get("salary")
            jobs.append(RawJob(
                external_id=ext,
                company=clean((j.get("company") or {}).get("name")) or fallback,
                title=clean(j.get("name")),
                url=j.get("url") or f"https://{board}.breezy.hr/p/{j.get('friendly_id', ext)}",
                location=clean(loc.get("name") if isinstance(loc, dict) else loc),
                remote=bool(isinstance(loc, dict) and loc.get("is_remote")) or None,
                # The list endpoint has no descriptions; title/department still drive matching.
                description=f"Salary: {sal}" if isinstance(sal, str) and sal else "",
                department=clean(j.get("department"), 200),
                employment_type=clean((j.get("type") or {}).get("name") if isinstance(j.get("type"), dict) else j.get("type"), 80),
                posted_at=parse_dt(j.get("published_date")),
            ))
        return self.result(board, jobs=jobs, present_ids=ids)


class _DetailedATS(_ATS):
    """Boards whose listing lacks descriptions. Details are fetched only for tech jobs that
    pass the title prefilter, capped per run, and reused from the database afterwards."""

    MAX_DETAILS_PER_RUN = 60

    def __init__(self) -> None:
        self._details: dict[str, dict[str, dict]] = {}
        self._sigs: dict[str, str] = {}

    async def _with_details(self, ctx: FetchContext, board: str, titles: dict[str, str], fetch_one) -> dict | None:
        """Fill and return the board's detail cache (external id -> fields), or None when the
        listing is unchanged since the last complete run. `fetch_one(ext)` returns a fields
        dict (or None on failure)."""
        import hashlib

        from ..trust import categorize
        from .base import stored_details

        cache = self._details.get(board)
        if cache is None and not ctx.light:
            cache = self._details[board] = await stored_details(self.board_key(board))
        cache = cache if cache is not None else {}
        wanted = [] if ctx.light else [
            ext for ext, t in titles.items() if ext not in cache and ctx.title_ok(t) and categorize(t)[1]
        ]
        sig = hashlib.sha1("\n".join(sorted(f"{e}|{t}" for e, t in titles.items())).encode()).hexdigest()
        if not wanted and self._sigs.get(board) == sig:
            return None
        sem = asyncio.Semaphore(3)

        async def one(ext: str) -> None:
            async with sem:
                try:
                    d = await fetch_one(ext)
                except FetchError:
                    return
            if d:
                cache[ext] = d

        await asyncio.gather(*(one(e) for e in wanted[: self.MAX_DETAILS_PER_RUN]))
        if len(wanted) <= self.MAX_DETAILS_PER_RUN:
            self._sigs[board] = sig
        return cache


class BambooHR(_DetailedATS):
    """BambooHR careers sites: https://<token>.bamboohr.com/careers.

    The listing JSON has titles/locations only; descriptions come from per-job detail
    calls, made only for tech jobs passing the title prefilter, capped per run and
    reused from the database afterwards (same scheme as Workday). An unknown token
    redirects to www.bamboohr.com, which is reported as HTTP 404 so the board retires."""

    name = "bamboohr"
    config_field = "bamboohr"
    LOCATION_TYPES = {"1": True, "2": None}  # 0 on-site, 1 remote, 2 hybrid

    @staticmethod
    def base(board: str) -> str:
        return f"https://{board.lower()}.bamboohr.com"

    async def _get(self, ctx: FetchContext, url: str, board: str):
        import json
        from urllib.parse import urlsplit

        resp = await ctx.http._request("GET", url, headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} for {url}", resp.status_code)
        if (urlsplit(str(resp.url)).hostname or "").lower() != f"{board.lower()}.bamboohr.com" \
                or "json" not in resp.headers.get("content-type", ""):
            raise FetchError(f"HTTP 404 for {url} (no BambooHR careers site)", 404)
        try:
            return json.loads(resp.content)
        except ValueError as e:
            raise FetchError(f"invalid JSON from {url}: {e}") from e

    @staticmethod
    def _location(j: dict) -> str:
        ats = j.get("atsLocation") or {}
        loc = j.get("location") or {}
        parts = [ats.get("city") or loc.get("city"), ats.get("state") or ats.get("province") or loc.get("state"),
                 ats.get("country") or loc.get("addressCountry")]
        return clean(", ".join(dict.fromkeys(p for p in parts if p)))

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        base = self.base(board)
        data = await self._get(ctx, f"{base}/careers/list", board)
        by_id = {str(j.get("id")): j for j in (data.get("result") or []) if j.get("id")}
        if not by_id:
            return self.result(board, jobs=[], present_ids=set())

        async def detail(ext: str) -> dict:
            d = await self._get(ctx, f"{base}/careers/{ext}/detail", board)
            jo = (d.get("result") or {}).get("jobOpening") or {}
            comp = jo.get("compensation")
            desc = html_to_text(jo.get("description"))
            if isinstance(comp, str) and comp.strip():
                desc = f"{desc}\n\nCompensation: {comp.strip()}"
            return {
                "description": desc or "-",
                "location": self._location(jo),
                "remote": self.LOCATION_TYPES.get(str(jo.get("locationType"))),
                "employment_type": clean(jo.get("employmentStatusLabel"), 80),
                "posted_at": parse_dt(jo.get("datePosted")),
                "url": jo.get("jobOpeningShareUrl") or f"{base}/careers/{ext}",
                "apply_url": f"{base}/careers/{ext}",
            }

        cache = await self._with_details(ctx, board, {e: clean(j.get("jobOpeningName")) for e, j in by_id.items()}, detail)
        if cache is None:
            return self.result(board, unchanged=True)
        company = self.company_name(ctx.cfg, board)
        jobs = []
        for ext, j in by_id.items():
            d = cache.get(ext) or {}
            lt = str(j.get("locationType"))
            jobs.append(RawJob(
                external_id=ext,
                company=company,
                title=clean(j.get("jobOpeningName")),
                url=d.get("url") or f"{base}/careers/{ext}",
                apply_url=d.get("apply_url") or f"{base}/careers/{ext}",
                location=d.get("location") or self._location(j) or ("Remote" if lt == "1" else ""),
                remote=d.get("remote") if d else (True if j.get("isRemote") or lt == "1" else None),
                description="" if d.get("description") == "-" else d.get("description", ""),
                department=clean(j.get("departmentLabel"), 200),
                employment_type=d.get("employment_type") or clean(j.get("employmentStatusLabel"), 80),
                posted_at=d.get("posted_at"),
            ))
        return self.result(board, jobs=jobs, present_ids=set(by_id))


class Teamtailor(_ATS):
    """Teamtailor career sites via their public RSS feed: https://<token>.teamtailor.com/jobs.rss.

    The feed has full descriptions. It returns 100 items by default; `per_page`/`offset`
    page through larger listings. Links may point at the company's custom domain."""

    name = "teamtailor"
    config_field = "teamtailor"
    PAGE = 500
    MAX_JOBS = 5000
    NS = "{https://teamtailor.com/locations}"
    REMOTE = {"fully": True, "remote": True, "hybrid": None, "temporary": None, "none": None}

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime

        url = f"https://{board.lower()}.teamtailor.com/jobs.rss"
        items: list = []
        title = ""
        offset = 0
        while offset < self.MAX_JOBS:
            resp = await ctx.http._request("GET", url, params={"per_page": self.PAGE, "offset": offset},
                                           headers={"Accept": "application/rss+xml, application/xml"})
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code} for {url}", resp.status_code)
            try:
                root = ET.fromstring(resp.content)
            except ET.ParseError as e:
                raise FetchError(f"invalid RSS from {url}: {e}") from e
            chan = root.find("channel")
            page = chan.findall("item") if chan is not None else []
            title = title or (chan.findtext("title") if chan is not None else "") or ""
            items.extend(page)
            if len(page) < 100:  # the feed may cap pages below PAGE; a short page is the last one
                break
            offset += len(page)
        complete = offset < self.MAX_JOBS

        fallback = self.company_name(ctx.cfg, board)
        slug = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # noqa: E731
        if title.strip() and slug(fallback) == slug(board):  # registry only knows the slug: use the feed's title
            fallback = clean(title)
        jobs, ids = [], set()
        for it in items:
            ext = (it.findtext("guid") or "").strip()
            if not ext or ext in ids:
                continue
            ids.add(ext)
            locs = []
            for loc in it.iter(f"{self.NS}location"):
                name = (loc.findtext(f"{self.NS}name") or "").strip()
                city = (loc.findtext(f"{self.NS}city") or "").strip()
                country = (loc.findtext(f"{self.NS}country") or "").strip()
                locs.append(", ".join(dict.fromkeys(x for x in (city or name, country) if x)))
            remote = self.REMOTE.get((it.findtext("remoteStatus") or "").strip().lower())
            location = " / ".join(dict.fromkeys(l for l in locs if l))
            try:
                posted = parsedate_to_datetime(it.findtext("pubDate") or "")
            except (TypeError, ValueError):
                posted = None
            link = (it.findtext("link") or "").strip()
            jobs.append(RawJob(
                external_id=ext,
                company=fallback,
                title=clean(it.findtext("title")),
                url=link,
                apply_url=link,
                location=clean(location) or ("Remote" if remote else ""),
                remote=remote,
                description=html_to_text(it.findtext("description")),
                department=clean(it.findtext(f"{self.NS}department"), 200),
                posted_at=posted,
            ))
        return self.result(board, jobs=jobs, present_ids=ids if complete else None)


class Rippling(_DetailedATS):
    """Rippling Recruiting boards: https://ats.rippling.com/<slug>/jobs.

    The public board API lists titles, departments and locations (one entry per job, or
    per job location); descriptions come from per-job detail calls, as for BambooHR."""

    name = "rippling"
    config_field = "rippling"
    API = "https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs"

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        api = self.API.format(board=board)
        data, _ = await ctx.http.get_json(api)
        by_id: dict[str, dict] = {}
        locs: dict[str, list[str]] = {}
        for j in data if isinstance(data, list) else []:
            ext = str(j.get("uuid") or "")
            if not ext:
                continue
            by_id.setdefault(ext, j)
            label = clean((j.get("workLocation") or {}).get("label"))
            if label:
                locs.setdefault(ext, []).append(label)

        async def detail(ext: str) -> dict:
            d, _ = await ctx.http.get_json(f"{api}/{ext}")
            desc = d.get("description") or {}
            text = "\n\n".join(t for t in (html_to_text(desc.get("role")), html_to_text(desc.get("company"))) if t)
            where = [clean(w) for w in d.get("workLocations") or [] if isinstance(w, str)]
            return {
                "description": text or "-",
                "location": " / ".join(dict.fromkeys(where)),
                "remote": True if any("remote" in w.lower() for w in where) else None,
                "employment_type": clean((d.get("employmentType") or {}).get("id"), 80),
                "posted_at": parse_dt(d.get("createdOn")),
                "url": d.get("url") or "",
                "apply_url": d.get("url") or "",
                "company": clean(d.get("companyName")),
            }

        cache = await self._with_details(ctx, board, {e: clean(j.get("name")) for e, j in by_id.items()}, detail)
        if cache is None:
            return self.result(board, unchanged=True)
        company = next((d["company"] for d in cache.values() if d.get("company")), "") \
            or self.company_name(ctx.cfg, board)
        jobs = []
        for ext, j in by_id.items():
            d = cache.get(ext) or {}
            location = d.get("location") or " / ".join(dict.fromkeys(locs.get(ext, [])))
            url = d.get("url") or j.get("url") or f"https://ats.rippling.com/{board}/jobs/{ext}"
            jobs.append(RawJob(
                external_id=ext,
                company=company,
                title=clean(j.get("name")),
                url=url,
                apply_url=d.get("apply_url") or url,
                location=location,
                remote=d.get("remote") if d else (True if "remote" in location.lower() else None),
                description="" if d.get("description") == "-" else d.get("description", ""),
                department=clean((j.get("department") or {}).get("label"), 200),
                employment_type=d.get("employment_type", ""),
                posted_at=d.get("posted_at"),
            ))
        return self.result(board, jobs=jobs, present_ids=set(by_id))


class Pinpoint(_ATS):
    """Pinpoint career sites: https://<token>.pinpointhq.com. The public postings.json feed
    has every live posting with full descriptions."""

    name = "pinpoint"
    config_field = "pinpoint"
    REMOTE = {"remote": True, "hybrid": None, "onsite": False}
    SECTIONS = (("description", ""), ("key_responsibilities", "key_responsibilities_header"),
                ("skills_knowledge_expertise", "skills_knowledge_expertise_header"), ("benefits", "benefits_header"))

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        data, unchanged = await ctx.http.get_json(f"https://{board.lower()}.pinpointhq.com/postings.json",
                                                  detect_unchanged=True)
        if unchanged:
            return self.result(board, unchanged=True)
        company = self.company_name(ctx.cfg, board)
        jobs, ids = [], set()
        for j in data.get("data") or []:
            ext = str(j.get("id") or "")
            if not ext:
                continue
            ids.add(ext)
            parts = []
            for key, header in self.SECTIONS:
                body = html_to_text(j.get(key))
                if body:
                    parts.append(f"{j.get(header) or ''}\n{body}".strip() if header else body)
            loc = j.get("location") or {}
            location = ", ".join(dict.fromkeys(x for x in (loc.get("city") or loc.get("name"), loc.get("province")) if x))
            wt = (j.get("workplace_type") or "").lower()
            remote = self.REMOTE.get(wt)
            smin = smax = None
            cur = ""
            if j.get("compensation_visible") and re.search(r"year|annual", str(j.get("compensation_frequency") or ""), re.I):
                smin, smax = to_int(j.get("compensation_minimum")), to_int(j.get("compensation_maximum"))
                cur = clean(j.get("compensation_currency"), 10).upper()
            elif j.get("compensation_visible") and isinstance(j.get("compensation"), str) and j["compensation"].strip():
                parts.append(f"Compensation: {j['compensation'].strip()}")
            dept = ((j.get("job") or {}).get("department") or {}).get("name")
            jobs.append(RawJob(
                external_id=ext,
                company=company,
                title=clean(j.get("title")),
                url=j.get("url") or f"https://{board.lower()}.pinpointhq.com{j.get('path') or ''}",
                location=clean(location) or ("Remote" if remote else ""),
                remote=remote,
                description="\n\n".join(parts),
                department=clean(dept, 200),
                employment_type=clean(j.get("employment_type_text"), 80),
                salary_min=smin,
                salary_max=smax,
                salary_currency=cur,
            ))
        return self.result(board, jobs=jobs, present_ids=ids)


_JAZZ_LINK = re.compile(r"""<a href=["'](https?://[^"']+/apply/([A-Za-z0-9]+)/[^"']*)["'][^>]*>\s*(.*?)\s*</a>""", re.S)
_JAZZ_LOC = re.compile(r"""fa-map-marker['"]></i>\s*([^<]*)""")
_JAZZ_ATTR = re.compile(r"""<div[^>]*title=["'](Location|Type|Department)["'][^>]*>\s*<i[^>]*></i>\s*([^<]*)""")
_JAZZ_TITLE = re.compile(r"<title>\s*(.*?)\s*</title>", re.S | re.I)


class JazzHR(_DetailedATS):
    """JazzHR job boards: https://<token>.applytojob.com/apply.

    There is no public JSON feed: the board page lists every open job (title, location) and
    each job page has the description, read for tech jobs only as for BambooHR. Unknown
    tokens redirect to jazzhr.com, which is reported as HTTP 404 so the board retires."""

    name = "jazzhr"
    config_field = "jazzhr"

    @staticmethod
    def base(board: str) -> str:
        return f"https://{board.lower()}.applytojob.com"

    async def _page(self, ctx: FetchContext, url: str, board: str) -> str:
        from urllib.parse import urlsplit

        resp = await ctx.http._request("GET", url, headers={"Accept": "text/html"})
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} for {url}", resp.status_code)
        if (urlsplit(str(resp.url)).hostname or "").lower() != f"{board.lower()}.applytojob.com":
            raise FetchError(f"HTTP 404 for {url} (no JazzHR board)", 404)
        return resp.text

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        import html as htmllib

        base = self.base(board)
        page = await self._page(ctx, f"{base}/apply", board)
        if "resumator" not in page and "list-group-item" not in page:
            raise FetchError(f"unexpected page for {base}/apply")
        items: dict[str, dict] = {}
        for chunk in page.split('<li class="list-group-item">')[1:]:
            m = _JAZZ_LINK.search(chunk)
            if not m:
                continue
            loc = _JAZZ_LOC.search(chunk)
            items.setdefault(m.group(2), {
                "url": m.group(1),
                "title": clean(htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group(3)))),
                "location": clean(htmllib.unescape(loc.group(1))) if loc else "",
            })

        async def detail(ext: str) -> dict:
            h = await self._page(ctx, items[ext]["url"], board)
            attrs = {k: clean(htmllib.unescape(v)) for k, v in _JAZZ_ATTR.findall(h)}
            i = h.find('id="job-description"')
            desc = ""
            if i >= 0:
                i = h.find(">", i) + 1
                ends = [e for e in (h.find("resumator-mobile-apply", i), h.find("job-application-form", i)) if e > 0]
                desc = html_to_text(h[i:min(ends) if ends else len(h)].rsplit("<div", 1)[0])
            return {
                "description": desc or "-",
                "location": attrs.get("Location", ""),
                "remote": True if "remote" in (attrs.get("Location", "") + " " + items[ext]["title"]).lower() else None,
                "employment_type": attrs.get("Type", "")[:80],
                "department": attrs.get("Department", ""),
            }

        cache = await self._with_details(ctx, board, {e: it["title"] for e, it in items.items()}, detail)
        if cache is None:
            return self.result(board, unchanged=True)
        company = self.company_name(ctx.cfg, board)
        slug = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # noqa: E731
        if slug(company) == slug(board) and (t := _JAZZ_TITLE.search(page)):
            # "Jobs at 10Pearls" / "10Pearls - Career Page"
            name = re.sub(r"^\s*(?:careers?|jobs)\s+at\s+|\s*[-|]\s*(?:careers?(?: page)?|jobs|job board)\s*$", "",
                          htmllib.unescape(t.group(1)), flags=re.I)
            company = clean(name) or company
        jobs = []
        for ext, it in items.items():
            d = cache.get(ext) or {}
            location = d.get("location") or it["location"]
            jobs.append(RawJob(
                external_id=ext,
                company=company,
                title=it["title"],
                url=it["url"],
                apply_url=it["url"],
                location=location,
                remote=d.get("remote") if d else (True if "remote" in f"{location} {it['title']}".lower() else None),
                description="" if d.get("description") == "-" else d.get("description", ""),
                department=clean(d.get("department"), 200),
                employment_type=d.get("employment_type", ""),
            ))
        return self.result(board, jobs=jobs, present_ids=set(items))


WORKDAY_LINK = re.compile(
    r"//(?:([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?|(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([a-z0-9-]+)/)"
    r"([A-Za-z0-9_-]+)", re.I)


def workday_board(url: str) -> str | None:
    """'https://acme.wd5.myworkdayjobs.com/en-US/External/job/...' -> 'acme.wd5/External'."""
    m = WORKDAY_LINK.search(url or "")
    if not m or m.group(5).lower() in {"wday", "job", "details", "login", "userhome"}:
        return None
    tenant, wd = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
    return f"{tenant.lower()}.{wd.lower()}/{m.group(5)}"


_WD_TOKEN = re.compile(r"^([a-z0-9-]+)\.(wd\d+)/([A-Za-z0-9_-]+)$", re.I)
_WD_POSTED = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


class Workday(_ATS):
    """Workday career sites via the public CXS JSON API that the site itself uses.

    Board token: "<tenant>.<wdN>/<site>" (from https://<tenant>.<wdN>.myworkdayjobs.com/<site>).
    The listing has titles/locations only; descriptions come from per-job detail
    calls, made only for tech jobs passing the title prefilter, capped per run and
    reused from the database afterwards."""

    name = "workday"
    config_field = "workday"
    PAGE = 20                 # the API rejects larger pages
    MAX_JOBS = 6000           # very large tenants: list at most this many (liveness then via page checks)
    MAX_DETAILS_PER_RUN = 120
    HOSTS = ("wd1", "wd5", "wd3", "wd12", "wd2", "wd10", "wd103", "wd101", "wd105", "wd108", "wd4", "wd6")
    SITE_GUESSES = ("External", "Careers", "{t}", "{t}Careers", "{t}_Careers", "{t}ExternalCareerSite",
                    "External_Careers", "ExternalCareers", "{t}External", "External_Career_Site", "jobs",
                    "{t}_External", "Search", "en-US")

    def __init__(self) -> None:
        self._details: dict[str, dict[str, dict]] = {}   # board -> external_id -> detail fields
        self._sigs: dict[str, str] = {}                  # board -> hash of the listing

    @staticmethod
    def parts(board: str) -> tuple[str, str, str]:
        m = _WD_TOKEN.match(board)
        if not m:
            raise ValueError(f"bad Workday board token {board!r}")
        return m.group(1).lower(), m.group(2).lower(), m.group(3)

    def urls(self, board: str) -> tuple[str, str]:
        tenant, wd, site = self.parts(board)
        base = f"https://{tenant}.{wd}.myworkdayjobs.com"
        return f"{base}/wday/cxs/{tenant}/{site}", f"{base}/en-US/{site}"

    @staticmethod
    def _posted(text: str):
        from datetime import datetime, timedelta, timezone

        m = _WD_POSTED.search(text or "")
        if not m or "+" in m.group(0):
            return None  # "30+ days ago" is too vague
        word = m.group(1).lower()
        days = 0 if word == "today" else 1 if word == "yesterday" else int(m.group(2))
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return today - timedelta(days=days)

    async def list_postings(self, http: Http, api: str, limit: int | None = None) -> tuple[list[dict], bool]:
        """Returns (postings, complete). `total` stops counting at 2000 but paging
        continues, so pages are read (3 at a time) until a short page."""
        limit = limit or self.MAX_JOBS
        body = {"appliedFacets": {}, "limit": self.PAGE, "offset": 0, "searchText": ""}
        first = await http.post_json(f"{api}/jobs", body)
        postings = list(first.get("jobPostings") or [])
        total = int(first.get("total") or 0)
        if len(postings) < self.PAGE:
            return postings, True
        offset = self.PAGE
        while offset < limit:
            # When total < 2000 it is exact; beyond that probe ahead in waves.
            stop = min(total, limit) if total < 2000 else min(offset + 3 * self.PAGE, limit)
            offs = list(range(offset, stop, self.PAGE)) or [offset]
            pages = await asyncio.gather(*(http.post_json(f"{api}/jobs", {**body, "offset": o}) for o in offs))
            short = False
            for d in pages:
                chunk = d.get("jobPostings") or []
                postings.extend(chunk)
                short = short or len(chunk) < self.PAGE
            offset = offs[-1] + self.PAGE
            if short or (total < 2000 and offset >= total):
                return postings, True
        return postings, False

    async def _fetch(self, ctx: FetchContext, board: str) -> BoardResult:
        import hashlib

        from ..trust import categorize
        from .base import stored_details

        api, public = self.urls(board)
        postings, complete = await self.list_postings(ctx.http, api)
        if not postings:
            return self.result(board, jobs=[], present_ids=set())
        by_id: dict[str, dict] = {}
        for p in postings:
            path = p.get("externalPath") or ""
            if path.startswith("/job/"):
                by_id.setdefault(path.rsplit("/", 1)[-1], p)

        cache = self._details.get(board)
        if cache is None and not ctx.light:
            cache = self._details[board] = await stored_details(self.board_key(board))
        cache = cache if cache is not None else {}
        wanted = [] if ctx.light else [
            (ext, p) for ext, p in by_id.items()
            if ext not in cache and ctx.title_ok(clean(p.get("title"))) and categorize(clean(p.get("title")))[1]
        ]
        sig = hashlib.sha1("\n".join(sorted(f"{e}|{p.get('title')}" for e, p in by_id.items())).encode()).hexdigest()
        if not wanted and self._sigs.get(board) == sig:
            return self.result(board, unchanged=True)

        sem = asyncio.Semaphore(4)

        async def detail(ext: str, p: dict) -> None:
            async with sem:
                try:
                    d, _ = await ctx.http.get_json(f"{api}{p['externalPath']}")
                except FetchError:
                    return
            info = d.get("jobPostingInfo") or {}
            locs = [info.get("location")] + list(info.get("additionalLocations") or [])
            remote_type = str(info.get("remoteType") or "")
            cache[ext] = {
                "description": html_to_text(info.get("jobDescription")) or "-",
                "location": clean(" / ".join(dict.fromkeys(l for l in locs if l))),
                "remote": True if remote_type.lower() == "remote" else None,
                "employment_type": clean(info.get("timeType"), 80),
                "posted_at": parse_dt(info.get("startDate")),
                "url": f"{public}{p['externalPath']}",
                "apply_url": f"{public}{p['externalPath']}/apply",
            }

        await asyncio.gather(*(detail(e, p) for e, p in wanted[: self.MAX_DETAILS_PER_RUN]))
        if len(wanted) <= self.MAX_DETAILS_PER_RUN:
            self._sigs[board] = sig  # everything we want is cached: unchanged listing = nothing to do
        company = self.company_name(ctx.cfg, board)
        jobs = []
        for ext, p in by_id.items():
            d = cache.get(ext) or {}
            url = f"{public}{p['externalPath']}"
            jobs.append(RawJob(
                external_id=ext,
                company=company,
                title=clean(p.get("title")),
                url=d.get("url") or url,
                apply_url=d.get("apply_url") or f"{url}/apply",
                location=d.get("location") or clean(p.get("locationsText")),
                remote=d.get("remote"),
                description="" if d.get("description") == "-" else d.get("description", ""),
                employment_type=d.get("employment_type", ""),
                posted_at=d.get("posted_at") or self._posted(p.get("postedOn") or ""),
            ))
        # A truncated listing cannot prove a job is gone: skip close-out for it.
        return self.result(board, jobs=jobs, present_ids=set(by_id) if complete else None)

    async def verify(self, http: Http, board: str, external_id: str, url: str) -> VerifyResult | None:
        m = re.search(r"/job/.+$", url or "")
        if not m:
            return None
        api, _ = self.urls(board)
        status = await http.status_of(f"{api}{m.group(0).removesuffix('/apply')}")
        if status == 200:
            return VerifyResult(True, "listed on Workday career site")
        if status in (404, 410):
            return VerifyResult(False, f"Workday returned {status}")
        return VerifyResult(None, f"Workday returned {status}")

    async def find_boards(self, http: Http, tenant: str) -> list[str]:
        """Locate a tenant's career site(s): a wrong host/tenant gives HTTP 422,
        the right one with an unknown site gives 404, a real site gives 200."""
        tenant = tenant.lower()
        if not re.fullmatch(r"[a-z0-9-]{2,60}", tenant):
            return []
        body = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}

        async def status(wd: str, site: str) -> int:
            try:
                r = await http._request("POST", f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                                        json=body, headers={"Accept": "application/json"})
                return r.status_code
            except FetchError:
                return 0

        for wd in self.HOSTS:
            if await status(wd, "zz-no-such-site") != 404:
                continue
            found = []
            for guess in self.SITE_GUESSES:
                site = guess.format(t=tenant)
                if await status(wd, site) == 200:
                    found.append(f"{tenant}.{wd}/{site}")
                    break
            return found
        return []
