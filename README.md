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

## Bonus-pace indicators (added 2026-05-21)
Cards tied to an H1 bonus metric get a subtle colored outline + a small status
pill (Off Track / Behind / On Track / Beating). The card headline stays the
rolling window; the pill reflects **pace-to-6/30** vs the H1 thresholds. Config
and scoring live in `refresh.py` under "BONUS PACE SCORING" (`BONUS_METRICS`,
`bonus_pace`, `bonus_tier`, `bonus_class`, `bonus_pill_html`). Thresholds come
from the H1 2026 Bonus Update PDF (2026-05-21).

Tiers map: <85% pace = Off Track (red) · 85–99% = Behind (yellow) · 100–114% =
On Track (green) · 115%+ = Beating (dark green). Lower-is-better metrics (Claim %,
TAT) invert the comparison.

Mapping of bonus metric → dashboard card:
- **System Sales** → the hero card (relabeled from "Total Network Revenue").
  Computed by `system_sales_rows()` as a SINGLE row-level pull over the widest
  window (min(Jan 1, R30-prior start) → today); `main()` then buckets the rows
  in Python to derive the R30 headline, the R30-prior comparison, AND the H1-YTD
  pace — one Canvas fetch instead of three SUM queries. IMPORTANT ASSUMPTION:
  new-job `order_total`, ILM-excluded, anchored on first-deposit date. If the
  official System Sales definition uses a different date anchor (e.g. sold date),
  adjust the SQL in `system_sales_rows`.
- **Refacing Revenue** → Refacing card. Pace uses `run_refacing_revenue` over
  Jan 1 → today (skipped during the offline/emit pass to save time).
- **Claim %** → Manufacturing card. Pace = H1-YTD claim % (a ratio, not projected).
- **TAT (Order → Ship)** → NEW placeholder card in the Network Lead Times column.
  Renders "—" with a "No data yet" pill until Mat wires a query/skill. Target is
  18 days; `tat_days` is already in `BONUS_METRICS` so scoring works the moment a
  value is supplied.
- **EBITDA Margin** → NOT on the dashboard and currently unscoreable (Jan-only,
  negative per the PDF). Future work: build an EBITDA skill, then a card. It is a
  25%-weight bonus metric, so it belongs on the board once P&Ls are closed.

Added cost: System Sales now costs ONE row-level pull (down from 3 SUM queries).
The bonus pace also adds one refacing-skill run (Refacing YTD) and a cheap CSV
re-read for claim per refresh. All paths go through the Canvas MCP cache — no
`canvas_query_runner.py` / credentials are used by the dashboard anymore.

## Token-budget notes (May 2026)
- All sparkline trendlines were **removed** from `refresh.py` on 2026-05-21
  (the trend functions, the `sparkline_svg` renderer, and the template
  backgrounds). Card numbers are now centered. Recover the trend functions from
  git history if the weekly deep-report needs them.
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
