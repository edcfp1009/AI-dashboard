"""Compute all dashboard metrics from committed snapshots into site/metrics.json.

Usage: uv run scripts/compute_metrics.py

Pure function of data/snapshots/ — no network. Metrics whose inputs are missing
are emitted as null plus an entry in `gaps[]`; the frontend renders those as
"instrumentation gap" cards. Never fabricate a number.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from common import SITE_DIR, all_snapshot_dates, load_config, read_snapshot, read_status

WINDOW = 7  # days for "current" KPIs


def payload(source: str, date: str) -> dict | None:
    snap = read_snapshot(source, date)
    return snap["payload"] if snap else None


def ratio(num, den):
    return round(num / den, 4) if den else None


def last_n(dates: list[str], n: int) -> list[str]:
    return dates[-n:]


def main():
    dates = all_snapshot_dates()
    if not dates:
        raise SystemExit("no snapshots found — run generate_mock_data.py or fetch_all.py first")
    data_through = dates[-1]
    win = last_n(dates, WINDOW)
    prev_win = dates[-2 * WINDOW : -WINDOW] if len(dates) >= 2 * WINDOW else []
    gaps = []

    # ---------- load per-day payloads once ----------
    dd = {d: payload("datadog", d) for d in dates}
    lf = {d: payload("langfuse", d) for d in dates}
    mp = {d: payload("mixpanel", d) for d in dates}

    # ---------- MCP ----------
    def mcp_day(d):
        p = dd.get(d)
        if not p:
            return None
        calls = sum(t["calls"] for t in p["tools"])
        errors = sum(t["calls"] - t["outcomes"].get("success", 0) for t in p["tools"])
        return {"calls": calls, "errors": errors}

    mcp_series_calls, mcp_series_err = [], []
    for d in dates:
        day = mcp_day(d)
        if day:
            mcp_series_calls.append([d, day["calls"]])
            mcp_series_err.append([d, ratio(day["errors"], day["calls"]) or 0])

    def window_tools(window_dates):
        agg: dict[str, dict] = {}
        for d in window_dates:
            p = dd.get(d)
            if not p:
                continue
            for t in p["tools"]:
                a = agg.setdefault(t["tool"], {"calls": 0, "outcomes": Counter(), "lat": []})
                a["calls"] += t["calls"]
                a["outcomes"].update(t["outcomes"])
                if t["latency_ms"].get("p50") is not None:
                    a["lat"].append((t["calls"], t["latency_ms"]["p50"], t["latency_ms"].get("p95") or t["latency_ms"]["p50"]))
        return agg

    tools_agg = window_tools(win)
    spark_dates = last_n(dates, 14)
    spark_by_tool: dict[str, list] = defaultdict(list)
    for d in spark_dates:
        p = dd.get(d)
        day_tools = {t["tool"]: t["calls"] for t in p["tools"]} if p else {}
        for tool in tools_agg:
            spark_by_tool[tool].append(day_tools.get(tool, 0))

    tools_out = []
    for tool, a in sorted(tools_agg.items(), key=lambda kv: -kv[1]["calls"]):
        total_lat = sum(n for n, _, _ in a["lat"]) or 1
        errors = a["calls"] - a["outcomes"].get("success", 0)
        tools_out.append({
            "tool": tool,
            "calls_7d": a["calls"],
            "error_rate_7d": ratio(errors, a["calls"]) or 0,
            "outcomes_7d": dict(a["outcomes"]),
            "p50_ms": round(sum(n * p for n, p, _ in a["lat"]) / total_lat) if a["lat"] else None,
            "p95_ms": round(sum(n * p for n, _, p in a["lat"]) / total_lat) if a["lat"] else None,
            "spark": spark_by_tool[tool],
        })

    def mcp_window_summary(window_dates):
        calls = errors = sessions = 0
        acct_tools: dict[str, int] = {}
        accts = set()
        for d in window_dates:
            p = dd.get(d)
            if not p:
                continue
            for t in p["tools"]:
                calls += t["calls"]
                errors += t["calls"] - t["outcomes"].get("success", 0)
            for s in p.get("account_sessions", []):
                sessions += s.get("sessions") or 0
                accts.add(s["account_id"])
                acct_tools[s["account_id"]] = max(acct_tools.get(s["account_id"], 0), s.get("distinct_tools") or 0)
        return calls, errors, sessions, accts, acct_tools

    calls7, errors7, sessions7, accts7, acct_tools7 = mcp_window_summary(win)
    pcalls7, perrors7, *_ = mcp_window_summary(prev_win) if prev_win else (0, 0, 0, set(), {})
    mcp = {
        "current": {
            "calls_7d": calls7,
            "calls_prev_7d": pcalls7 or None,
            "error_rate_7d": ratio(errors7, calls7),
            "error_rate_prev_7d": ratio(perrors7, pcalls7),
            "active_accounts_7d": len(accts7),
            "calls_per_connection_7d": ratio(calls7, sessions7),
            "avg_distinct_tools_per_account_7d": ratio(sum(acct_tools7.values()), len(acct_tools7)),
        },
        "series": {"calls": mcp_series_calls, "error_rate": mcp_series_err},
        "tools": tools_out,
    }
    if sessions7 == 0 and calls7 > 0:
        mcp["current"]["calls_per_connection_7d"] = None
        gaps.append({"metric": "MCP calls per connection", "reason": "No session/connection counts in Datadog snapshots.", "needs": "@mcp_session_id cardinality per account (fetch_datadog query #3)"})

    # ---------- Copilot ----------
    def copilot_window(window_dates):
        sessions, queries, tool_calls = 0, 0, 0
        users = set()
        shipments = set()
        with_shipment = 0
        fallback = 0
        pages = Counter()
        manual_by_user: dict[str, list] = defaultdict(list)
        manual_events_exist = False
        for d in window_dates:
            m = mp.get(d)
            if m:
                for ev in m.get("events", []):
                    if ev["event"] == "manual_task_completed":
                        manual_events_exist = True
                        manual_by_user[ev.get("user_id")].append(ev)
        for d in window_dates:
            p = lf.get(d)
            if not p:
                continue
            for cs in p.get("copilot_sessions", []):
                sessions += 1
                queries += cs.get("query_count") or 0
                tool_calls += cs.get("tool_call_count") or 0
                if cs.get("user_id"):
                    users.add(cs["user_id"])
                if cs.get("page"):
                    pages[cs["page"]] += 1
                sids = cs.get("shipment_ids") or []
                shipments.update(sids)
                if sids:
                    with_shipment += 1
                    ended = cs.get("ended_at")
                    for ev in manual_by_user.get(cs.get("user_id"), []):
                        same_shipment = ev.get("shipment_id") and ev["shipment_id"] in sids
                        close_in_time = False
                        if ended and ev.get("ts"):
                            try:
                                dt_end = datetime.fromisoformat(ended)
                                dt_ev = datetime.fromisoformat(ev["ts"])
                                close_in_time = timedelta(0) <= dt_ev - dt_end <= timedelta(minutes=30)
                            except ValueError:
                                pass
                        if same_shipment or close_in_time:
                            fallback += 1
                            break
        return {
            "sessions": sessions, "queries": queries, "tool_calls": tool_calls,
            "users": users, "shipments": shipments, "with_shipment": with_shipment,
            "fallback": fallback, "pages": pages, "manual_events_exist": manual_events_exist,
        }

    c7 = copilot_window(win)
    cprev = copilot_window(prev_win) if prev_win else None
    copilot_series_sessions, copilot_series_qps, copilot_series_toolcalls = [], [], []
    for d in dates:
        cd = copilot_window([d])
        copilot_series_sessions.append([d, cd["sessions"]])
        copilot_series_toolcalls.append([d, cd["tool_calls"]])
        copilot_series_qps.append([d, ratio(cd["queries"], cd["sessions"]) or 0])

    fallback_rate = ratio(c7["fallback"], c7["with_shipment"])
    if not c7["manual_events_exist"]:
        fallback_rate = None
        gaps.append({"metric": "Copilot fallback rate", "reason": "No `manual_task_completed` events found in Mixpanel data.", "needs": "Mixpanel event `manual_task_completed` with user_id + shipment_id"})
    elif c7["with_shipment"] == 0 and c7["sessions"] > 0:
        fallback_rate = None
        gaps.append({"metric": "Copilot fallback rate / sessions per shipment", "reason": "Copilot sessions carry no shipment_id in Langfuse metadata.", "needs": "metadata.shipment_id on copilot traces"})

    sessions_per_shipment = ratio(c7["sessions"], len(c7["shipments"]))
    if not c7["shipments"] and c7["sessions"] > 0:
        sessions_per_shipment = None

    by_page = [{"page": p, "sessions_7d": n, "share": ratio(n, c7["sessions"])} for p, n in c7["pages"].most_common()]
    if not by_page and c7["sessions"] > 0:
        gaps.append({"metric": "Copilot usage per page", "reason": "Copilot sessions carry no page in Langfuse metadata.", "needs": "metadata.page on copilot traces"})

    copilot = {
        "current": {
            "sessions_7d": c7["sessions"],
            "sessions_prev_7d": cprev["sessions"] if cprev else None,
            "queries_per_session_7d": ratio(c7["queries"], c7["sessions"]),
            "tool_calls_7d": c7["tool_calls"],
            "tool_calls_prev_7d": cprev["tool_calls"] if cprev else None,
            "sessions_per_shipment_7d": sessions_per_shipment,
            "fallback_rate_7d": fallback_rate,
            "fallback_rate_prev_7d": (ratio(cprev["fallback"], cprev["with_shipment"]) if cprev and c7["manual_events_exist"] else None),
            "active_users_7d": len(c7["users"]),
        },
        "series": {
            "sessions": copilot_series_sessions,
            "tool_calls": copilot_series_toolcalls,
            "queries_per_session": copilot_series_qps,
        },
        "by_page": by_page,
    }

    # ---------- Accessorial Agent ----------
    denominators = load_config("denominators.json")

    def accessorial_window(window_dates):
        recs, accepted, validator = [], 0, 0
        ltl = 0
        ltl_events_exist = False
        rec_ids = set()
        for d in window_dates:
            p = lf.get(d)
            if p:
                for r in p.get("accessorial", {}).get("recommendations", []):
                    recs.append(r)
                    if r.get("shipment_id"):
                        rec_ids.add(r["shipment_id"])
            m = mp.get(d)
            if m:
                for ev in m.get("events", []):
                    if ev["event"] == "accessorial_validator_clicked" and (not rec_ids or ev.get("shipment_id") in rec_ids or ev.get("shipment_id") is None):
                        validator += 1
                    elif ev["event"] == "accessorial_accepted":
                        accepted += 1
                    elif ev["event"] == "ltl_shipment_created":
                        ltl += 1
                        ltl_events_exist = True
        return recs, accepted, validator, ltl, ltl_events_exist

    recs7, accepted7, validator7, ltl7, ltl_exist = accessorial_window(win)
    acc_series_recs, acc_series_accept = [], []
    for d in dates:
        r, a, _, _, _ = accessorial_window([d])
        acc_series_recs.append([d, len(r)])
        acc_series_accept.append([d, ratio(a, len(r)) or 0])

    if ltl_exist:
        ltl_den = ltl7
    elif denominators.get("ltl_shipments_per_day"):
        ltl_den = denominators["ltl_shipments_per_day"] * len(win)
    else:
        ltl_den = None
        gaps.append({"metric": "Recommendations per LTL shipment", "reason": "Total LTL shipment count is not available in any connected source.", "needs": "Mixpanel event `ltl_shipment_created` (or set ltl_shipments_per_day in config/denominators.json)"})

    accessorial = {
        "current": {
            "recommendations_7d": len(recs7),
            "ltl_shipments_7d": ltl_den,
            "recs_per_ltl_7d": ratio(len(recs7), ltl_den) if ltl_den else None,
            "acceptance_rate_7d": ratio(accepted7, len(recs7)),
            "validator_click_rate_7d": ratio(validator7, len(recs7)),
        },
        "series": {"recommendations": acc_series_recs, "acceptance_rate": acc_series_accept},
    }

    # ---------- Adoption ----------
    # activity per account/user per day, across all sources
    first_activity_by_account: dict[str, str] = {}
    active_users_by_day: dict[str, set] = defaultdict(set)
    active_accounts_by_day: dict[str, set] = defaultdict(set)
    for d in dates:
        p = dd.get(d)
        if p:
            for actor in p.get("actors", []):
                active_users_by_day[d].add(actor["user_id"])
                active_accounts_by_day[d].add(actor["account_id"])
        q = lf.get(d)
        if q:
            for cs in q.get("copilot_sessions", []):
                if cs.get("user_id"):
                    active_users_by_day[d].add(cs["user_id"])
                if cs.get("account_id"):
                    active_accounts_by_day[d].add(cs["account_id"])
        for acct in active_accounts_by_day[d]:
            first_activity_by_account.setdefault(acct, d)

    latest_mp = next((mp[d] for d in reversed(dates) if mp.get(d)), None)
    enablement = latest_mp.get("enablement", []) if latest_mp else []
    adoption = {"current": {}, "series": {}}
    if not enablement:
        gaps.append({"metric": "Adoption (zero-use %, active % of seats)", "reason": "No enablement roster (accounts/seats enabled) available.", "needs": "Mixpanel profiles with ai_enabled_date + seats_enabled, or config/enablement.csv"})
        adoption["current"] = {"enabled_accounts": None, "total_seats": None, "active_pct_of_seats_30d": None, "zero_use_7d_pct": None, "zero_use_30d_pct": None}
    else:
        total_seats = sum(a.get("seats_enabled") or 0 for a in enablement)
        win30 = last_n(dates, 30)
        active_users_30d = set().union(*(active_users_by_day[d] for d in win30)) if win30 else set()
        enabled_user_set = set()
        for a in enablement:
            enabled_user_set.update(a.get("enabled_users") or [])
        # count only active users who are on the enabled roster when the roster lists users
        active_enabled_30d = active_users_30d & enabled_user_set if enabled_user_set else active_users_30d

        def zero_use_pct(cohort_days: int):
            today = datetime.strptime(data_through, "%Y-%m-%d")
            eligible = zero = 0
            for a in enablement:
                if not a.get("enabled_date"):
                    continue
                enabled = datetime.strptime(a["enabled_date"], "%Y-%m-%d")
                if (today - enabled).days < cohort_days:
                    continue  # cohort not mature yet
                if a["enabled_date"] < dates[0]:
                    continue  # first-N-days window predates our data — can't judge
                eligible += 1
                first = first_activity_by_account.get(a["account_id"])
                cutoff = (enabled + timedelta(days=cohort_days)).strftime("%Y-%m-%d")
                if first is None or first > cutoff:
                    zero += 1
            return ratio(zero, eligible), eligible

        z7, elig7 = zero_use_pct(7)
        z30, elig30 = zero_use_pct(30)
        adoption["current"] = {
            "enabled_accounts": len(enablement),
            "total_seats": total_seats,
            "active_users_30d": len(active_enabled_30d),
            "active_pct_of_seats_30d": ratio(len(active_enabled_30d), total_seats),
            "zero_use_7d_pct": z7,
            "zero_use_7d_cohort": elig7,
            "zero_use_30d_pct": z30,
            "zero_use_30d_cohort": elig30,
        }
        series_active_pct = []
        for i, d in enumerate(dates):
            trailing = dates[max(0, i - 29) : i + 1]
            users = set().union(*(active_users_by_day[x] for x in trailing))
            if enabled_user_set:
                users &= enabled_user_set
            series_active_pct.append([d, ratio(len(users), total_seats) or 0])
        adoption["series"] = {
            "active_pct_of_seats_30d": series_active_pct,
            "active_users": [[d, len(active_users_by_day[d])] for d in dates],
        }

    # ---------- envelope ----------
    status = read_status()
    config = load_config("sources.json")
    source_status = {}
    for source in ("datadog", "langfuse", "mixpanel"):
        st = status.get(source, {})
        mode = config.get(source, {}).get("mode", "mock")
        last = st.get("last_success")
        stale = None
        if last:
            stale = (datetime.strptime(data_through, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days
        source_status[source] = {"mode": mode, "last_success": last, "stale_days": stale, "last_error": st.get("last_error")}

    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_through": data_through,
        "window_days": WINDOW,
        "source_status": source_status,
        "adoption": adoption,
        "copilot": copilot,
        "mcp": mcp,
        "accessorial": accessorial,
        "gaps": gaps,
    }
    out = SITE_DIR / "metrics.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metrics, indent=1) + "\n")
    print(f"wrote {out} (data through {data_through}, {len(gaps)} gaps)")


if __name__ == "__main__":
    main()
