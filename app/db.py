"""Database models and engine (PostgreSQL via asyncpg)."""
from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Computed, DateTime, Index, Integer, SmallInteger, String, Text, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://jobhunt:jobhunt@db:5432/jobhunt")

POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))  # each connection is a Postgres backend (memory)
engine = create_async_engine(DATABASE_URL, pool_size=POOL_SIZE, max_overflow=max(2, POOL_SIZE // 2), pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    uid: Mapped[str] = mapped_column(String(300), unique=True)  # "<source>:<board>:<external id>"
    source: Mapped[str] = mapped_column(String(40))
    board: Mapped[str] = mapped_column(String(160))  # feed the job came from, for miss tracking
    authoritative: Mapped[bool] = mapped_column(Boolean, default=False)
    external_id: Mapped[str] = mapped_column(String(200))

    company: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(400))
    location: Mapped[str] = mapped_column(String(400), default="")
    remote: Mapped[bool] = mapped_column(Boolean, default=False)
    department: Mapped[str] = mapped_column(String(200), default="")
    employment_type: Mapped[str] = mapped_column(String(80), default="")
    url: Mapped[str] = mapped_column(Text)
    apply_url: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_currency: Mapped[str] = mapped_column(String(10), default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    skills: Mapped[list[str]] = mapped_column(ARRAY(String(60)), default=list)
    seniority: Mapped[str] = mapped_column(String(20), default="unspecified")
    years_required: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    matched: Mapped[bool] = mapped_column(Boolean, default=True)
    match_score: Mapped[int] = mapped_column(SmallInteger, default=0)
    match_notes: Mapped[str] = mapped_column(Text, default="")

    # Classification / trust (see trust.py)
    category: Mapped[str] = mapped_column(String(40), default="other")
    is_tech: Mapped[bool] = mapped_column(Boolean, default=False)
    languages: Mapped[list[str]] = mapped_column(ARRAY(String(30)), default=list)  # required non-English
    text_lang: Mapped[str] = mapped_column(String(16), default="unknown")
    lang_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    trust_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    trusted: Mapped[bool] = mapped_column(Boolean, default=True)
    link_rank: Mapped[int] = mapped_column(SmallInteger, default=3)  # 0 = employer ATS ... 3 = aggregator page

    content_hash: Mapped[str] = mapped_column(String(40))
    dedup_key: Mapped[str] = mapped_column(String(300))
    duplicate_of: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Liveness
    status: Mapped[str] = mapped_column(String(12), default="active")  # active | closed
    miss_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    check_result: Mapped[str] = mapped_column(String(200), default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # User state (mirrored to the tracker file, which is the source of truth)
    user_state: Mapped[str] = mapped_column(String(12), default="new")  # new | applied | hidden
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('simple', coalesce(title,'')), 'A') || "
            "setweight(to_tsvector('simple', coalesce(company,'')), 'B') || "
            "setweight(to_tsvector('english', left(coalesce(description,''), 50000)), 'C')",
            persisted=True,
        ),
    )

    __table_args__ = (
        Index("ix_jobs_listing", "user_state", "status", "is_tech", "matched", "trusted", "lang_ok", "duplicate_of"),
        Index("ix_jobs_category", "category"),
        Index("ix_jobs_company", "company"),
        Index("ix_jobs_board", "board"),
        Index("ix_jobs_dedup", "dedup_key"),
        Index("ix_jobs_posted", "posted_at"),
        Index("ix_jobs_first_seen", "first_seen_at"),
        Index("ix_jobs_score", "match_score"),
        Index("ix_jobs_check", "status", "authoritative", "last_checked_at"),
        Index("ix_jobs_skills", "skills", postgresql_using="gin"),
        Index("ix_jobs_search", "search", postgresql_using="gin"),
        Index("ix_jobs_title_trgm", "title", postgresql_using="gin", postgresql_ops={"title": "gin_trgm_ops"}),
        Index("ix_jobs_company_trgm", "company", postgresql_using="gin", postgresql_ops={"company": "gin_trgm_ops"}),
    )


class Company(Base):
    """Registry of employer career boards that are polled."""
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ats: Mapped[str] = mapped_column(String(30))
    token: Mapped[str] = mapped_column(String(200))
    token_key: Mapped[str] = mapped_column(String(200))  # lower-cased token for uniqueness
    name: Mapped[str] = mapped_column(String(300), default="")
    website: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(120), default="")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)  # listed in config.yaml: always hot
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    leased: Mapped[bool] = mapped_column(Boolean, default=False)  # claimed by a running batch
    status: Mapped[str] = mapped_column(String(12), default="new")  # new | ok | empty | error | gone
    jobs_count: Mapped[int] = mapped_column(Integer, default=0)
    tech_count: Mapped[int] = mapped_column(Integer, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_fetch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("uq_companies_ats_token", "ats", "token_key", unique=True),
        Index("ix_companies_due", "enabled", "next_fetch_at"),
    )

    @property
    def board_key(self) -> str:
        return f"{self.ats}:{self.token}"


class CompanyResolution(Base):
    """Attempts to find an employer's own ATS board for aggregator-only listings."""
    __tablename__ = "company_resolutions"

    company_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    company: Mapped[str] = mapped_column(String(300))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    found: Mapped[str] = mapped_column(String(250), default="")  # "ats:token" or ""
    detail: Mapped[str] = mapped_column(Text, default="")


class FetchRun(Base):
    __tablename__ = "fetch_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    board: Mapped[str] = mapped_column(String(160), index=True)
    source: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool] = mapped_column(Boolean)
    unchanged: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    matched: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    closed: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


def _add_missing_columns(sync_conn) -> list[str]:
    """Tiny forward-only migration: add columns that exist in the models but not
    in the database (new features), so upgrades never require wiping data."""
    from sqlalchemy import inspect
    from sqlalchemy.schema import CreateIndex

    insp = inspect(sync_conn)
    added = []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have or col.computed is not None:
                continue
            ddl_type = col.type.compile(dialect=sync_conn.dialect)
            default = ""
            if col.default is not None and getattr(col.default, "is_scalar", False):
                v = col.default.arg
                default = f" DEFAULT {'TRUE' if v is True else 'FALSE' if v is False else repr(v) if isinstance(v, str) else v}"
            elif col.default is not None and callable(getattr(col.default, "arg", None)):
                default = " DEFAULT '{}'" if "[]" in ddl_type else ""
            sync_conn.exec_driver_sql(f'ALTER TABLE {table.name} ADD COLUMN IF NOT EXISTS "{col.name}" {ddl_type}{default}')
            added.append(f"{table.name}.{col.name}")
        have_idx = {i["name"] for i in insp.get_indexes(table.name)}
        for idx in table.indexes:
            if idx.name not in have_idx:
                sync_conn.execute(CreateIndex(idx, if_not_exists=True))
    return added


async def init_db() -> None:
    import logging

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)
        added = await conn.run_sync(_add_missing_columns)
        if added:
            logging.getLogger(__name__).info("schema migrated: added %s", added)
