"""Common types and helpers for job sources."""
from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable

from ..config import AppConfig
from ..http import Http


@dataclass
class RawJob:
    external_id: str
    company: str
    title: str
    url: str
    location: str = ""
    remote: bool | None = None
    description: str = ""
    department: str = ""
    employment_type: str = ""
    apply_url: str = ""
    posted_at: datetime | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class BoardResult:
    source: str
    board: str  # stable key, e.g. "greenhouse:stripe"
    authoritative: bool
    jobs: list[RawJob] = field(default_factory=list)
    # External ids present in the feed (authoritative boards only). Used to
    # detect jobs that disappeared, independent of which ones matched filters.
    present_ids: set[str] | None = None
    ok: bool = True
    unchanged: bool = False
    error: str = ""


@dataclass
class VerifyResult:
    alive: bool | None  # None = could not determine
    detail: str


@dataclass
class FetchContext:
    cfg: AppConfig
    http: Http
    title_ok: Callable[[str], bool]  # cheap prefilter, used to skip expensive detail calls
    light: bool = False  # probing only: skip per-job detail requests


class Source(ABC):
    name: str = ""
    authoritative: bool = False  # True when a board returns its full, current listing

    @abstractmethod
    def boards(self, cfg: AppConfig) -> list[str]:
        """Board identifiers configured for this source."""

    @abstractmethod
    async def fetch_board(self, ctx: FetchContext, board: str, name: str = "") -> BoardResult:
        ...

    async def verify(self, http: Http, board: str, external_id: str, url: str) -> VerifyResult | None:
        """Source-specific liveness check. None means fall back to the generic page check."""
        return None

    def board_key(self, board: str) -> str:
        return f"{self.name}:{board}" if board != self.name else self.name

    def result(self, board: str, **kw: Any) -> BoardResult:
        return BoardResult(source=self.name, board=self.board_key(board), authoritative=self.authoritative, **kw)


# ---------------------------------------------------------------- helpers


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


_WS = re.compile(r"[ \t\r\f\v ]+")
_NL = re.compile(r"\n\s*\n+")


def html_to_text(raw: str | None, *, escaped: bool = False) -> str:
    if not raw:
        return ""
    if escaped:  # Greenhouse returns entity-escaped HTML
        raw = html.unescape(raw)
    if "<" not in raw:
        text = html.unescape(raw)
    else:
        p = _TextExtractor()
        try:
            p.feed(raw)
            p.close()
            text = "".join(p.parts)
        except Exception:  # malformed markup: strip tags crudely
            text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NL.sub("\n\n", text).strip()


def parse_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            n = float(v)
            if n > 1e12:  # milliseconds
                n /= 1000
            return datetime.fromtimestamp(n, tz=timezone.utc)
        s = str(v).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def to_int(v: Any) -> int | None:
    try:
        n = int(float(v))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def clean(s: Any, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


async def stored_details(board_key: str) -> dict[str, dict[str, Any]]:
    """Detail fields already stored for a board's jobs (external_id -> fields).

    Sources whose listing endpoint lacks descriptions use this so a restart
    does not re-download every job detail (and the content hash still matches)."""
    from sqlalchemy import select

    from ..db import Job, Session

    async with Session() as s:
        rows = (await s.execute(
            select(Job.external_id, Job.description, Job.location, Job.remote, Job.employment_type,
                   Job.posted_at, Job.url, Job.apply_url)
            .where(Job.board == board_key, Job.description != "")
        )).mappings().all()
    return {r["external_id"]: dict(r) for r in rows}
