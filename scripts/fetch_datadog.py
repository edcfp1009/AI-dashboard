"""Fetch one UTC day of MCP server telemetry from Datadog Logs into a snapshot.

Usage: uv run scripts/fetch_datadog.py [--date YYYY-MM-DD]

Source of truth (verified 2026-08): service:freightpop-mcp emits structured
`mcp.tool.call` logs with attributes @tool, @outcome (success | user_error |
upstream_error | rate_limited), @company_id, @user_id, @mcp_session_id,
@duration_ms. NOTE: as of 2026-08 the structured build only runs in env:test;
prod (service:tms-mcp) is an older image with unstructured logs. Switch the
query filter once the new build ships to prod.

Env: DD_API_KEY, DD_APP_KEY, DD_SITE (default datadoghq.com)
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict

import requests

from common import day_bounds_utc, require_env, utc_yesterday, write_snapshot

# Flip to 'env:prod service:tms-mcp' once the structured-logging build is deployed to prod.
LOG_FILTER = "service:freightpop-mcp @event:mcp.tool.call"


def dd_aggregate(session: requests.Session, base: str, headers: dict, body: dict) -> list[dict]:
    r = session.post(f"{base}/api/v2/logs/analytics/aggregate", headers=headers, json=body, timeout=60)
    r.raise_for_status()
    return r.json().get("data", {}).get("buckets", [])


def fetch_day(date_str: str) -> dict:
    env = require_env("DD_API_KEY", "DD_APP_KEY")
    site = os.environ.get("DD_SITE", "datadoghq.com")
    base = f"https://api.{site}"
    headers = {"DD-API-KEY": env["DD_API_KEY"], "DD-APPLICATION-KEY": env["DD_APP_KEY"]}
    start, end = day_bounds_utc(date_str)
    time_filter = {"from": start.isoformat(), "to": end.isoformat(), "query": LOG_FILTER, "indexes": ["*"]}
    s = requests.Session()

    # 1) per tool x outcome: counts + latency percentiles
    buckets = dd_aggregate(s, base, headers, {
        "filter": time_filter,
        "group_by": [
            {"facet": "@tool", "limit": 100},
            {"facet": "@outcome", "limit": 10},
        ],
        "compute": [
            {"aggregation": "count"},
            {"aggregation": "pc50", "metric": "@duration_ms"},
            {"aggregation": "pc95", "metric": "@duration_ms"},
        ],
    })
    tools: dict[str, dict] = {}
    for b in buckets:
        by = b.get("by", {})
        tool, outcome = by.get("@tool"), by.get("@outcome", "success")
        if not tool:
            continue
        c = b.get("computes", {})
        t = tools.setdefault(tool, {"tool": tool, "calls": 0, "outcomes": defaultdict(int), "_lat": []})
        count = int(c.get("c0", 0) or 0)
        t["calls"] += count
        t["outcomes"][outcome] += count
        if c.get("c1") is not None:
            t["_lat"].append((count, float(c["c1"]), float(c.get("c2") or c["c1"])))

    # 2) per tool x account: call counts (for by_account + distinct tools per account)
    buckets = dd_aggregate(s, base, headers, {
        "filter": time_filter,
        "group_by": [
            {"facet": "@tool", "limit": 100},
            {"facet": "@company_id", "limit": 1000},
        ],
        "compute": [{"aggregation": "count"}],
    })
    acct_tools: dict[str, set] = defaultdict(set)
    for b in buckets:
        by = b.get("by", {})
        tool, acct = by.get("@tool"), by.get("@company_id")
        if not tool or not acct:
            continue
        count = int(b.get("computes", {}).get("c0", 0) or 0)
        tools.setdefault(tool, {"tool": tool, "calls": 0, "outcomes": defaultdict(int), "_lat": []})
        tools[tool].setdefault("by_account", []).append({"account_id": str(acct), "calls": count, "errors": None})
        acct_tools[str(acct)].add(tool)

    # 3) per account: distinct sessions + calls
    buckets = dd_aggregate(s, base, headers, {
        "filter": time_filter,
        "group_by": [{"facet": "@company_id", "limit": 1000}],
        "compute": [
            {"aggregation": "count"},
            {"aggregation": "cardinality", "metric": "@mcp_session_id"},
        ],
    })
    account_sessions = []
    for b in buckets:
        acct = b.get("by", {}).get("@company_id")
        if not acct:
            continue
        c = b.get("computes", {})
        account_sessions.append({
            "account_id": str(acct),
            "sessions": int(c.get("c1", 0) or 0),
            "distinct_tools": len(acct_tools.get(str(acct), ())),
        })

    # 4) distinct actors (account, user) — one bucket per pair
    buckets = dd_aggregate(s, base, headers, {
        "filter": time_filter,
        "group_by": [
            {"facet": "@company_id", "limit": 1000},
            {"facet": "@user_id", "limit": 1000},
        ],
        "compute": [{"aggregation": "count"}],
    })
    actors = []
    for b in buckets:
        by = b.get("by", {})
        if by.get("@company_id") and by.get("@user_id"):
            actors.append({"account_id": str(by["@company_id"]), "user_id": str(by["@user_id"])})

    # finalize tool records: weighted latency across outcome buckets
    tools_out = []
    for t in tools.values():
        lat = t.pop("_lat", [])
        total = sum(n for n, _, _ in lat) or 1
        t["latency_ms"] = {
            "p50": round(sum(n * p50 for n, p50, _ in lat) / total) if lat else None,
            "p95": round(sum(n * p95 for n, _, p95 in lat) / total) if lat else None,
        }
        t["outcomes"] = dict(t["outcomes"])
        t.setdefault("by_account", [])
        tools_out.append(t)

    return {"tools": tools_out, "account_sessions": account_sessions, "actors": actors}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=utc_yesterday())
    args = ap.parse_args()
    payload = fetch_day(args.date)
    path = write_snapshot("datadog", args.date, payload, mode="live")
    print(f"datadog {args.date}: {sum(t['calls'] for t in payload['tools'])} tool calls -> {path}")


if __name__ == "__main__":
    main()
