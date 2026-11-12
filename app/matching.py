"""Validation, normalisation, classification and config-based matching of jobs."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .config import AppConfig
from .skills import REMOTE_RE, SkillExtractor, detect_seniority, detect_years, get_extractor
from .sources.base import RawJob
from .trust import categorize, link_rank, required_languages, text_language, trust_flags


def _term_re(terms: list[str]) -> re.Pattern[str] | None:
    terms = [t.strip() for t in terms if t.strip()]
    if not terms:
        return None
    return re.compile("|".join(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])" for t in terms), re.I)


# Words in a remote posting's location that name no place ("Fully remote job", "Remote-first").
_LOC_NOISE = {
    "remote", "remotely", "fully", "full", "100", "job", "jobs", "work", "working", "from", "home", "wfh",
    "first", "friendly", "only", "ok", "possible", "optional", "flexible", "hybrid", "location", "locations",
    "distributed", "team", "based", "or", "and", "in", "the", "of", "any", "all", "multiple", "various",
    "tbd", "na", "n", "a", "time", "zone", "timezone", "timezones", "utc", "gmt", "office", "virtual",
    "telecommute", "telework", "position", "role", "opportunity", "available", "open", "to", "with",
}
# Terms meaning "no geographic restriction" (in addition to the configured locations).
_GLOBAL_RE = re.compile(
    r"(?<![A-Za-z0-9])(worldwide|anywhere|global(ly)?|international(ly)?|everywhere|any\s+(location|country|where)|"
    r"all\s+(countries|locations)|no\s+location\s+restrictions?)(?![A-Za-z0-9])", re.I)
# Region restrictions written into the title: "QA Engineer - US Remote", "(Remote, EMEA)", "UK only".
_REGIONS = r"(us|usa|u\.s\.?|united\s+states|uk|united\s+kingdom|eu|europe|emea|latam|canada|americas|north\s+america|germany|australia|anz)"
_TITLE_REGION_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + _REGIONS + r"[\s,/-]*(remote|only|based)|remote[\s,(/-]*" + _REGIONS + r")(?![A-Za-z0-9])", re.I)
_WORD_RE = re.compile(r"[A-Za-z]+|\d+")


class Matcher:
    """Compiled view of the `match` / `trust` sections of the config."""

    def __init__(self, cfg: AppConfig):
        m = cfg.match
        self.cfg = cfg
        self.extractor: SkillExtractor = get_extractor(cfg)
        self.title_inc = _term_re(m.title_include)
        self.title_exc = _term_re(m.title_exclude)
        self.desc_exc = _term_re(m.description_exclude)
        self.company_exc = {c.strip().lower() for c in m.companies_exclude}
        # Whole-word, like every other text filter: "india" must not match "Indiana".
        self.locations = _term_re(m.locations)
        # Allowed places other than the bare word "remote" ("india", "apac", "worldwide", ...).
        self.places = _term_re([t for t in m.locations if t.strip().lower() not in ("remote", "remotely")])
        canon = self.extractor.canonical
        self.required = [canon(s) for s in m.skills_required]
        self.any_of = [canon(s) for s in m.skills_any]
        self.preferred = {canon(s): w for s, w in m.skills_preferred.items()}
        self.seniority = set(m.seniority)
        self.allowed_langs = set(m.allowed_languages)
        self.blocked_domains = tuple(d.lower() for d in cfg.trust.blocked_domains)
        self.blocked_companies = tuple(c.lower().strip() for c in cfg.trust.blocked_companies)

    def title_ok(self, title: str) -> bool:
        if self.title_inc and not self.title_inc.search(title):
            return False
        return not (self.title_exc and self.title_exc.search(title))

    def evaluate(self, job: "NormalizedJob") -> tuple[bool, int, str]:
        """Returns (matched, score 0-100, human readable notes)."""
        m = self.cfg.match
        fails, notes = [], []
        if not job.is_tech:
            fails.append(f"non-tech ({job.category})")
        if not self.title_ok(job.title):
            fails.append("title filter")
        if job.company.lower() in self.company_exc:
            fails.append("excluded company")
        if self.desc_exc and (hit := self.desc_exc.search(job.description)):
            fails.append(f"description contains '{hit.group(0)}'")
        if m.remote_only and not job.remote:
            fails.append("not remote")
        if self.locations:
            hay = f"{job.location} {'remote' if job.remote else ''}"
            if not self.locations.search(hay):
                fails.append("location")
            elif m.skip_region_locked_remote and (region := self.region_lock(job)):
                fails.append(f"remote limited to {region}")
        if self.seniority and job.seniority not in self.seniority and job.seniority != "unspecified":
            fails.append(f"seniority {job.seniority}")
        if m.max_years_experience is not None and job.years_required and job.years_required > m.max_years_experience:
            fails.append(f"requires {job.years_required}+ years")
        if m.max_age_days and job.posted_at and job.posted_at < datetime.now(timezone.utc) - timedelta(days=m.max_age_days):
            fails.append("too old")
        skills = set(job.skills)
        missing = [s for s in self.required if s not in skills]
        if missing:
            fails.append(f"missing required {', '.join(missing)}")
        if self.any_of and not skills.intersection(self.any_of):
            fails.append("none of skills_any")

        if self.preferred:
            total = sum(self.preferred.values())
            got = [s for s in self.preferred if s in skills]
            score = round(100 * sum(self.preferred[s] for s in got) / total)
            if got:
                notes.append("preferred: " + ", ".join(got))
        else:
            score = 100 if not fails else 0
        if score < m.min_score:
            fails.append(f"score {score} < {m.min_score}")
        return (not fails), score, "; ".join(fails + notes)

    def region_lock(self, job: "NormalizedJob") -> str | None:
        """For a job that passed the location filter only by being remote, return the place it
        is restricted to ("United States" for "Remote (United States)"), else None."""
        loc = job.location or ""
        if (hit := _TITLE_REGION_RE.search(job.title or "")) and not (self.places and self.places.search(job.title)):
            return hit.group(0)
        if (self.places and self.places.search(loc)) or _GLOBAL_RE.search(loc):
            return None
        rest = [w for w in _WORD_RE.findall(loc) if w.lower() not in _LOC_NOISE and not w.isdigit()]
        return " ".join(rest)[:60] if rest else None

    def language_ok(self, job: "NormalizedJob") -> bool:
        if not self.cfg.match.skip_language_requirements:
            return True
        if any(lang not in self.allowed_langs for lang in job.languages):
            return False
        return job.text_lang in ("english", "unknown") or job.text_lang in self.allowed_langs


@dataclass
class NormalizedJob:
    uid: str
    source: str
    board: str
    authoritative: bool
    external_id: str
    company: str
    title: str
    url: str
    apply_url: str
    location: str
    remote: bool
    description: str
    department: str
    employment_type: str
    posted_at: datetime | None
    salary_min: int | None
    salary_max: int | None
    salary_currency: str
    content_hash: str
    dedup_key: str
    skills: list[str] = field(default_factory=list)
    seniority: str = "unspecified"
    years_required: int | None = None
    matched: bool = True
    match_score: int = 0
    match_notes: str = ""
    category: str = "other"
    is_tech: bool = False
    languages: list[str] = field(default_factory=list)
    text_lang: str = "unknown"
    lang_ok: bool = True
    trust_flags: list[str] = field(default_factory=list)
    trusted: bool = True
    link_rank: int = 3


# Skills that indicate hands-on software work (used to classify ambiguous titles).
CORE_TECH_SKILLS = {
    "Python", "Java", "JavaScript", "TypeScript", "Go", "Rust", "C++", "C#", "Ruby", "PHP", "Kotlin", "Swift", "Scala",
    "React", "Node.js", "Django", "Spring", ".NET", "Kubernetes", "Docker", "AWS", "GCP", "Azure", "Terraform",
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "Kafka", "Spark", "Linux", "Git", "CI/CD", "Microservices",
    "REST APIs", "GraphQL", "Machine Learning", "PyTorch", "TensorFlow", "SQL", "Distributed Systems",
}


# Test/QA titles that are often about physical products ("Mechanical Test Engineer",
# "Hardware Validation Engineer", PLC "Automation Engineer"): tech only with software evidence.
_PHYSICAL_TEST_RE = re.compile(
    r"\b(mechanical|hardware|electrical|electronics?|civil|structural|chemical|manufacturing|industrial|"
    r"automotive|vehicle|aerospace|avionics|hvac|rf|analog|silicon|asic|pcb|battery|materials|environmental|"
    r"reliability|plc|controls?|instrumentation|process|production|equipment|product safety)\b|^automation engineer$", re.I)
TEST_TOOL_SKILLS = {"Testing", "Playwright", "Selenium", "Cypress", "Appium", "API Testing", "pytest", "JUnit"}


class InvalidJob(ValueError):
    pass


def _valid_url(u: str) -> bool:
    try:
        p = urlsplit(u)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc) and len(u) < 4000


_COMPANY_NOISE = re.compile(r"\b(inc|llc|ltd|limited|gmbh|ag|sa|sas|bv|plc|corp|corporation|co|company|the|group|holdings?)\b")
_TITLE_NOISE = re.compile(r"\b(remote|hybrid|on[- ]?site|onsite|m/w/d|m/f/d|f/m/d|w/m/d|all genders?|full[- ]time|part[- ]time|contract|\d{4})\b")


def _slug(s: str, noise: re.Pattern[str] | None = None) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\([^)]*\)", " ", s)  # "(Remote, EU)" style suffixes
    if noise is not None:
        s = noise.sub(" ", s)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def dedup_key(company: str, title: str) -> str:
    """Same role at the same company, regardless of which board listed it."""
    title = re.split(r"\s+[-–|@]\s+", title)[0]  # "Engineer - Berlin" / "Engineer | Acme"
    return f"{_slug(company, _COMPANY_NOISE)}|{_slug(title, _TITLE_NOISE)}"[:300]


def normalize(raw: RawJob, *, source: str, board: str, authoritative: bool, max_desc: int) -> NormalizedJob:
    """Validate a raw job and build the canonical record. Raises InvalidJob."""
    title = raw.title.strip()
    company = raw.company.strip()
    if not raw.external_id:
        raise InvalidJob("missing id")
    if len(title) < 2:
        raise InvalidJob("missing title")
    if not company:
        raise InvalidJob("missing company")
    if not _valid_url(raw.url):
        raise InvalidJob(f"bad url {raw.url!r}")
    apply_url = raw.apply_url if raw.apply_url and _valid_url(raw.apply_url) else ""
    now = datetime.now(timezone.utc)
    posted = raw.posted_at if raw.posted_at and raw.posted_at <= now + timedelta(days=1) else None
    smin, smax = raw.salary_min, raw.salary_max
    if smin and smax and smin > smax:
        smin, smax = smax, smin
    description = raw.description[:max_desc]
    if raw.tags:
        description = f"{description}\n\nTags: {', '.join(map(str, raw.tags))}"[: max_desc + 500]
    remote = bool(raw.remote) if raw.remote is not None else bool(REMOTE_RE.search(f"{raw.location} {title}"))
    h = hashlib.sha1("\x1f".join(map(str, (
        title, company, raw.location, remote, raw.url, apply_url, description, raw.department,
        raw.employment_type, posted, smin, smax, raw.salary_currency,
    ))).encode()).hexdigest()
    return NormalizedJob(
        uid=f"{source}:{board.split(':', 1)[-1]}:{raw.external_id}"[:300] if authoritative else f"{source}:{raw.external_id}"[:300],
        source=source, board=board, authoritative=authoritative, external_id=raw.external_id[:200],
        company=company[:200], title=title[:400], url=raw.url, apply_url=apply_url,
        location=raw.location[:400], remote=remote, description=description,
        department=raw.department[:200], employment_type=raw.employment_type[:80], posted_at=posted,
        salary_min=smin, salary_max=smax, salary_currency=raw.salary_currency[:10],
        content_hash=h, dedup_key=dedup_key(company, title),
        link_rank=link_rank(raw.url, apply_url, authoritative),
    )


def classify(job: NormalizedJob, matcher: Matcher) -> NormalizedJob:
    """Cheap, title-level pass applied to every job (tech or not)."""
    job.category, job.is_tech = categorize(job.title, job.department)
    job.seniority = detect_seniority(job.title)
    job.trust_flags = trust_flags(
        company=job.company, title=job.title, description=job.description, url=job.url, apply_url=job.apply_url,
        authoritative=job.authoritative, blocked_domains=matcher.blocked_domains,
        blocked_companies=matcher.blocked_companies,
    )
    job.trusted = not job.trust_flags
    return job


def enrich(job: NormalizedJob, matcher: Matcher) -> NormalizedJob:
    """Full pass: classification + (for tech jobs) skills, requirements, language and matching."""
    classify(job, matcher)
    if job.category == "engineering/other":
        # Ambiguous "Engineer" title: decide from the skills the JD asks for.
        job.skills = matcher.extractor.extract(f"{job.title}\n{job.department}\n{job.description}")
        job.is_tech = len(CORE_TECH_SKILLS.intersection(job.skills)) >= 2
        if job.is_tech:
            job.category = "software"
    elif job.category == "qa/test" and _PHYSICAL_TEST_RE.search(job.title.strip()):
        job.skills = matcher.extractor.extract(f"{job.title}\n{job.department}\n{job.description}")
        if len((CORE_TECH_SKILLS | TEST_TOOL_SKILLS).intersection(job.skills)) < 2:
            job.category, job.is_tech = "engineering (non-software)", False
    if job.is_tech:
        text = f"{job.title}\n{job.department}\n{job.description}"
        job.skills = matcher.extractor.extract(text)
        job.years_required = detect_years(job.description)
        job.languages = required_languages(job.title, job.description)
        job.text_lang = text_language(job.description)
        job.lang_ok = matcher.language_ok(job)
    else:
        # Non-tech jobs are kept as slim records: no skill extraction, short description.
        job.skills, job.years_required, job.languages, job.text_lang, job.lang_ok = [], None, [], "unknown", True
        job.description = job.description[: matcher.cfg.fetch.non_tech_description_chars]
    job.matched, job.match_score, job.match_notes = matcher.evaluate(job)
    return job
