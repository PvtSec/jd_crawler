#!/usr/bin/env python3
"""Container entrypoint: runs the whole stack in one container.

  1. reads the `server` section of config/config.yaml (env vars PORT / DATABASE_URL override)
  2. seeds the company lists into the data dir (first start only)
  3. starts an embedded PostgreSQL unless an external database is configured
  4. starts the web app (API + dashboard), the fetch worker and, optionally, the discovery daemon
  5. supervises them: if one exits, everything is stopped and the container exits
     (Render then restarts it); SIGTERM shuts everything down cleanly.
"""
from __future__ import annotations

import os
import pwd
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

CONFIG = Path(os.getenv("JOBHUNT_CONFIG", "/app/config/config.yaml"))
SEED_DIR = Path("/app/seed")
PG_BIN = Path(os.getenv("PG_BIN", "/usr/lib/postgresql/15/bin"))
RUN_AS = "app"
PG_SOCKET_DIR = "/tmp/pgsocket"


def log(msg: str) -> None:
    print(f"[start] {msg}", flush=True)


def load_server_cfg() -> dict:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    return raw.get("server") or {}


def demote():
    """preexec_fn: drop root to the unprivileged app user (no-op when already unprivileged)."""
    if os.getuid() != 0:
        return None
    pw = pwd.getpwnam(RUN_AS)

    def _set():
        os.setgroups([])
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)
    return _set


def own(path: Path) -> None:
    """Give `path` to the app user. Anything already owned by it is skipped, so a large
    existing database costs nothing; a freshly attached (root-owned) disk is fixed once."""
    if os.getuid() != 0:
        return
    pw = pwd.getpwnam(RUN_AS)

    def fix(p: Path) -> None:
        if p.stat(follow_symlinks=False).st_uid != pw.pw_uid:
            os.chown(p, pw.pw_uid, pw.pw_gid, follow_symlinks=False)
            if p.is_dir() and not p.is_symlink():
                for q in p.rglob("*"):
                    os.chown(q, pw.pw_uid, pw.pw_gid, follow_symlinks=False)

    fix(path)
    if path.is_dir():
        for child in path.iterdir():
            fix(child)


def run(cmd: list[str], **kw) -> None:
    subprocess.run(cmd, check=True, preexec_fn=demote(), **kw)


class Stack:
    def __init__(self) -> None:
        self.procs: list[tuple[str, subprocess.Popen]] = []
        self.optional: set[str] = set()
        self.stopping = False

    def spawn(self, name: str, cmd: list[str], env: dict, optional: bool = False) -> subprocess.Popen:
        log(f"starting {name}")
        if optional:
            self.optional.add(name)
        p = subprocess.Popen(cmd, env=env, preexec_fn=demote())
        self.procs.append((name, p))
        return p

    def stop(self, *_):
        if self.stopping:
            return
        self.stopping = True
        # Reverse order: app and discovery first, the database last.
        for name, p in reversed(self.procs):
            if p.poll() is None:
                log(f"stopping {name}")
                p.send_signal(signal.SIGINT if name == "postgres" else signal.SIGTERM)
                try:
                    p.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    p.kill()

    def supervise(self) -> int:
        while not self.stopping:
            for name, p in list(self.procs):
                if p.poll() is not None and name in self.optional:
                    log(f"{name} exited with code {p.returncode}; the site keeps running without it")
                    self.procs.remove((name, p))
                elif p.poll() is not None:
                    log(f"{name} exited with code {p.returncode}; shutting down")
                    self.stop()
                    return p.returncode or 1
            time.sleep(1)
        return 0


def start_postgres(stack: Stack, data_dir: Path, cfg: dict, env: dict) -> str:
    pgdata = data_dir / "pgdata"
    sock = Path(PG_SOCKET_DIR)
    sock.mkdir(parents=True, exist_ok=True)
    own(sock)
    if not (pgdata / "PG_VERSION").exists():
        log(f"initialising database in {pgdata}")
        pgdata.mkdir(parents=True, exist_ok=True)
        own(pgdata)
        os.chmod(pgdata, 0o700)
        run([str(PG_BIN / "initdb"), "-D", str(pgdata), "-U", "jobhunt", "--auth=trust",
             "--encoding=UTF8", "--locale=C.UTF-8"], env=env, stdout=subprocess.DEVNULL)
    own(pgdata)  # a freshly attached disk may be owned by root
    stack.spawn("postgres", [
        str(PG_BIN / "postgres"), "-D", str(pgdata),
        "-c", "listen_addresses=",              # unix socket only: nothing exposed, even inside the network
        "-c", f"unix_socket_directories={sock}",
        "-c", f"shared_buffers={cfg.get('postgres_shared_buffers', '64MB')}",
        "-c", f"max_connections={int(cfg.get('postgres_max_connections', 40))}",
        "-c", f"work_mem={cfg.get('postgres_work_mem', '4MB')}",
        "-c", "maintenance_work_mem=32MB",
        "-c", "logging_collector=off",
    ], env)
    for _ in range(60):
        if subprocess.run([str(PG_BIN / "pg_isready"), "-h", str(sock), "-U", "jobhunt", "-d", "postgres"],
                          stdout=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(1)
    else:
        raise SystemExit("postgres did not become ready")
    exists = subprocess.run([str(PG_BIN / "psql"), "-h", str(sock), "-U", "jobhunt", "-d", "postgres", "-tAc",
                             "SELECT 1 FROM pg_database WHERE datname='jobhunt'"],
                            capture_output=True, text=True, check=True).stdout.strip()
    if exists != "1":
        run([str(PG_BIN / "createdb"), "-h", str(sock), "-U", "jobhunt", "jobhunt"], env=env)
    return f"postgresql+asyncpg://jobhunt@/jobhunt?host={sock}"


def main() -> int:
    cfg = load_server_cfg()
    data_dir = Path(cfg.get("data_dir") or "/app/data")
    data_dir.mkdir(parents=True, exist_ok=True)
    for f in SEED_DIR.glob("*.csv"):  # first start: seed the registry; later runs keep the (updated) copies
        if not (data_dir / f.name).exists():
            shutil.copy2(f, data_dir / f.name)
    own(data_dir)

    env = {**os.environ, "HOME": "/tmp"}
    env.setdefault("JOBHUNT_CONFIG", str(CONFIG))
    env["JOBHUNT_TRACKER"] = str(data_dir / "tracker.json")
    env["LOG_LEVEL"] = str(cfg.get("log_level") or "INFO").upper()
    env["DB_POOL_SIZE"] = str(int(cfg.get("db_pool_size") or 5))
    port = str(os.getenv("PORT") or cfg.get("port") or 10000)

    stack = Stack()
    signal.signal(signal.SIGTERM, stack.stop)
    signal.signal(signal.SIGINT, stack.stop)

    db_url = os.getenv("DATABASE_URL") or cfg.get("database_url") or ""
    if not db_url:
        db_url = start_postgres(stack, data_dir, cfg, env)
    else:
        log("using external database (DATABASE_URL / server.database_url)")
    env["DATABASE_URL"] = db_url

    if cfg.get("separate_worker", True):
        # Fetching/parsing runs in its own process so the site stays responsive while
        # thousands of boards are processed. The API needs only a few DB connections.
        web_env = {**env, "JOBHUNT_ROLE": "api", "DB_POOL_SIZE": "3"}
        worker_env = {**env, "JOBHUNT_ROLE": "worker"}
    else:  # one process does both (lowest memory)
        web_env, worker_env = {**env, "JOBHUNT_ROLE": "all"}, None
    stack.spawn("web", [
        sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", port,
        "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log",
    ], web_env)
    if worker_env is not None:
        stack.spawn("worker", [sys.executable, "-m", "app.worker"], worker_env)
    if cfg.get("discovery_enabled"):
        stack.spawn("discovery", [
            sys.executable, "-m", "app.discover", "--daemon",
            "--every-hours", str(cfg.get("discovery_every_hours", 24)),
            "--out", str(data_dir / "companies.discovered.csv"),
        ], env, optional=True)
    log(f"listening on port {port}")
    return stack.supervise()


if __name__ == "__main__":
    sys.exit(main())
