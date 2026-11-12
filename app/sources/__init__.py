"""Registry of job sources. Add a new source by subclassing Source and listing it here."""
from .aggregators import Arbeitnow, Himalayas, RemoteOK, Remotive
from .ats import (
    Ashby, BambooHR, Breezy, Greenhouse, JazzHR, Lever, Personio, Pinpoint, Recruitee, Rippling, SmartRecruiters, Teamtailor,
    Workable, Workday,
)
from .base import BoardResult, FetchContext, RawJob, Source, VerifyResult

ATS_SOURCES = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "personio", "breezy", "bamboohr", "teamtailor", "rippling", "pinpoint", "jazzhr", "workday")

SOURCES: dict[str, Source] = {
    s.name: s
    for s in (Greenhouse(), Lever(), Ashby(), SmartRecruiters(), Workable(), Recruitee(), Personio(), Breezy(), BambooHR(), Teamtailor(), Rippling(), Pinpoint(), JazzHR(), Workday(), Remotive(), RemoteOK(), Arbeitnow(), Himalayas())
}

__all__ = ["ATS_SOURCES", "SOURCES", "BoardResult", "FetchContext", "RawJob", "Source", "VerifyResult"]
