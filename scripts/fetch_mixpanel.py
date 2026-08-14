"""Fetch one UTC day of enablement roster + UI events from Mixpanel.

Usage: uv run scripts/fetch_mixpanel.py [--date YYYY-MM-DD]

- Events via raw export API (data.mixpanel.com). NOTE: from_date/to_date are
  interpreted in the PROJECT's timezone, so we fetch with a one-day pad and
  filter to the UTC day using each event's `time` property.
- Enablement roster via Engage API group/user profiles. If the roster turns
  out not to live in Mixpanel, drop a config/enablement.csv instead (columns:
  account_id,seats_enabled,enabled_date,enabled_users semicolon-separated) —
  this fetcher will prefer the CSV when present.

Env: MIXPANEL_SA_USERNAME, MIXPANEL_SA_SECRET, MIXPANEL_PROJECT_ID
Optional: MIXPANEL_DATA_HOST (default data.mixpanel.com), MIXPANEL_API_HOST (default mixpanel.com)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timedelta, timezone

import requests

from common import CONFIG_DIR, day_bounds_utc, require_env, utc_yesterday, write_snapshot

EVENTS = [
    "copilot_opened",
    "manual_task_completed",
    "accessorial_validator_clicked",
    "accessorial_accepted",
    "ltl_shipment_created",
]


def fetch_events(auth, project_id: str, date_str: str) -> list[dict]:
    data_host = os.environ.get("MIXPANEL_DATA_HOST", "data.mixpanel.com")
    start, end = day_bounds_utc(date_str)
    pad_from = (start - timedelta(days=1)).strftime("%Y-%m-%d")
    pad_to = (end + timedelta(days=1)).strftime("%Y-%m-%d")
    r = requests.get(
        f"https://{data_host}/api/2.0/export",
        auth=auth,
        params={"project_id": project_id, "from_date": pad_from, "to_date": pad_to, "event": json.dumps(EVENTS)},
        timeout=120,
        stream=True,
    )
    r.raise_for_status()
    out = []
    for line in r.iter_lines():
        if not line:
            continue
        ev = json.loads(line)
        props = ev.get("properties", {})
        ts = datetime.fromtimestamp(props.get("time", 0), tz=timezone.utc)
        if not (start <= ts < end):
            continue
        out.append({
            "event": ev.get("event"),
            "user_id": props.get("distinct_id"),
            "account_id": props.get("account_id") or props.get("company_id"),
            "shipment_id": props.get("shipment_id"),
            "page": props.get("page"),
            "ts": ts.isoformat(timespec="seconds"),
        })
    return out


def fetch_enablement_from_csv() -> list[dict] | None:
    path = CONFIG_DIR / "enablement.csv"
    if not path.exists():
        return None
    out = []
    with path.open() as f:
        for row in csv.DictReader(f):
            out.append({
                "account_id": row["account_id"],
                "seats_enabled": int(row["seats_enabled"] or 0),
                "enabled_users": [u for u in (row.get("enabled_users") or "").split(";") if u],
                "enabled_date": row.get("enabled_date") or None,
                "features": (row.get("features") or "copilot;mcp;accessorial").split(";"),
            })
    return out


def fetch_enablement_from_engage(auth, project_id: str) -> list[dict]:
    api_host = os.environ.get("MIXPANEL_API_HOST", "mixpanel.com")
    url = f"https://{api_host}/api/2.0/engage"
    out, page, session_id = [], 0, None
    while True:
        params = {"project_id": project_id, "where": 'defined(properties["ai_enabled_date"])'}
        if session_id:
            params.update({"session_id": session_id, "page": page})
        r = requests.post(url, auth=auth, params=params, timeout=60)
        r.raise_for_status()
        body = r.json()
        for rec in body.get("results", []):
            p = rec.get("$properties", {})
            out.append({
                "account_id": p.get("account_id") or rec.get("$distinct_id"),
                "seats_enabled": p.get("seats_enabled") or 0,
                "enabled_users": p.get("enabled_users") or [],
                "enabled_date": p.get("ai_enabled_date"),
                "features": p.get("ai_features") or ["copilot", "mcp", "accessorial"],
            })
        if not body.get("results") or len(body["results"]) < body.get("page_size", 1000):
            return out
        session_id, page = body.get("session_id"), page + 1


def fetch_day(date_str: str) -> dict:
    env = require_env("MIXPANEL_SA_USERNAME", "MIXPANEL_SA_SECRET", "MIXPANEL_PROJECT_ID")
    auth = (env["MIXPANEL_SA_USERNAME"], env["MIXPANEL_SA_SECRET"])
    enablement = fetch_enablement_from_csv()
    if enablement is None:
        enablement = fetch_enablement_from_engage(auth, env["MIXPANEL_PROJECT_ID"])
    return {"enablement": enablement, "events": fetch_events(auth, env["MIXPANEL_PROJECT_ID"], date_str)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=utc_yesterday())
    args = ap.parse_args()
    payload = fetch_day(args.date)
    path = write_snapshot("mixpanel", args.date, payload, mode="live")
    print(f"mixpanel {args.date}: {len(payload['enablement'])} accounts, {len(payload['events'])} events -> {path}")


if __name__ == "__main__":
    main()
