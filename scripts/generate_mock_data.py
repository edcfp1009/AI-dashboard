"""Generate deterministic mock snapshots for all three sources.

Usage: uv run scripts/generate_mock_data.py --days 60 [--seed 42] [--end YYYY-MM-DD]

The same fake universe of accounts/users is used across datadog, langfuse and
mixpanel snapshots so cross-source joins (fallback rate, adoption cohorts) can
be exercised before any real API is connected.

Intentionally left missing (to demo the "instrumentation gap" card):
- no `ltl_shipment_created` events in Mixpanel and no manual denominator, so
  "recommendations per LTL shipment" computes to a gap.
"""
from __future__ import annotations

import argparse
import math
import random
from datetime import datetime, timedelta, timezone

from common import utc_yesterday, write_snapshot

MCP_TOOLS = [
    # (tool, base daily calls, error_rate, p50_ms)
    ("get_rates", 30, 0.03, 2800),
    ("get_carriers", 22, 0.02, 320),
    ("get_tracking", 26, 0.05, 450),
    ("freightpop_create_shipment", 12, 0.06, 5200),
    ("freightpop_get_shipment_details", 14, 0.03, 380),
    ("freightpop_import_orders", 8, 0.22, 7400),  # deliberately unhealthy
    ("get_packaging_rules", 6, 0.04, 900),
    ("freightpop_validate_address", 9, 0.02, 610),
    ("search_wms_picking_status", 5, 0.07, 540),
    ("get_wms_inventory_by_item", 7, 0.05, 700),
]

COPILOT_PAGES = ["quoting", "shipments", "tracking", "orders", "reports"]


def build_universe(rng: random.Random, end: datetime, n_accounts: int = 18):
    accounts = []
    uid = 0
    for i in range(n_accounts):
        seats = rng.randint(4, 40)
        users = [f"u_{uid + j:04d}" for j in range(seats)]
        uid += seats
        enabled_offset = rng.randint(5, 95)  # days before end
        # ~20% of accounts never activate (feeds the zero-use cohort metric)
        dormant = rng.random() < 0.2
        accounts.append(
            {
                "account_id": f"acct_{i + 1:03d}",
                "seats": seats,
                "users": users,
                "enabled_date": (end - timedelta(days=enabled_offset)).strftime("%Y-%m-%d"),
                "dormant": dormant,
                # fraction of seats that actually use the features, grows over time
                "engagement": rng.uniform(0.15, 0.75),
            }
        )
    return accounts


def active_accounts_on(accounts, date: datetime, rng: random.Random):
    out = []
    for a in accounts:
        enabled = datetime.strptime(a["enabled_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if date < enabled or a["dormant"]:
            continue
        # slow adoption ramp over the first 3 weeks after enablement
        ramp = min(1.0, max(0.1, (date - enabled).days / 21))
        if rng.random() < 0.85 * ramp:
            out.append((a, ramp))
    return out


def weekday_factor(date: datetime) -> float:
    return 0.25 if date.weekday() >= 5 else 1.0


def gen_day(accounts, date: datetime, day_index: int, total_days: int, rng: random.Random):
    date_str = date.strftime("%Y-%m-%d")
    wf = weekday_factor(date)
    growth = 0.6 + 0.4 * (day_index / max(1, total_days - 1))  # overall usage climbing
    active = active_accounts_on(accounts, date, rng)

    # ---------------- datadog (MCP server) ----------------
    tools_out = []
    account_calls: dict[str, dict] = {}
    actors = set()
    # latency regression on get_rates in the middle third of the series
    regression = total_days // 3 <= day_index < 2 * total_days // 3
    for tool, base, err_rate, p50 in MCP_TOOLS:
        calls = max(0, round(base * wf * growth * rng.uniform(0.7, 1.3) * len(active) / 10))
        if calls == 0:
            continue
        errors = sum(1 for _ in range(calls) if rng.random() < err_rate)
        upstream = round(errors * 0.6)
        user_err = round(errors * 0.3)
        rate_lim = errors - upstream - user_err
        p50_eff = p50 * (2.2 if (tool == "get_rates" and regression) else 1.0)
        by_account = []
        # spread calls across active accounts
        weights = [max(0.05, a["engagement"]) * r for a, r in active]
        total_w = sum(weights) or 1
        for (a, _r), w in zip(active, weights):
            share = round(calls * w / total_w)
            if share == 0:
                continue
            by_account.append({"account_id": a["account_id"], "calls": share, "errors": min(share, round(errors * w / total_w))})
            acc = account_calls.setdefault(a["account_id"], {"calls": 0, "sessions": 0, "tools": set(), "users": set()})
            acc["calls"] += share
            acc["tools"].add(tool)
            n_users = max(1, round(len(a["users"]) * a["engagement"] * 0.3))
            for u in rng.sample(a["users"], min(n_users, len(a["users"]))):
                acc["users"].add(u)
                actors.add((a["account_id"], u))
        tools_out.append(
            {
                "tool": tool,
                "calls": calls,
                "outcomes": {
                    "success": calls - errors,
                    "user_error": user_err,
                    "upstream_error": upstream,
                    "rate_limited": rate_lim,
                },
                "latency_ms": {
                    "p50": round(p50_eff * rng.uniform(0.85, 1.15)),
                    "p95": round(p50_eff * rng.uniform(2.5, 4.5)),
                },
                "by_account": by_account,
            }
        )
    account_sessions = []
    for acct_id, acc in account_calls.items():
        acc["sessions"] = max(1, round(acc["calls"] / rng.uniform(4, 9)))
        account_sessions.append({"account_id": acct_id, "sessions": acc["sessions"], "distinct_tools": len(acc["tools"])})
    datadog_payload = {
        "tools": tools_out,
        "account_sessions": account_sessions,
        "actors": [{"account_id": a, "user_id": u} for a, u in sorted(actors)],
    }

    # ---------------- langfuse (copilot + accessorial) ----------------
    copilot_sessions = []
    accessorial_recs = []
    shipment_seq = 0
    for a, ramp in active:
        n_users = max(1, round(len(a["users"]) * a["engagement"] * ramp * wf * 0.4))
        for u in rng.sample(a["users"], min(n_users, len(a["users"]))):
            for s in range(rng.choice([1, 1, 1, 2])):
                shipment_seq += 1
                sid = f"shp_{date_str}_{shipment_seq:04d}"
                start = date + timedelta(hours=rng.uniform(13, 23))  # US business hours in UTC
                dur = rng.uniform(2, 18)
                copilot_sessions.append(
                    {
                        "session_id": f"cs_{date_str}_{a['account_id']}_{u}_{s}",
                        "user_id": u,
                        "account_id": a["account_id"],
                        "page": rng.choices(COPILOT_PAGES, weights=[5, 4, 3, 2, 1])[0],
                        "query_count": rng.randint(1, 8),
                        "tool_call_count": rng.randint(0, 12),
                        "shipment_ids": [sid] if rng.random() < 0.7 else [],
                        "started_at": start.isoformat(timespec="seconds"),
                        "ended_at": (start + timedelta(minutes=dur)).isoformat(timespec="seconds"),
                    }
                )
                if rng.random() < 0.30:  # some sessions are about an LTL shipment -> agent ran
                    accessorial_recs.append(
                        {
                            "trace_id": f"ar_{date_str}_{shipment_seq:04d}",
                            "shipment_id": sid,
                            "account_id": a["account_id"],
                            "user_id": u,
                            "recommended": rng.sample(["liftgate", "residential", "inside_delivery", "limited_access"], rng.randint(1, 2)),
                        }
                    )
    langfuse_payload = {
        "copilot_sessions": copilot_sessions,
        "accessorial": {"recommendations": accessorial_recs},
    }

    # ---------------- mixpanel (enablement + UI events) ----------------
    enablement = [
        {
            "account_id": a["account_id"],
            "seats_enabled": a["seats"],
            "enabled_users": a["users"],
            "enabled_date": a["enabled_date"],
            "features": ["copilot", "mcp", "accessorial"],
        }
        for a in accounts
        if datetime.strptime(a["enabled_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc) <= date
    ]
    events = []
    for cs in copilot_sessions:
        events.append({"event": "copilot_opened", "user_id": cs["user_id"], "account_id": cs["account_id"], "shipment_id": (cs["shipment_ids"] or [None])[0], "page": cs["page"], "ts": cs["started_at"]})
        # fallback: user finishes the task manually shortly after a copilot session (~18%)
        if cs["shipment_ids"] and rng.random() < 0.18:
            ts = datetime.fromisoformat(cs["ended_at"]) + timedelta(minutes=rng.uniform(1, 25))
            events.append({"event": "manual_task_completed", "user_id": cs["user_id"], "account_id": cs["account_id"], "shipment_id": cs["shipment_ids"][0], "page": cs["page"], "ts": ts.isoformat(timespec="seconds")})
    for rec in accessorial_recs:
        base_ts = date + timedelta(hours=rng.uniform(13, 23))
        if rng.random() < 0.55:  # click into validator
            events.append({"event": "accessorial_validator_clicked", "user_id": rec["user_id"], "account_id": rec["account_id"], "shipment_id": rec["shipment_id"], "page": "quoting", "ts": base_ts.isoformat(timespec="seconds")})
            if rng.random() < 0.6:  # then accept
                events.append({"event": "accessorial_accepted", "user_id": rec["user_id"], "account_id": rec["account_id"], "shipment_id": rec["shipment_id"], "page": "quoting", "ts": (base_ts + timedelta(minutes=2)).isoformat(timespec="seconds")})
    mixpanel_payload = {"enablement": enablement, "events": events}

    return datadog_payload, langfuse_payload, mixpanel_payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--end", default=utc_yesterday(), help="last mock day (YYYY-MM-DD, UTC)")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    accounts = build_universe(rng, end)

    for i in range(args.days):
        date = end - timedelta(days=args.days - 1 - i)
        dd, lf, mp = gen_day(accounts, date, i, args.days, rng)
        d = date.strftime("%Y-%m-%d")
        write_snapshot("datadog", d, dd, mode="mock")
        write_snapshot("langfuse", d, lf, mode="mock")
        write_snapshot("mixpanel", d, mp, mode="mock")
    print(f"wrote {args.days} days of mock snapshots ending {args.end}")


if __name__ == "__main__":
    main()
