"""YAML configuration: schema, validation and hot reload.

The config file is re-read whenever its mtime changes. An invalid file never
replaces a valid one: the last good config stays active and the error is
exposed through /api/status so the dashboard can show it.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

log = logging.getLogger(__name__)

CONFIG_PATH = Path(os.getenv("JOBHUNT_CONFIG", "/app/config/config.yaml"))


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FetchCfg(Strict):
    interval_minutes: int = Field(30, ge=5, le=1440)
    max_concurrency: int = Field(8, ge=1, le=64)
    per_host_concurrency: int = Field(2, ge=1, le=16)
    timeout_seconds: float = Field(25, ge=3, le=120)
    retries: int = Field(2, ge=0, le=6)
    user_agent: str = "JobHunt/1.0 (personal job tracker)"
    max_description_chars: int = Field(20000, ge=500, le=200000)
    # Company registry polling tiers (adaptive: busy boards often, empty ones rarely)
    warm_interval_hours: float = Field(4, ge=0.25, le=168)    # board has tech jobs but none match
    cool_interval_hours: float = Field(12, ge=0.5, le=336)    # board has only non-tech jobs
    cold_interval_hours: float = Field(48, ge=1, le=720)      # board currently has no jobs
    max_backoff_hours: float = Field(168, ge=1, le=2160)      # failing boards back off up to this
    gone_after_failures: int = Field(4, ge=1, le=50)          # consecutive 404s => board retired
    non_tech_description_chars: int = Field(400, ge=0, le=200000)  # non-tech jobs are kept as slim records


class ValidationCfg(Strict):
    interval_minutes: int = Field(60, ge=5, le=1440)
    batch_size: int = Field(150, ge=1, le=5000)
    recheck_after_hours: int = Field(12, ge=1, le=720)
    # ATS boards return the complete listing; a job missing from N consecutive
    # successful fetches is considered closed.
    closed_after_misses: int = Field(2, ge=1, le=20)
    purge_closed_after_days: int = Field(30, ge=1, le=3650)
    dead_phrases: list[str] = Field(default_factory=lambda: [
        "no longer accepting applications",
        "no longer available",
        "job has expired",
        "this job has been closed",
        "position has been filled",
        "job is closed",
        "job not found",
        "posting has been removed",
        "this position is no longer open",
        "the job you are looking for",
        "page you are looking for doesn",
    ])


class MatchCfg(Strict):
    title_include: list[str] = Field(default_factory=list)
    title_exclude: list[str] = Field(default_factory=list)
    description_exclude: list[str] = Field(default_factory=list)
    skills_required: list[str] = Field(default_factory=list)
    skills_any: list[str] = Field(default_factory=list)
    skills_preferred: dict[str, int] = Field(default_factory=dict)
    locations: list[str] = Field(default_factory=list)
    remote_only: bool = False
    # Skip remote postings limited to a country/region not in `locations`, e.g.
    # "Remote (US)", "United States, Remote", "Remote - EMEA". A remote posting passes
    # only if it names an allowed location, a global term, or no place at all.
    skip_region_locked_remote: bool = False
    seniority: list[str] = Field(default_factory=list)
    max_years_experience: int | None = Field(None, ge=0, le=40)
    max_age_days: int | None = Field(45, ge=1)
    min_score: int = Field(0, ge=0, le=100)
    store_non_matching: bool = True
    companies_exclude: list[str] = Field(default_factory=list)
    # Postings that explicitly require a language not in this list (or whose
    # description is written in another language) are hidden by default.
    allowed_languages: list[str] = Field(default_factory=lambda: ["english"])
    skip_language_requirements: bool = True

    @field_validator("allowed_languages")
    @classmethod
    def _lower_langs(cls, v: list[str]) -> list[str]:
        return [x.strip().lower() for x in v if x.strip()]

    @field_validator("skills_preferred", mode="before")
    @classmethod
    def _list_to_weights(cls, v: Any) -> Any:
        if isinstance(v, list):
            return {str(s): 1 for s in v}
        return v or {}

    @field_validator("skills_preferred")
    @classmethod
    def _positive_weights(cls, v: dict[str, int]) -> dict[str, int]:
        for k, w in v.items():
            if w < 1 or w > 10:
                raise ValueError(f"weight for '{k}' must be 1..10")
        return v

    @field_validator("seniority")
    @classmethod
    def _known_seniority(cls, v: list[str]) -> list[str]:
        from .skills import SENIORITY_LEVELS

        bad = [s for s in v if s.lower() not in SENIORITY_LEVELS]
        if bad:
            raise ValueError(f"unknown seniority {bad}; allowed: {SENIORITY_LEVELS}")
        return [s.lower() for s in v]


class TrustCfg(Strict):
    blocked_domains: list[str] = Field(default_factory=list)    # extra harvester / middleman hosts
    blocked_companies: list[str] = Field(default_factory=list)  # exact company names to always flag
    exclude_middlemen_from_registry: bool = True


class AggregatorCfg(Strict):
    enabled: bool = True
    # Optional search terms / tags / categories, interpreted per source.
    queries: list[str] = Field(default_factory=list)
    max_pages: int = Field(3, ge=1, le=50)


class SourcesCfg(Strict):
    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)
    smartrecruiters: list[str] = Field(default_factory=list)
    workable: list[str] = Field(default_factory=list)  # account slug or company id
    recruitee: list[str] = Field(default_factory=list)
    personio: list[str] = Field(default_factory=list)
    breezy: list[str] = Field(default_factory=list)
    bamboohr: list[str] = Field(default_factory=list)    # <token>.bamboohr.com
    teamtailor: list[str] = Field(default_factory=list)  # <token>.teamtailor.com
    rippling: list[str] = Field(default_factory=list)    # ats.rippling.com/<slug>
    pinpoint: list[str] = Field(default_factory=list)    # <token>.pinpointhq.com
    jazzhr: list[str] = Field(default_factory=list)      # <token>.applytojob.com
    workday: list[str] = Field(default_factory=list)  # "tenant.wdN/Site", e.g. "nvidia.wd5/NVIDIAExternalCareerSite"
    # CSV registries of companies (ats,token,name,...) polled adaptively.
    # Relative paths are resolved against server.data_dir.
    company_files: list[str] = Field(default_factory=lambda: [
        "companies.csv", "companies.discovered.csv", "companies.imported.csv"])
    auto_register_from_aggregators: bool = True  # learn boards from aggregator apply links
    remotive: AggregatorCfg = AggregatorCfg()
    remoteok: AggregatorCfg = AggregatorCfg()
    arbeitnow: AggregatorCfg = AggregatorCfg()
    himalayas: AggregatorCfg = AggregatorCfg()


class ServerCfg(Strict):
    """Deployment settings. Read once at container start (changes need a restart)."""
    public_url: str = ""                 # e.g. https://jobs.example.com (no trailing slash)
    port: int = Field(10000, ge=1, le=65535)  # overridden by the PORT env var (Render sets it)
    data_dir: str = "/app/data"          # database + company lists; mount a persistent disk here
    database_url: str = ""               # empty = embedded PostgreSQL; DATABASE_URL env overrides
    log_level: str = "INFO"
    postgres_shared_buffers: str = "64MB"
    postgres_work_mem: str = "4MB"
    postgres_max_connections: int = Field(40, ge=20, le=500)
    db_pool_size: int = Field(5, ge=2, le=100)     # worker connections (each costs memory in Postgres)
    separate_worker: bool = True         # fetch in its own process (responsive UI; ~+80 MB memory)
    memory_limit_mb: int = Field(0, ge=0)  # 0 = detect from the container (Render free: 512)
    memory_guard_pct: int = Field(85, ge=50, le=95)  # restart the fetch worker above this % of the limit
    discovery_enabled: bool = False      # grow the company registry daily (CPU/network heavy)
    discovery_every_hours: float = Field(24, ge=1, le=720)


class KeepaliveCfg(Strict):
    """Used by keepalive/keepalive.py (runs outside the app), not by the app itself."""
    url: str = ""                        # empty = <server.public_url>/api/health
    interval_minutes: float = Field(10, gt=0, le=60)   # Render free instances sleep after 15 idle minutes
    timeout_seconds: float = Field(60, gt=0, le=300)   # a cold start can take ~1 minute


class StorageCfg(Strict):
    """Keeps the database inside a size budget (free hosted Postgres tiers are ~0.5-1 GB)."""
    tech_only: bool = True               # store only tech jobs (non-tech postings are never written)
    max_db_mb: int = Field(0, ge=0)      # database size budget in MB; 0 = unlimited
    high_water_pct: int = Field(80, ge=30, le=95)  # start evicting at this % of max_db_mb
    evict_pct: int = Field(15, ge=1, le=50)        # each eviction step drops this % of jobs (oldest first)


class AppConfig(Strict):
    server: ServerCfg = ServerCfg()
    storage: StorageCfg = StorageCfg()
    keepalive: KeepaliveCfg = KeepaliveCfg()
    fetch: FetchCfg = FetchCfg()
    validation: ValidationCfg = ValidationCfg()
    match: MatchCfg = MatchCfg()
    trust: TrustCfg = TrustCfg()
    sources: SourcesCfg = SourcesCfg()
    # Extra skills for the extractor: {"Name": ["alias", "re:regex"]}
    skills_extra: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _resolve_company_files(self) -> "AppConfig":
        base = Path(self.server.data_dir)
        self.sources.company_files = [f if Path(f).is_absolute() else str(base / f) for f in self.sources.company_files]
        return self


class ConfigStore:
    """Thread-safe holder that reloads the YAML file when it changes."""

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._cfg = AppConfig()
        self.error: str | None = None
        self.version = 0
        self.reload(force=True)

    @property
    def current(self) -> AppConfig:
        return self._cfg

    def reload(self, force: bool = False) -> bool:
        """Re-read the file if it changed. Returns True when a new config was applied."""
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                if self._mtime is not None or force:
                    self.error = f"config file not found at {self.path}; using defaults"
                    log.warning(self.error)
                self._mtime = None
                return False
            if not force and mtime == self._mtime:
                return False
            self._mtime = mtime
            try:
                raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                cfg = AppConfig.model_validate(raw)
                from .skills import validate_skill_names

                validate_skill_names(cfg)
            except (yaml.YAMLError, ValidationError, ValueError) as e:
                self.error = f"invalid config ({self.path.name}): {e}"
                log.error(self.error)
                return False
            self._cfg = cfg
            self.error = None
            self.version += 1
            log.info("config loaded (version %s)", self.version)
            return True


config_store = ConfigStore()
