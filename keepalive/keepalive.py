#!/usr/bin/env python3
"""Keep-alive poller: pings the deployed app so Render never puts it to sleep.

Runs OUTSIDE the app (it has to: a sleeping instance cannot wake itself), with no
dependencies beyond Python 3.9+. Settings come from the `keepalive` and `server`
sections of config/config.yaml.

  python3 keepalive/keepalive.py              # loop forever, one ping every interval_minutes
  python3 keepalive/keepalive.py --once       # single ping (for cron / GitHub Actions); exit 1 on failure
  python3 keepalive/keepalive.py --url https://jobs.example.com/api/health   # override the target

Env overrides: KEEPALIVE_URL, JOBHUNT_CONFIG (path to the config file).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


def _scalar(v: str):
    v = re.sub(r"\s+#.*$", "", v).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return v


def read_sections(path: Path, names: tuple[str, ...]) -> dict[str, dict]:
    """Read flat `key: value` pairs of top-level YAML sections. Uses PyYAML when installed,
    else a minimal parser that is sufficient for the `server` / `keepalive` sections."""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        raw = yaml.safe_load(text) or {}
        return {n: raw.get(n) or {} for n in names}
    except ImportError:
        pass
    out: dict[str, dict] = {n: {} for n in names}
    current = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            key = line.split(":", 1)[0].strip()
            current = key if key in names else None
            continue
        if current and (m := re.match(r"^\s+([A-Za-z_]\w*):\s*(.*)$", line)):
            out[current][m.group(1)] = _scalar(m.group(2))
    return out


def ping(url: str, timeout: float) -> tuple[bool, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "jobhunt-keepalive/1.0"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(256)
            return 200 <= r.status < 400, f"HTTP {r.status} in {time.monotonic() - started:.1f}s"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # network errors, timeouts
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.getenv("JOBHUNT_CONFIG", str(DEFAULT_CONFIG)))
    ap.add_argument("--url", default=os.getenv("KEEPALIVE_URL", ""))
    ap.add_argument("--once", action="store_true", help="ping once and exit (non-zero on failure)")
    args = ap.parse_args()

    cfg = read_sections(Path(args.config), ("server", "keepalive"))
    ka, server = cfg["keepalive"], cfg["server"]
    url = args.url or ka.get("url") or ""
    if not url:
        base = str(server.get("public_url") or "").rstrip("/")
        if not base:
            print("no URL: set keepalive.url or server.public_url in the config", file=sys.stderr)
            return 2
        url = base + "/api/health"
    interval = float(ka.get("interval_minutes") or 10) * 60
    timeout = float(ka.get("timeout_seconds") or 60)

    while True:
        ok, detail = ping(url, timeout)
        if not ok:  # one quick retry: the first request may only wake the instance
            time.sleep(15)
            ok, detail = ping(url, timeout)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        print(f"{stamp} {'ok  ' if ok else 'FAIL'} {url} {detail}", flush=True)
        if args.once:
            return 0 if ok else 1
        time.sleep(interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
