# FreightPOP AI Metrics Dashboard

Daily-refreshed usage dashboard for the AI product suite: **MCP platform**, **in-app Copilot**, and **Accessorial Agent**.

**How it works:** GitHub Actions runs every day at 13:30 UTC → fetches yesterday's data from Datadog / Langfuse / Mixpanel → commits normalized JSON snapshots to `data/snapshots/` → computes `site/metrics.json` → deploys the static dashboard to GitHub Pages. No server, no database.

## Local development

```bash
uv run scripts/generate_mock_data.py --days 60   # seed 60 days of mock snapshots
uv run scripts/fetch_all.py                      # fetch live sources (mock sources are skipped)
uv run scripts/compute_metrics.py                # snapshots -> site/metrics.json
python3 -m http.server -d site 8000              # open http://localhost:8000
```

`fetch()` fails on `file://`, so always preview through the local server.

## Switching a source from mock to live

Each source flips independently — no code changes:

1. Get the keys (see `.env.example` for where each key is created).
2. Add them as **repo secrets** (Settings → Secrets and variables → Actions). Secrets cannot be set from a Claude Code session — this is a manual step, and until it's done a source in `live` mode fails every run with `missing env vars`.
3. Edit `config/sources.json`: `"langfuse": { "mode": "live" }`.
4. Trigger the workflow manually (Actions → daily-refresh → Run workflow) or wait for the next daily run.

Recommended order: **Langfuse → Datadog → Mixpanel** (simplest auth first, most instrumentation risk last).

To test a live source locally: copy `.env.example` to `.env`, fill in the keys, then
`set -a; source .env; set +a; uv run scripts/fetch_langfuse.py --date 2026-08-13`.

Backfill: `uv run scripts/fetch_all.py --start 2026-07-01 --end 2026-08-13` (Datadog is limited by log retention, typically 15–30 days).

## One-time repo setup

1. **Settings → Pages → Source: "GitHub Actions"** (required before the deploy job can run).
2. Secrets are only needed once a source goes live — mock mode runs green with zero secrets.

## Architecture

```
scripts/fetch_<source>.py   one fetcher per source, writes data/snapshots/<date>/<source>.json
scripts/fetch_all.py        orchestrator — per-source failure isolation, retry w/ backoff
scripts/compute_metrics.py  pure function of snapshots -> site/metrics.json (no network)
scripts/audit_ids.py        checks account/user id consistency across sources (run before trusting joins)
site/                       buildless static dashboard (Chart.js vendored in site/vendor/)
config/sources.json         per-source mock/live switch
config/denominators.json    manual denominators for metrics with no instrumented source
data/status.json            per-source freshness/error state (drives the header pills)
```

Rules that keep this maintainable:

- **Fetchers never compute; compute never fetches.** The dashboard can always be rebuilt from committed history.
- **A broken live source never freezes the dashboard.** `fetch_all.py` records the error in `data/status.json` and exits 0, so compute + deploy still run off committed snapshots; the workflow's `flag-sources` job reds the run afterwards so the breakage is still visible.
- **A missing metric is a `null` + an entry in `gaps[]`, never a fabricated number.** The dashboard renders those as "instrumentation gap" cards — that list is the tracking backlog.
- The metric day is the **UTC calendar day**; the KPI window is the last 7 full UTC days.

## Known data caveats (2026-08)

- Datadog: structured `mcp.tool.call` logs only exist in **env:test** (`service:freightpop-mcp`). Prod runs an older build with unstructured logs — ask the dev team to deploy the current build, then flip `LOG_FILTER` in `scripts/fetch_datadog.py` to prod.
- Cross-source joins (fallback rate, adoption) assume the same `user_id`/`account_id` values across sources — run `scripts/audit_ids.py` before trusting them; use `config/id_map.json` if formats differ.
- Enablement roster is expected from Mixpanel profiles; `config/enablement.csv` works as a manual stopgap (columns: `account_id,seats_enabled,enabled_date,enabled_users` — users separated by `;`).
