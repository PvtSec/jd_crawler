# JobHunt (public, single-container build)

A public listing of every job JobHunt finds on employers' own career boards (Greenhouse, Lever,
Ashby, Workday, SmartRecruiters, … ~48k boards) plus a few public aggregators. Visitors browse and
filter the whole list; there is no personal matching, scoring or applied/hidden tracking.

```
 one container ──────────────────────────────────────────────
 │ start.py (supervisor)                                      │
 │   ├─ PostgreSQL 15   unix socket only, <data_dir>/pgdata   │
 │   ├─ uvicorn :$PORT  API (/api, read-only) + dashboard (/) │
 │   │                  + background fetch/dedup/liveness     │
 │   └─ discovery       optional, grows the company registry  │
 ─────────────────────────────────────────────────────────────
 keepalive/keepalive.py   runs elsewhere, pings /api/health
```

## Configuration

**Everything is in [`config/config.yaml`](config/config.yaml)**: deployment (`server`), the
keep-alive poller (`keepalive`), fetch intervals, filters and sources. Only two environment
variables are honoured: `PORT` (set by Render) and, optionally, `DATABASE_URL` for an external
Postgres (so its password stays out of git).

When your domain is ready, change `server.public_url` to it (e.g. `https://jobs.example.com`) and
push. The app itself doesn't depend on its hostname; the URL is used by the keep-alive poller.

## Deploy to Render

1. Push this directory to a GitHub/GitLab repository.
2. Render → **New → Web Service** → pick the repo. Runtime **Docker** (Dockerfile at the root).
3. **Health check path:** `/api/health`.
4. **Persistent disk** (paid instance types): mount path `/app/data`, 10 GB or more. Without a disk
   the database is rebuilt from scratch after every deploy or restart. It refills on its own,
   but takes a while.
5. Instance size: 512 MB works with the defaults in `config.yaml`. With 2 GB or more, raise
   the values noted in the `server` section for faster crawling.
6. Custom domain: Render → Settings → Custom Domains, then update `server.public_url`.

On first start the image seeds the company registry (~30 s) and starts crawling. Jobs appear
within a minute, and the list keeps growing as the ~48k boards are fetched.

Run locally (any free port; doesn't clash with the compose-based app on 8080):

```bash
docker build -t jobhunt-render .
docker run --rm -p 127.0.0.1:10000:10000 -v jobhunt-render-data:/app/data jobhunt-render
```

## Keep-alive poller

Render's free instances sleep after 15 minutes without traffic. `keepalive/keepalive.py` pings
`<public_url>/api/health` every `keepalive.interval_minutes` (default 10). It is independent of the
app, has no dependencies (Python 3.9+ standard library), and reads the same config file.

* **GitHub Actions** (included): `.github/workflows/keepalive.yml` runs `--once` every 10 minutes.
  It runs automatically once the repo is on GitHub. Scheduled runs can be a few minutes late, and
  GitHub pauses them after 60 days with no repository activity.
* **Any always-on machine:** `python3 keepalive/keepalive.py` (loops forever), or from cron:
  `*/10 * * * * python3 /path/to/keepalive/keepalive.py --once`
* Override the target without editing the config: `--url https://…/api/health` or `KEEPALIVE_URL`.

## What changed versus the personal version

* One container: embedded PostgreSQL + FastAPI (which now also serves the dashboard; no nginx).
* Matching rules are neutral (no title/skill/location/seniority filters, no pinned companies), so
  every job found is stored and listed. The dashboard shows all open jobs by default; filters
  (tech only, remote, skills, category, hide flagged agencies, …) are opt-in.
* Read-only API: applied/hidden tracking, notes, CSV export, and the manual refresh, verify and
  validate endpoints are removed. `/api/config` no longer publishes the config. API docs are off.
