"""Print sample account/user IDs from each source side-by-side.

Usage: uv run scripts/audit_ids.py

Run this before trusting any cross-source metric (fallback rate, adoption).
If the same human shows up with different id formats across sources (e.g.
email in Langfuse, GUID in Mixpanel), create config/id_map.json instead of
fuzzy matching.
"""
from __future__ import annotations

from common import all_snapshot_dates, read_snapshot


def main():
    dates = all_snapshot_dates()
    if not dates:
        raise SystemExit("no snapshots")
    latest = dates[-1]
    samples = {"datadog": {"accounts": set(), "users": set()},
               "langfuse": {"accounts": set(), "users": set()},
               "mixpanel": {"accounts": set(), "users": set()}}
    for d in reversed(dates[-7:]):
        dd = read_snapshot("datadog", d)
        if dd:
            for a in dd["payload"].get("actors", []):
                samples["datadog"]["accounts"].add(a["account_id"])
                samples["datadog"]["users"].add(a["user_id"])
        lf = read_snapshot("langfuse", d)
        if lf:
            for cs in lf["payload"].get("copilot_sessions", []):
                if cs.get("account_id"):
                    samples["langfuse"]["accounts"].add(cs["account_id"])
                if cs.get("user_id"):
                    samples["langfuse"]["users"].add(cs["user_id"])
        mp = read_snapshot("mixpanel", d)
        if mp:
            for a in mp["payload"].get("enablement", []):
                samples["mixpanel"]["accounts"].add(a["account_id"])
                samples["mixpanel"]["users"].update((a.get("enabled_users") or [])[:3])

    print(f"ID samples from snapshots up to {latest}\n")
    for source, s in samples.items():
        print(f"[{source}]")
        print(f"  accounts: {sorted(s['accounts'])[:10]}")
        print(f"  users:    {sorted(s['users'])[:10]}\n")

    dd_u, lf_u, mp_u = (samples[s]["users"] for s in ("datadog", "langfuse", "mixpanel"))
    if lf_u and mp_u:
        overlap = len(lf_u & mp_u)
        print(f"langfuse ∩ mixpanel users: {overlap} (needed for fallback rate join)")
    if dd_u and mp_u:
        print(f"datadog ∩ mixpanel users: {len(dd_u & mp_u)} (needed for adoption join)")


if __name__ == "__main__":
    main()
