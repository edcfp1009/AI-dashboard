"""Orchestrator: fetch snapshots for every source per config/sources.json.

Usage:
  uv run scripts/fetch_all.py                       # yesterday (UTC)
  uv run scripts/fetch_all.py --date 2026-08-10     # a specific day
  uv run scripts/fetch_all.py --start 2026-07-01 --end 2026-08-10   # backfill
  uv run scripts/fetch_all.py --backfill-days 7     # last N days (CI input)

Per-source failure isolation: one source failing never blocks the others.
Exit code is 0 as long as every LIVE source either succeeded or at least one
source produced data (the dashboard shows staleness per source); 1 only if
every live source failed.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timedelta

import fetch_datadog
import fetch_langfuse
import fetch_mixpanel
from common import date_range, load_config, update_status, utc_yesterday, with_retry, write_snapshot

FETCHERS = {
    "datadog": fetch_datadog.fetch_day,
    "langfuse": fetch_langfuse.fetch_day,
    "mixpanel": fetch_mixpanel.fetch_day,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--backfill-days", type=int, default=0)
    args = ap.parse_args()

    if args.start and args.end:
        dates = date_range(args.start, args.end)
    elif args.backfill_days > 0:
        end = utc_yesterday()
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=args.backfill_days - 1)).strftime("%Y-%m-%d")
        dates = date_range(start, end)
    else:
        dates = [args.date or utc_yesterday()]

    config = load_config("sources.json")
    live_sources = [s for s, c in config.items() if c.get("mode") == "live"]
    successes, failures = [], []

    for source in FETCHERS:
        mode = config.get(source, {}).get("mode", "mock")
        if mode != "live":
            print(f"[{source}] mode={mode}, skipping fetch (snapshots already in repo)")
            continue
        for d in dates:
            try:
                payload = with_retry(lambda: FETCHERS[source](d))
                write_snapshot(source, d, payload, mode="live")
                update_status(source, mode="live", ok=True, date_str=d)
                print(f"[{source}] {d} OK")
                successes.append(source)
            except Exception as e:
                traceback.print_exc()
                update_status(source, mode="live", ok=False, date_str=d, error=str(e))
                print(f"[{source}] {d} FAILED: {e}", file=sys.stderr)
                failures.append(source)

    # mock sources still get a status entry so the dashboard can label them
    for source in FETCHERS:
        if config.get(source, {}).get("mode", "mock") == "mock":
            update_status(source, mode="mock", ok=True, date_str=dates[-1])

    if live_sources and not successes and failures:
        print("all live sources failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
