"""Import companies from another tracker's CSV, verify each board live, write a registry CSV.

    docker compose run --rm --no-deps -v /path/companies.csv:/import.csv:ro discovery \
        python -m app.import_companies /import.csv --out /app/data/companies.imported.csv

Input columns: company_name, ats_type, board_token, career_page_url (job_auto format).
Only boards that answer on their ATS API (with the same checks as discovery) are kept;
recruiters, staffing agencies and job boards are dropped. Boards already in the output
file are not re-probed.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from pathlib import Path

from .discover import Discovery, write_csv
from .registry import board_from_link
from .sources.ats import workday_board

ATS_MAP = {"greenhouse": "greenhouse", "lever": "lever", "ashby": "ashby", "smartrecruiters": "smartrecruiters",
           "workable": "workable-slug", "recruitee": "recruitee", "personio": "personio", "breezy": "breezy",
           "breezyhr": "breezy", "workday": "workday",
           "bamboohr": "bamboohr", "teamtailor": "teamtailor",
           "rippling": "rippling", "pinpoint": "pinpoint", "pinpointhq": "pinpoint",
           "jazzhr": "jazzhr", "applytojob": "jazzhr"}


def read_candidates(path: Path, known: set[tuple[str, str]]) -> dict[str, dict[str, str]]:
    cands: dict[str, dict[str, str]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ats = ATS_MAP.get((r.get("ats_type") or "").strip().lower())
            if not ats:
                continue
            tok = (r.get("board_token") or "").strip()
            if ats == "workday":
                tok = workday_board(r.get("career_page_url") or "") or workday_board(tok) or ""
            elif not tok and (found := board_from_link(r.get("career_page_url") or "")) and found[0] == ats:
                tok = found[1]
            if not tok or "/" in tok and ats != "workday":
                continue
            reg_ats = "workable" if ats == "workable-slug" else ats
            if (reg_ats, tok.lower()) in known:
                continue
            cands.setdefault(ats, {}).setdefault(tok, (r.get("company_name") or "").strip())
    return cands


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("--out", default="/app/data/companies.imported.csv")
    ap.add_argument("--known", nargs="*", default=["/app/data/companies.discovered.csv", "/app/config/companies.csv"],
                    help="registry CSVs whose boards are skipped")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    known: set[tuple[str, str]] = set()
    for p in args.known:
        try:
            with open(p, newline="", encoding="utf-8") as f:
                known |= {(r["ats"], r["token"].lower()) for r in csv.DictReader(f) if r.get("ats")}
        except FileNotFoundError:
            pass
    out = Path(args.out)
    d = Discovery(24, 0, 0, 0, out)
    d.load_existing(out)  # resume: already verified imports are kept and not re-probed
    d.candidates = read_candidates(Path(args.source), known)
    d.websites = {}
    logging.info("import candidates: %s", {k: len(v) for k, v in d.candidates.items()})

    async def run() -> None:
        await d.verify()
        await d.client.aclose()

    asyncio.run(run())
    kept = d.filtered(count=True)
    write_csv(out, kept)
    logging.info("wrote %d verified companies to %s (excluded: %s)", len(kept), out, dict(d.rejected))


if __name__ == "__main__":
    main()
