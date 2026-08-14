"""Shared helpers: snapshot IO, date handling, retry, status tracking."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
STATUS_FILE = DATA_DIR / "status.json"
CONFIG_DIR = ROOT / "config"
SITE_DIR = ROOT / "site"

SOURCES = ("datadog", "langfuse", "mixpanel")


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def utc_yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def date_range(start: str, end: str) -> list[str]:
    """Inclusive list of YYYY-MM-DD strings from start to end."""
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    return [(s + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((e - s).days + 1)]


def day_bounds_utc(date_str: str) -> tuple[datetime, datetime]:
    """[start, end) datetimes in UTC for a metric day."""
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def load_config(name: str) -> dict:
    return json.loads((CONFIG_DIR / name).read_text())


def snapshot_path(source: str, date_str: str) -> Path:
    return SNAPSHOT_DIR / date_str / f"{source}.json"


def write_snapshot(source: str, date_str: str, payload: dict, mode: str) -> Path:
    path = snapshot_path(source, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "source": source,
        "date": date_str,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "payload": payload,
    }
    path.write_text(json.dumps(envelope, indent=1, sort_keys=True) + "\n")
    return path


def read_snapshot(source: str, date_str: str) -> dict | None:
    path = snapshot_path(source, date_str)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def all_snapshot_dates() -> list[str]:
    if not SNAPSHOT_DIR.exists():
        return []
    return sorted(p.name for p in SNAPSHOT_DIR.iterdir() if p.is_dir())


def update_status(source: str, *, mode: str, ok: bool, date_str: str, error: str | None = None) -> None:
    status = {}
    if STATUS_FILE.exists():
        status = json.loads(STATUS_FILE.read_text())
    entry = status.get(source, {})
    entry["mode"] = mode
    if ok:
        entry["last_success"] = date_str
        entry["last_error"] = None
    else:
        entry["last_error"] = f"{date_str}: {error}"
    status[source] = entry
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status, indent=1, sort_keys=True) + "\n")


def read_status() -> dict:
    if STATUS_FILE.exists():
        return json.loads(STATUS_FILE.read_text())
    return {}


def with_retry(fn, *, attempts: int = 3, base_delay: float = 2.0):
    """Run fn(); on exception retry with exponential backoff. Raises the last error."""
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (2**i))


def require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"missing env vars: {', '.join(missing)}")
    return {n: os.environ[n] for n in names}
