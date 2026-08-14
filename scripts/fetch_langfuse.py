"""Fetch one UTC day of Copilot + Accessorial Agent traces from Langfuse.

Usage: uv run scripts/fetch_langfuse.py [--date YYYY-MM-DD]

Expectations about instrumentation (verify in Phase 2; anything missing shows
up as an "instrumentation gap" card on the dashboard rather than a wrong number):
- copilot traces carry sessionId + userId, metadata.account_id, metadata.page,
  metadata.shipment_id; distinguished by trace name/tags containing "copilot"
- accessorial agent traces carry metadata.shipment_id + metadata.recommended,
  distinguished by name/tags containing "accessorial"

Env: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST (default cloud.langfuse.com)
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict

import requests

from common import day_bounds_utc, require_env, utc_yesterday, write_snapshot


def paginate(session: requests.Session, url: str, auth, params: dict):
    page = 1
    while True:
        r = session.get(url, auth=auth, params={**params, "page": page, "limit": 100}, timeout=60)
        r.raise_for_status()
        body = r.json()
        data = body.get("data", [])
        yield from data
        meta = body.get("meta", {})
        if page >= meta.get("totalPages", page):
            return
        page += 1


def classify(trace: dict) -> str | None:
    text = " ".join([str(trace.get("name") or ""), " ".join(trace.get("tags") or [])]).lower()
    if "accessorial" in text:
        return "accessorial"
    if "copilot" in text:
        return "copilot"
    return None


def fetch_day(date_str: str) -> dict:
    env = require_env("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    auth = (env["LANGFUSE_PUBLIC_KEY"], env["LANGFUSE_SECRET_KEY"])
    start, end = day_bounds_utc(date_str)
    window = {"fromTimestamp": start.isoformat(), "toTimestamp": end.isoformat()}
    s = requests.Session()

    sessions: dict[str, dict] = {}
    recommendations = []
    for tr in paginate(s, f"{host}/api/public/traces", auth, dict(window)):
        kind = classify(tr)
        md = tr.get("metadata") or {}
        if kind == "accessorial":
            recommendations.append({
                "trace_id": tr.get("id"),
                "shipment_id": md.get("shipment_id"),
                "account_id": md.get("account_id"),
                "user_id": tr.get("userId"),
                "recommended": md.get("recommended") or [],
            })
            continue
        if kind != "copilot":
            continue
        sid = tr.get("sessionId") or f"trace:{tr.get('id')}"
        cs = sessions.setdefault(sid, {
            "session_id": sid,
            "user_id": tr.get("userId"),
            "account_id": md.get("account_id"),
            "page": md.get("page"),
            "query_count": 0,
            "tool_call_count": 0,
            "shipment_ids": [],
            "started_at": tr.get("timestamp"),
            "ended_at": tr.get("timestamp"),
        })
        cs["query_count"] += 1
        if md.get("shipment_id") and md["shipment_id"] not in cs["shipment_ids"]:
            cs["shipment_ids"].append(md["shipment_id"])
        ts = tr.get("timestamp")
        if ts:
            cs["started_at"] = min(cs["started_at"] or ts, ts)
            cs["ended_at"] = max(cs["ended_at"] or ts, ts)

    # tool-call counts per session from TOOL observations
    tool_counts: dict[str, int] = defaultdict(int)
    try:
        for ob in paginate(s, f"{host}/api/public/observations", auth, {**window, "type": "TOOL"}):
            trace_id = ob.get("traceId")
            if trace_id:
                tool_counts[trace_id] += 1
        # observations reference traces; re-walk traces to map trace->session
        for tr in paginate(s, f"{host}/api/public/traces", auth, dict(window)):
            if classify(tr) == "copilot":
                sid = tr.get("sessionId") or f"trace:{tr.get('id')}"
                if sid in sessions:
                    sessions[sid]["tool_call_count"] += tool_counts.get(tr.get("id"), 0)
    except requests.HTTPError:
        pass  # older Langfuse versions may not support type=TOOL filter; counts stay 0

    return {
        "copilot_sessions": list(sessions.values()),
        "accessorial": {"recommendations": recommendations},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=utc_yesterday())
    args = ap.parse_args()
    payload = fetch_day(args.date)
    path = write_snapshot("langfuse", args.date, payload, mode="live")
    print(f"langfuse {args.date}: {len(payload['copilot_sessions'])} copilot sessions, "
          f"{len(payload['accessorial']['recommendations'])} accessorial recs -> {path}")


if __name__ == "__main__":
    main()
