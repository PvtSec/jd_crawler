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


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def memory_limit_mb(configured: int) -> int:
    """The container's memory limit: server.memory_limit_mb, else the cgroup limit, else 512."""
    if configured:
        return configured
    raw = _read("/sys/fs/cgroup/memory.max") or _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if raw and raw.isdigit() and int(raw) < 2**50:
        return int(raw) // 2**20
    return 512


def memory_used_mb() -> float | None:
    """Memory the kernel can't reclaim (process memory + shared memory), i.e. what triggers
    the OOM killer. Page cache is excluded: it is dropped automatically under pressure."""
    stat = _read("/sys/fs/cgroup/memory.stat")
    keys = ("anon", "shmem")
    if stat is None:  # cgroup v1
        stat, keys = _read("/sys/fs/cgroup/memory/memory.stat"), ("total_rss", "total_shmem")
    if stat is None:
        return None
    vals = dict(line.split()[:2] for line in stat.splitlines() if " " in line)
    return sum(int(vals.get(k, 0)) for k in keys) / 2**20


class Stack:
    def __init__(self) -> None:
        self.procs: list[tuple[str, subprocess.Popen]] = []
        self.optional: set[str] = set()
        self.restartable: dict[str, tuple[list[str], dict]] = {}
        self.restarts: dict[str, float] = {}
        self.stopping = False
        self.mem_limit = 512
        self.mem_guard_pct = 85
        self.last_mem_restart = 0.0

    def spawn(self, name: str, cmd: list[str], env: dict, optional: bool = False,
              restartable: bool = False) -> subprocess.Popen:
        log(f"starting {name}")
        if optional:
            self.optional.add(name)
        if restartable:
            self.restartable[name] = (cmd, env)
        p = subprocess.Popen(cmd, env=env, preexec_fn=demote())
        self.procs.append((name, p))
        return p

    def restart(self, name: str, reason: str) -> None:
        """Stop and start one process; the rest of the stack (and the site) keeps running."""
        for entry in list(self.procs):
            n, p = entry
            if n != name:
                continue
            log(f"restarting {name}: {reason}")
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
            self.procs.remove(entry)
            cmd, env = self.restartable[name]
            self.spawn(name, cmd, env)
            self.restarts[name] = time.monotonic()

    def guard_memory(self) -> None:
        """Before the container reaches its memory limit (where the platform would kill and
        restart everything), restart the process holding the most memory that is safe to
        restart: the fetch worker. It picks up where it left off; the site stays up."""
        used = memory_used_mb()
        if used is None or used < self.mem_limit * self.mem_guard_pct / 100:
            return
        if time.monotonic() - self.last_mem_restart < 120:  # give the restart time to take effect
            return
        self.last_mem_restart = time.monotonic()
        running = [n for n, p in self.procs if p.poll() is None]
        for name in ("discovery", "worker", "web"):  # least to most user-visible
            if name in running and name in self.restartable:
                self.restart(name, f"memory {used:.0f} MB of {self.mem_limit} MB limit")
                return

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
            self.guard_memory()
            for name, p in list(self.procs):
                if p.poll() is not None and name in self.restartable and name != "web":
                    # A crashed worker is restarted on its own (at most once a minute).
                    if time.monotonic() - self.restarts.get(name, 0) > 60:
                        self.restart(name, f"exited with code {p.returncode}")
                elif p.poll() is not None and name in self.optional:
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
    stack.mem_limit = memory_limit_mb(int(cfg.get("memory_limit_mb") or 0))
    stack.mem_guard_pct = int(cfg.get("memory_guard_pct") or 85)
    log(f"memory limit {stack.mem_limit} MB; the fetch worker is restarted above {stack.mem_guard_pct}%")
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
    ], web_env, restartable=worker_env is None)  # single-process mode: last-resort memory restart
    if worker_env is not None:
        stack.spawn("worker", [sys.executable, "-m", "app.worker"], worker_env, restartable=True)
    if cfg.get("discovery_enabled"):
        stack.spawn("discovery", [
            sys.executable, "-m", "app.discover", "--daemon",
            "--every-hours", str(cfg.get("discovery_every_hours", 24)),
            "--out", str(data_dir / "companies.discovered.csv"),
        ], env, optional=True, restartable=True)
    log(f"listening on port {port}")
    return stack.supervise()


if __name__ == "__main__":
    sys.exit(main())
