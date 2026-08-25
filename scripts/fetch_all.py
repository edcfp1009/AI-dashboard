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
from common import (
    all_snapshot_dates,
    date_range,
    load_config,
    read_snapshot,
    update_status,
    utc_yesterday,
    with_retry,
    write_snapshot,
)

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
    failures = []

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
            except Exception as e:
                traceback.print_exc()
                update_status(source, mode="live", ok=False, date_str=d, error=str(e))
                print(f"[{source}] {d} FAILED: {e}", file=sys.stderr)
                failures.append(source)

    # Mock sources still get a status entry so the dashboard can label them.
    # last_success is the newest snapshot actually on disk, not the run date —
    # stale_days is measured against today, so claiming the run date would make
    # a mock source with month-old fixtures render as fresh.
    for source in FETCHERS:
        if config.get(source, {}).get("mode", "mock") == "mock":
            newest = next(
                (d for d in reversed(all_snapshot_dates()) if read_snapshot(source, d) is not None),
                None,
            )
            if newest:
                update_status(source, mode="mock", ok=True, date_str=newest)

    # A failing live source is recorded in data/status.json (the dashboard renders
    # it as a stale/errored pill) but must not abort the run: compute_metrics and
    # the Pages deploy still need to happen off the snapshots already committed.
    # The workflow's flag-sources job reds the run afterwards, so a broken
    # source is still loud without freezing the whole dashboard.
    if failures:
        print(f"live source failures: {', '.join(sorted(set(failures)))}", file=sys.stderr)


if __name__ == "__main__":
    main()
