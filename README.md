# aod-ops-dashboard
Dashboard for the Ops Team.

## Before editing any Canvas SQL in here
Read `../CANVAS-MCP-RULES.md`. It documents the mandatory filters
(`franchisee_id != 1` for ILM, `current_status_id != 19` for deleted jobs,
`include='y'` for payments), the `list_things` / `get_thing_config` discovery
flow, and the list of pre-built KPI tools available on the Canvas MCP.

The shared filter helpers live in `../canvas_data.py` —
`standard_filters()` returns the canonical snippets so they can't be
forgotten.

## How refreshes run
The dashboard refreshes via a single **Cowork scheduled task** named
`ops-dashboard-updater`, configured to run **once daily at 9:30am local time**
(Monday–Friday). It is the only active scheduler — there is no 3×/day task and
no launchd job. The task orchestrates the emit → fetch via Canvas MCP → compute
flow:

1. Run `refresh.py` once in offline/emit mode (writes a manifest of every SQL
   query the script wants to run).
2. For each query in the manifest, call the Canvas MCP `run_select_query`
   tool and persist the result into the cache directory as `<sha1>.json`.
3. Re-run `refresh.py` (now in compute mode) — it reads from the cache and
   writes `index.html`.
4. Git-push the new `index.html` so the live dashboard page updates.

The previous `launchd` + `run.sh` setup has been removed. The Cowork
scheduled task is the single source of truth for the schedule.

## Token-budget notes (May 2026)
- All sparkline trendlines are disabled in `refresh.py`. As of 2026-05-21 the
  trend-computing functions were **removed** from the file (they were never
  called from `main()`); each sparkline now renders `sparkline_svg([])`, a flat
  line. Recover the functions from git history if they're needed again.
- `install_trend_5x30` (now deleted) was the single most expensive call — a
  150-day install-vs-deposit re-run every refresh. Don't revive it lightly.
- A separate "Wednesday 9:30am deep-report" skill is planned to surface
  trend-style context once a week. That's the right place to revive the
  expensive multi-period queries.

## KPI-tool swaps (pending admin approval)
Look for `KPI-TOOL TODO` comments in `refresh.py`. Once the Canvas MCP is
fully approved for this account, the major custom-SQL queries
(`revenue_in_window`, `appointment_count`) can be replaced with single calls
to pre-built KPI tools (`get_revenue`, `get_appointment_count`). The KPI
tools have AoD's fine-tuning baked in, so swapping is both a correctness and
a token win.
