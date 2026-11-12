"""Company discovery: build a verified registry of real employers' career boards.

    docker compose run --rm backend python -m app.discover [--out /app/data/companies.discovered.csv]

Sources (all public, keyless):
  * SmartRecruiters global job search  -> company identifiers of every employer with live postings
  * Workable global job search         -> company ids of every employer with live postings
  * SimplifyJobs open listings (GitHub) -> Greenhouse / Lever / Ashby / Workable / SmartRecruiters slugs, Workday sites
  * Y Combinator company directory     -> candidate slugs, probed on Greenhouse / Lever / Ashby
Every Greenhouse / Lever / Ashby / Workable-slug board is verified live against the ATS API.
Recruitment agencies, staffing firms, job boards and talent marketplaces are excluded.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import logging
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import h2.exceptions
import httpx

from .sources.ats import workday_board
from .trust import is_middleman

log = logging.getLogger("discover")
UA = "JobHunt/1.0 (personal job tracker; company discovery)"

WORKABLE_LOCATIONS = [
    "", "United States", "United Kingdom", "Germany", "France", "Spain", "Italy", "Netherlands", "Belgium", "Ireland",
    "Portugal", "Greece", "Cyprus", "Poland", "Romania", "Bulgaria", "Czechia", "Hungary", "Austria", "Switzerland",
    "Sweden", "Denmark", "Norway", "Finland", "Estonia", "Lithuania", "Latvia", "Croatia", "Serbia", "Slovenia",
    "Canada", "Mexico", "Brazil", "Argentina", "Colombia", "Chile", "Peru", "Israel", "Turkey", "United Arab Emirates",
    "Saudi Arabia", "Qatar", "Egypt", "South Africa", "Nigeria", "Kenya", "India", "Pakistan", "Philippines",
    "Singapore", "Malaysia", "Indonesia", "Vietnam", "Thailand", "Japan", "South Korea", "Australia", "New Zealand",
    "Ukraine", "Georgia", "Armenia", "Morocco", "Remote",
]
WORKABLE_QUERIES = ["", "software", "engineer", "developer", "data", "devops", "security", "product", "it", "cloud",
                    "manager", "sales", "marketing", "analyst", "designer", "support", "operations", "finance"]
SR_EXTRA_KEYWORDS = [
    # places
    "london", "berlin", "munich", "hamburg", "paris", "lyon", "madrid", "barcelona", "lisbon", "porto", "milan", "rome",
    "amsterdam", "rotterdam", "brussels", "dublin", "warsaw", "krakow", "wroclaw", "prague", "budapest", "bucharest",
    "sofia", "athens", "vienna", "zurich", "geneva", "stockholm", "copenhagen", "oslo", "helsinki", "tallinn", "riga",
    "vilnius", "belgrade", "zagreb", "istanbul", "tel aviv", "dubai", "abu dhabi", "riyadh", "doha", "cairo",
    "johannesburg", "cape town", "lagos", "nairobi", "bangalore", "bengaluru", "hyderabad", "pune", "chennai", "mumbai",
    "delhi", "gurgaon", "noida", "karachi", "lahore", "manila", "singapore", "kuala lumpur", "jakarta", "bangkok",
    "ho chi minh", "hanoi", "tokyo", "osaka", "seoul", "shanghai", "beijing", "shenzhen", "hong kong", "taipei",
    "sydney", "melbourne", "brisbane", "auckland", "toronto", "vancouver", "montreal", "ottawa", "calgary", "new york",
    "san francisco", "seattle", "austin", "boston", "chicago", "denver", "atlanta", "dallas", "houston", "miami",
    "los angeles", "san diego", "phoenix", "minneapolis", "detroit", "philadelphia", "washington", "raleigh",
    "salt lake city", "portland", "nashville", "charlotte", "pittsburgh", "columbus", "mexico city", "guadalajara",
    "sao paulo", "rio de janeiro", "buenos aires", "santiago", "bogota", "medellin", "lima", "remote", "hybrid",
    # titles & functions
    "backend", "frontend", "full stack", "fullstack", "mobile", "ios", "android", "embedded", "firmware", "qa",
    "test", "automation", "sre", "platform", "infrastructure", "network", "systems", "administrator", "architect",
    "scientist", "machine learning", "ai", "analytics", "bi", "database", "dba", "erp", "sap", "salesforce", "crm",
    "servicenow", "workday", "oracle", "microsoft", "azure", "aws", "gcp", "linux", "windows", "cyber", "soc",
    "penetration", "compliance", "risk", "audit", "governance", "architecture", "solutions", "consulting",
    "technical support", "helpdesk", "service desk", "implementation", "integration", "product manager",
    "project manager", "program manager", "scrum", "agile", "ux", "ui", "graphic", "game", "unity", "unreal",
    "blockchain", "fintech", "payments", "banking", "insurance", "telecom", "automotive", "aerospace", "defense",
    "semiconductor", "electronics", "robotics", "energy", "renewable", "oil", "mining", "logistics", "supply chain",
    "retail", "ecommerce", "hospitality", "hotel", "restaurant", "healthcare", "pharma", "biotech", "medical device",
    "education", "government", "nonprofit", "media", "advertising", "publishing", "legal", "real estate",
    "construction", "manufacturing", "chemical", "food", "beverage", "fashion", "luxury", "sports", "travel",
    "airline", "shipping", "transport", "accounting", "tax", "payroll", "hr", "recruiting", "procurement",
    "buyer", "warehouse", "driver", "mechanic", "electrician", "welder", "chef", "cashier", "pharmacist", "nurse",
    "doctor", "therapist", "teacher", "lecturer", "researcher", "phd", "graduate", "trainee", "apprentice",
    "working student", "werkstudent", "praktikum", "stage", "alternance", "stagiaire", "becario", "estagio",
    "ingenieur", "entwickler", "informatik", "développeur", "ingénieur", "desarrollador", "ingeniero",
    "sviluppatore", "programmeur", "ontwikkelaar", "inżynier", "programista",
]
SR_KEYWORDS = ["", "engineer", "software", "developer", "data", "it", "security", "cloud", "analyst", "manager",
               "sales", "support", "technician", "consultant", "product", "design", "operations", "nurse",
               "finance", "marketing", "specialist", "intern", "senior", "lead", "associate", "director"]


@dataclass
class Company:
    ats: str
    token: str
    name: str = ""
    website: str = ""
    origin: set[str] = field(default_factory=set)
    jobs: int = 0
    description: str = ""


class Discovery:
    def __init__(self, concurrency: int, max_workable_pages: int, max_sr_offset: int,
                 tranco_limit: int = 0, checkpoint: Path | None = None):
        self.tranco_limit = tranco_limit
        self.tranco_start = 0
        self.tranco_next = 0
        self.sr_queries = 4000
        self.checkpoint = checkpoint
        self.client = httpx.AsyncClient(
            http2=True, timeout=httpx.Timeout(40, connect=10), follow_redirects=True,
            headers={"User-Agent": UA, "Accept": "application/json"},
            limits=httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency),
        )
        self.hosts: dict[str, asyncio.Semaphore] = {}
        self.per_host = max(2, concurrency // 3)
        self.companies: dict[tuple[str, str], Company] = {}
        self.rejected: Counter[str] = Counter()
        self.rejected_examples: dict[str, str] = {}
        self.max_workable_pages = max_workable_pages
        self.max_sr_offset = max_sr_offset
        self.requests = 0
        self.t0 = time.monotonic()
        # Polite pacing for the shared search APIs (they rate-limit aggressively).
        self.min_gap = {
            "jobs.workable.com": 0.4, "jobs.smartrecruiters.com": 0.35,
            # probing ATS APIs: ~12 requests/second per platform
            "boards-api.greenhouse.io": 0.08, "api.lever.co": 0.08, "api.ashbyhq.com": 0.08,
            "apply.workable.com": 0.1, "recruitee.com": 0.08, "jobs.personio.de": 0.08, "breezy.hr": 0.08,
            "bamboohr.com": 0.1, "teamtailor.com": 0.1,
            "api.rippling.com": 0.1, "pinpointhq.com": 0.1, "applytojob.com": 0.15,
            "api.smartrecruiters.com": 0.1, "myworkdayjobs.com": 0.1,
        }
        self._pace_locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self.blocked_hosts: set[str] = set()

    @staticmethod
    def platform(host: str) -> str:
        for suffix in ("recruitee.com", "jobs.personio.de", "breezy.hr", "myworkdayjobs.com", "bamboohr.com", "teamtailor.com",
                       "pinpointhq.com", "applytojob.com"):
            if host.endswith("." + suffix):
                return suffix
        return host

    async def get(self, url: str, method: str = "GET", **kw) -> httpx.Response | None:
        host = self.platform(urlsplit(url).hostname or "")
        sem = self.hosts.setdefault(host, asyncio.Semaphore(self.per_host))
        if host in self.blocked_hosts:
            return None
        gap = self.min_gap.get(host, 0)
        for attempt in range(6):
            try:
                if gap:
                    lock = self._pace_locks.setdefault(host, asyncio.Lock())
                    async with lock:
                        wait = self._last.get(host, 0) + gap - time.monotonic()
                        if wait > 0:
                            await asyncio.sleep(wait)
                        self._last[host] = time.monotonic()
                async with sem:
                    self.requests += 1
                    r = await self.client.request(method, url, **kw)
                if r.status_code == 429:
                    pause = float(r.headers.get("retry-after", 0) or 0) or 15 * (attempt + 1)
                    if pause > 600:  # daily quota exhausted: skip this host for the rest of the run
                        if host not in self.blocked_hosts:
                            log.warning("%s quota exhausted (retry-after %.0fs); skipping it this run", host, pause)
                        self.blocked_hosts.add(host)
                        return None
                    log.info("429 from %s, pausing %.0fs", host, pause)
                    self._last[host] = time.monotonic() + pause  # pause the whole host, not just this request
                    await asyncio.sleep(pause)
                    continue
                if r.status_code in (500, 502, 503, 504):
                    await asyncio.sleep(2 ** attempt + random.random())
                    continue
                return r
            # Some hosts (e.g. breezy.hr) break HTTP/2 connections; h2 errors escape httpx unwrapped.
            except (httpx.TransportError, httpx.TimeoutException, h2.exceptions.H2Error):
                await asyncio.sleep(2 ** attempt)
        return None

    def add(self, ats: str, token: str, name: str = "", website: str = "", origin: str = "", jobs: int = 0,
            description: str = "") -> None:
        token = token.strip()
        if not token:
            return
        key = (ats, token.lower())
        c = self.companies.get(key)
        if c is None:
            c = self.companies[key] = Company(ats, token)
        c.name = c.name or name.strip()
        c.website = c.website or (website or "").strip()
        c.description = c.description or (description or "")[:2000]
        c.jobs += jobs
        if origin:
            c.origin.add(origin)

    def progress(self, stage: str) -> None:
        by_ats = Counter(c.ats for c in self.companies.values())
        log.info("[%s] %ds, %d requests, companies: %s", stage, time.monotonic() - self.t0, self.requests, dict(by_ats))

    # ------------------------------------------------------------ SmartRecruiters

    async def smartrecruiters(self) -> None:
        """The search endpoint ignores pagination, so fan out over many distinct
        keywords (skills, titles, places, industries): each returns up to 100
        live postings and therefore a different slice of employers."""
        url = "https://jobs.smartrecruiters.com/sr-jobs/search"
        from .skills import SKILLS

        seeds = set(SR_KEYWORDS) | set(SR_EXTRA_KEYWORDS) | {a.lower() for al in SKILLS.values() for a in al if not a.startswith("re:")}
        queue: list[str] = sorted(seeds)
        random.shuffle(queue)
        used: set[str] = set()
        budget = self.sr_queries

        async def one(kw: str) -> tuple[int, set[str]]:
            r = await self.get(url, params={"keyword": kw, "limit": 100})
            if r is None or r.status_code != 200:
                return 0, set()
            try:
                content = r.json().get("content") or []
            except ValueError:
                return 0, set()
            new, words = 0, set()
            for p in content:
                comp = p.get("company") or {}
                if comp.get("identifier"):
                    new += ("smartrecruiters", comp["identifier"].lower()) not in self.companies
                    self.add("smartrecruiters", comp["identifier"], comp.get("name", ""), origin="sr-search", jobs=1)
                # Snowball: title words become new queries -> new slices of employers.
                words.update(w for w in re.findall(r"[a-zà-ÿ]{4,20}", (p.get("name") or "").lower()))
            return new, words

        done = 0
        while queue and done < budget:
            batch = []
            while queue and len(batch) < 20:
                kw = queue.pop(0)
                if kw not in used:
                    used.add(kw)
                    batch.append(kw)
            results = await asyncio.gather(*(one(k) for k in batch))
            done += len(batch)
            for _, words in results:
                queue.extend(w for w in words - used)
            if done % 400 < 20:
                self.progress(f"smartrecruiters {done}/{budget} queries")

    # ------------------------------------------------------------ Workable

    async def workable(self) -> None:
        url = "https://jobs.workable.com/api/v1/jobs"

        async def stream(query: str, location: str) -> None:
            token, dry = None, 0
            for _ in range(self.max_workable_pages):
                params = {"query": query, "location": location}
                if token:
                    params["pageToken"] = token
                r = await self.get(url, params=params)
                if r is None or r.status_code != 200:
                    return
                try:
                    data = r.json()
                except ValueError:
                    return
                jobs = data.get("jobs") or []
                new = 0
                for j in jobs:
                    c = j.get("company") or {}
                    if c.get("id"):
                        key = ("workable", c["id"].lower())
                        new += key not in self.companies
                        self.add("workable", c["id"], c.get("title", ""), c.get("website", ""), "workable-search", 1,
                                 c.get("socialSharingDescription") or "")
                token = data.get("nextPageToken")
                if not token or not jobs:
                    return
                dry = dry + 1 if new == 0 else 0
                if dry >= 40:  # this slice keeps returning known companies
                    return

        sem = asyncio.Semaphore(3)  # requests are paced per host anyway

        async def guarded(q: str, loc: str) -> None:
            async with sem:
                await stream(q, loc)

        streams = [(q, loc) for loc in WORKABLE_LOCATIONS for q in (WORKABLE_QUERIES if loc in ("", "United States", "United Kingdom", "Germany", "India", "Remote") else [""])]
        tasks = [asyncio.create_task(guarded(q, loc)) for q, loc in streams]
        while tasks:
            done, pending = await asyncio.wait(tasks, timeout=60)
            tasks = list(pending)
            self.progress(f"workable ({len(tasks)} streams left)")

    # ------------------------------------------------------------ GitHub listings

    async def simplify(self) -> None:
        patterns = {
            "greenhouse": r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)",
            "lever": r"jobs\.lever\.co/([A-Za-z0-9_.-]+)",
            "ashby": r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)",
            "workable-slug": r"apply\.workable\.com/([A-Za-z0-9_-]+)",
            "smartrecruiters": r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)",
        }
        skip = {"embed", "j", "api", "jobs", "careers", "oneclick-ui", "sr-jobs", "search", "static", "assets"}
        for repo in ("Summer2026-Internships", "New-Grad-Positions", "Summer2025-Internships"):
            url = f"https://raw.githubusercontent.com/SimplifyJobs/{repo}/dev/.github/scripts/listings.json"
            r = await self.get(url)
            if r is None or r.status_code != 200:
                continue
            try:
                listings = r.json()
            except ValueError:
                continue
            for item in listings:
                link = item.get("url") or ""
                name = item.get("company_name") or ""
                if tok := workday_board(link):
                    self.candidates.setdefault("workday", {}).setdefault(tok, name)
                    continue
                for ats, pat in patterns.items():
                    if m := re.search(pat, link):
                        tok = m.group(1)
                        if tok.lower() not in skip:
                            self.candidates.setdefault(ats, {}).setdefault(tok, name)
        log.info("simplify candidates: %s", {k: len(v) for k, v in self.candidates.items()})

    async def ycombinator(self) -> None:
        r = await self.get("https://yc-oss.github.io/api/companies/all.json")
        if r is None or r.status_code != 200:
            return
        for c in r.json():
            if (c.get("status") or "").lower() not in ("active", "public"):
                continue
            name, slug, site = c.get("name") or "", c.get("slug") or "", c.get("website") or ""
            stems = {slug, re.sub(r"[^a-z0-9]", "", name.lower())}
            host = (urlsplit(site).hostname or "").removeprefix("www.")
            if host:
                stems.add(host.split(".")[0])
            for stem in filter(None, stems):
                for ats in ("greenhouse", "lever", "ashby"):
                    self.candidates.setdefault(ats, {}).setdefault(stem, name)
                self.websites[stem] = site
        log.info("candidates after YC: %s", {k: len(v) for k, v in self.candidates.items()})

    def workable_slug_candidates(self) -> None:
        """Workable's search gives company ids; boards are polled by account slug,
        usually the hyphenated company name (verified below)."""
        n = 0
        for c in list(self.companies.values()):
            if c.ats != "workable" or "-" not in c.token or len(c.token) != 36:
                continue
            base = re.sub(r"\b(inc|llc|ltd|gmbh|s\.?a|plc|corp|co)\b\.?", "", c.name.lower())
            slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
            for cand in {slug, slug.replace("-", "")}:
                if cand:
                    self.candidates.setdefault("workable-slug", {}).setdefault(cand, c.name)
                    self.websites.setdefault(cand, c.website)
                    n += 1
        # ids themselves are not stored: they need the quota-limited search host.
        for key in [k for k, c in self.companies.items() if c.ats == "workable" and len(c.token) == 36]:
            self.companies.pop(key)
        log.info("workable slug candidates from search: %d", n)

    async def tranco(self, limit: int) -> None:
        """Top-N registrable domains (Tranco list) -> slug candidates for every ATS."""
        if limit <= 0:
            return
        meta = await self.get("https://tranco-list.eu/api/lists/date/latest")
        if meta is None or meta.status_code != 200:
            log.warning("tranco list unavailable")
            return
        lst = await self.get(meta.json()["download"])  # full top-1M list (~22 MB)
        if lst is None or lst.status_code != 200:
            return
        lines = lst.text.splitlines()
        start = self.tranco_start % max(1, len(lines))
        slice_ = lines[start:start + limit]
        self.tranco_next = start + limit if start + limit < len(lines) else 0
        log.info("tranco: ranks %d-%d of %d", start + 1, start + len(slice_), len(lines))
        stems = []
        for line in slice_:
            _, _, dom = line.partition(",")
            stem = dom.strip().lower().split(".")[0]
            if 3 <= len(stem) <= 40 and re.fullmatch(r"[a-z0-9][a-z0-9-]*", stem) and not stem.isdigit():
                stems.append(stem)
        stems = list(dict.fromkeys(stems))
        for stem in stems:
            for ats in ("greenhouse", "lever", "ashby", "workable-slug", "recruitee", "personio", "breezy", "bamboohr", "teamtailor",
                        "rippling", "pinpoint", "jazzhr"):
                self.candidates.setdefault(ats, {}).setdefault(stem, "")
        self.tranco_stems = set(stems)
        log.info("tranco: %d domain stems -> %d probes", len(stems), len(stems) * 12)

    # ------------------------------------------------------------ verification

    async def verify(self) -> None:
        async def gh(tok: str, name: str) -> None:
            r = await self.get(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs")
            if r is not None and r.status_code == 200:
                jobs = r.json().get("jobs") or []
                real = next((j.get("company_name") for j in jobs if j.get("company_name")), "")
                if not real:
                    meta = await self.get(f"https://boards-api.greenhouse.io/v1/boards/{tok}")
                    real = (meta.json().get("name") if meta is not None and meta.status_code == 200 else "") or ""
                self.add("greenhouse", tok, real or name, self.websites.get(tok, ""), "verified", len(jobs))

        async def lever(tok: str, name: str) -> None:
            r = await self.get(f"https://api.lever.co/v0/postings/{tok}", params={"mode": "json", "limit": 200})
            if r is not None and r.status_code == 200 and isinstance(r.json(), list):
                self.add("lever", tok, name, self.websites.get(tok, ""), "verified", len(r.json()))

        async def ashby(tok: str, name: str) -> None:
            r = await self.get(f"https://api.ashbyhq.com/posting-api/job-board/{tok}")
            if r is not None and r.status_code == 200:
                try:
                    jobs = r.json().get("jobs") or []
                except ValueError:
                    return
                self.add("ashby", tok, name, self.websites.get(tok, ""), "verified", len(jobs))

        async def workable_slug(tok: str, name: str) -> None:
            r = await self.get(f"https://apply.workable.com/api/v1/widget/accounts/{tok}")
            if r is not None and r.status_code == 200:
                d = r.json()
                self.add("workable", tok, d.get("name") or name, "", "verified", len(d.get("jobs") or []),
                         d.get("description") or "")

        async def sr(tok: str, name: str) -> None:
            r = await self.get(f"https://api.smartrecruiters.com/v1/companies/{tok}/postings", params={"limit": 1})
            if r is not None and r.status_code == 200 and (r.json().get("totalFound") or 0) > 0:
                self.add("smartrecruiters", tok, name, "", "verified", r.json()["totalFound"])

        async def recruitee(tok: str, name: str) -> None:
            r = await self.get(f"https://{tok}.recruitee.com/api/offers/")
            if r is not None and r.status_code == 200:
                try:
                    offers = r.json().get("offers") or []
                except ValueError:
                    return
                real = next((o.get("company_name") for o in offers if o.get("company_name")), "")
                self.add("recruitee", tok, real or name, self.websites.get(tok, ""), "verified", len(offers))

        async def personio(tok: str, name: str) -> None:
            r = await self.get(f"https://{tok}.jobs.personio.de/xml")
            if r is not None and r.status_code == 200 and b"<workzag-jobs" in r.content[:500]:
                m = re.search(rb"<subcompany>([^<]+)</subcompany>", r.content)
                real = m.group(1).decode(errors="replace").strip() if m else ""
                self.add("personio", tok, real or name, self.websites.get(tok, ""), "verified", r.content.count(b"<position>"))

        async def breezy(tok: str, name: str) -> None:
            r = await self.get(f"https://{tok}.breezy.hr/json")
            if r is not None and r.status_code == 200:
                try:
                    jobs = r.json()
                except ValueError:
                    return
                if isinstance(jobs, list):
                    real = next(((j.get("company") or {}).get("name") for j in jobs if (j.get("company") or {}).get("name")), "")
                    self.add("breezy", tok, real or name, self.websites.get(tok, ""), "verified", len(jobs))

        async def workday(tok: str, name: str) -> None:
            tenant, wd, site = tok.split(".", 1)[0], tok.split(".", 1)[1].split("/")[0], tok.split("/", 1)[1]
            r = await self.get(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs", method="POST",
                               json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
            if r is not None and r.status_code == 200:
                total = int(r.json().get("total") or 0)
                if total:
                    self.add("workday", tok, name, "", "verified", total)

        async def bamboohr(tok: str, name: str) -> None:
            r = await self.get(f"https://{tok}.bamboohr.com/careers/list")
            # unknown tokens redirect to www.bamboohr.com (HTML)
            if r is not None and r.status_code == 200 and r.url.host == f"{tok.lower()}.bamboohr.com" \
                    and "json" in r.headers.get("content-type", ""):
                jobs = r.json().get("result") or []
                if jobs:
                    self.add("bamboohr", tok, name, self.websites.get(tok, ""), "verified", len(jobs))

        async def teamtailor(tok: str, name: str) -> None:
            r = await self.get(f"https://{tok}.teamtailor.com/jobs.rss")
            if r is not None and r.status_code == 200 and b"<rss" in r.content[:300]:
                n = r.content.count(b"<item>")
                if n:
                    m = re.search(rb"<channel>\s*<title>([^<]+)</title>", r.content)
                    real = m.group(1).decode(errors="replace").strip() if m else ""
                    self.add("teamtailor", tok, real or name, self.websites.get(tok, ""), "verified", n)

        async def rippling(tok: str, name: str) -> None:
            r = await self.get(f"https://api.rippling.com/platform/api/ats/v1/board/{tok}/jobs")
            if r is not None and r.status_code == 200:
                try:
                    jobs = r.json()
                except ValueError:
                    return
                if isinstance(jobs, list) and jobs:
                    self.add("rippling", tok, name, self.websites.get(tok, ""), "verified", len({j.get("uuid") for j in jobs}))

        async def pinpoint(tok: str, name: str) -> None:
            r = await self.get(f"https://{tok}.pinpointhq.com/postings.json")
            if r is not None and r.status_code == 200 and r.url.host == f"{tok.lower()}.pinpointhq.com":
                try:
                    jobs = r.json().get("data") or []
                except (ValueError, AttributeError):
                    return
                if jobs:
                    self.add("pinpoint", tok, name, self.websites.get(tok, ""), "verified", len(jobs))

        async def jazzhr(tok: str, name: str) -> None:
            r = await self.get(f"https://{tok}.applytojob.com/apply")
            # unknown tokens redirect to jazzhr.com
            if r is not None and r.status_code == 200 and r.url.host == f"{tok.lower()}.applytojob.com":
                n = r.text.count('<li class="list-group-item">')
                if n:
                    m = re.search(r"<title>\s*(.*?)\s*</title>", r.text, re.S | re.I)
                    real = re.sub(r"^\s*(?:careers?|jobs)\s+at\s+|\s*[-|]\s*(?:careers?(?: page)?|jobs|job board)\s*$", "",
                                  html.unescape(m.group(1)), flags=re.I).strip() if m else ""
                    self.add("jazzhr", tok, (real if real.lower() != "jazzhr" else "") or name,
                             self.websites.get(tok, ""), "verified", n)

        checks = {"rippling": rippling, "pinpoint": pinpoint, "jazzhr": jazzhr, "workday": workday, "bamboohr": bamboohr, "teamtailor": teamtailor, "greenhouse": gh, "lever": lever, "ashby": ashby, "workable-slug": workable_slug, "smartrecruiters": sr,
                  "recruitee": recruitee, "personio": personio, "breezy": breezy}
        work = [(checks[ats], tok, name) for ats, toks in self.candidates.items() for tok, name in toks.items()
                if (("workable" if ats == "workable-slug" else ats), tok.lower()) not in self.companies]
        # Named candidates (YC / Simplify / Workable) first, then domain stems by popularity;
        # interleave platforms so every per-platform pacer stays busy.
        named = [w for w in work if w[2]]
        unnamed = [w for w in work if not w[2]]
        random.shuffle(named)
        work = named + unnamed
        log.info("verifying %d candidate boards (%d named)", len(work), len(named))
        sem = asyncio.Semaphore(96)

        async def run(fn, tok, name):
            async with sem:
                try:
                    await fn(tok, name)
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass

        for i in range(0, len(work), 5000):
            await asyncio.gather(*(run(*w) for w in work[i:i + 5000]))
            self.progress(f"verify {min(i + 5000, len(work))}/{len(work)}")
            if self.checkpoint:
                write_csv(self.checkpoint, self.filtered())

    # ------------------------------------------------------------ main

    async def run(self, skip: set[str]) -> list[Company]:
        self.candidates: dict[str, dict[str, str]] = {}
        self.websites: dict[str, str] = {}
        phases = []
        if "smartrecruiters" not in skip:
            phases.append(self.smartrecruiters())
        if "workable" not in skip:
            phases.append(self.workable())
        if "simplify" not in skip:
            phases.append(self.simplify())
        if "yc" not in skip:
            phases.append(self.ycombinator())
        await asyncio.gather(*phases)
        self.workable_slug_candidates()
        if "tranco" not in skip:
            await self.tranco(self.tranco_limit)
        await self.verify()
        await self.client.aclose()
        kept = self.filtered(count=True)
        self.progress("done")
        log.info("excluded as middlemen/job boards: %s (e.g. %s)", dict(self.rejected), self.rejected_examples)
        return kept

    def load_existing(self, path: Path) -> int:
        """Merge mode: previously verified companies are kept and not re-probed."""
        n = 0
        try:
            with path.open(newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if r.get("ats") and r.get("token"):
                        self.add(r["ats"], r["token"], r.get("name", ""), r.get("website", ""),
                                 r.get("origin") or "previous", int(r.get("jobs_seen") or 0))
                        n += 1
        except FileNotFoundError:
            pass
        log.info("loaded %d previously discovered companies from %s", n, path)
        return n

    def filtered(self, count: bool = False) -> list[Company]:
        kept = []
        for c in self.companies.values():
            if not c.name:
                c.name = c.token.replace("-", " ").replace("_", " ").title()
            reason = is_middleman(c.name, c.description, c.website)
            if reason:
                if count:
                    kind = reason.split(" (")[0]
                    self.rejected[kind] += 1
                    self.rejected_examples.setdefault(kind, c.name)
                continue
            kept.append(c)
        return kept


def write_csv(path: Path, companies: list[Company]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    now = datetime.now(timezone.utc).date().isoformat()
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ats", "token", "name", "website", "jobs_seen", "origin", "verified_on"])
        for c in sorted(companies, key=lambda c: (c.ats, c.name.lower(), c.token.lower())):
            w.writerow([c.ats, c.token, c.name, c.website, c.jobs, "+".join(sorted(c.origin)), now])
    tmp.replace(path)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def run_once(args: argparse.Namespace) -> None:
    out = Path(args.out)
    state_path = out.with_name("discovery_state.json")
    state = _load_state(state_path)
    d = Discovery(args.concurrency, args.max_workable_pages, args.max_sr_offset, args.tranco, out)
    d.sr_queries = args.sr_queries
    d.tranco_start = int(state.get("tranco_next", 0)) if args.incremental else 0
    if args.incremental:
        d.load_existing(out)
    companies = asyncio.run(d.run(set(args.skip)))
    write_csv(out, companies)
    state.update(tranco_next=d.tranco_next, last_run=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 companies=len(companies))
    state_path.write_text(json.dumps(state, indent=2))
    log.info("wrote %d companies to %s: %s", len(companies), out, dict(Counter(c.ats for c in companies)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="/app/data/companies.discovered.csv")
    ap.add_argument("--concurrency", type=int, default=24)
    ap.add_argument("--max-workable-pages", type=int, default=9000, help="pages (20 jobs) per Workable search stream")
    ap.add_argument("--max-sr-offset", type=int, default=400_000)
    ap.add_argument("--sr-queries", type=int, default=4000, help="SmartRecruiters search queries (snowballed keywords)")
    ap.add_argument("--tranco", type=int, default=120_000, help="Tranco domains probed per run (0 = off)")
    ap.add_argument("--incremental", action="store_true", help="merge with the existing CSV and continue the Tranco cursor")
    ap.add_argument("--daemon", action="store_true", help="run incrementally every --every-hours (implies --incremental)")
    ap.add_argument("--every-hours", type=float, default=24)
    ap.add_argument("--skip", nargs="*", default=[], choices=["smartrecruiters", "workable", "simplify", "yc", "tranco"])
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not args.daemon:
        run_once(args)
        return
    args.incremental = True
    state_path = Path(args.out).with_name("discovery_state.json")
    while True:
        last = _load_state(state_path).get("last_run")
        if last:
            wait = datetime.fromisoformat(last).timestamp() + args.every_hours * 3600 - time.time()
            if wait > 0:
                log.info("next discovery run in %.1f h", wait / 3600)
                time.sleep(wait)
        try:
            run_once(args)
        except Exception:
            log.exception("discovery run failed; retrying in 1 h")
            time.sleep(3600)


if __name__ == "__main__":
    main()
