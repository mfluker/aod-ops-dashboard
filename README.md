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

## TV build — fonts, layout, anti-sleep (2026-08-14)
The board's only job is to be read from across the room on the ops-room TV
(a 48" x 28" panel, ~55" 16:9, driven at 1080p from the TV's own browser).
Design rules, in priority order:

- **Everything scales off one root.** `:root { font-size: clamp(12px, 2.4vh, 36px) }`
  and every size in `template.html` is expressed in `rem`. At 1080p 1rem = 26px.
  The board therefore fills a 1080p, 1440p or 4K panel identically — do NOT
  reintroduce fixed `px` font sizes.
- **~19px is the legibility floor.** At 8-10 ft on a 48"-wide 1080p screen, one
  CSS px is ~0.025", so the smallest label must stay at/above ~0.72rem. If a new
  element needs to be smaller than that to fit, the layout is wrong, not the type.
- **No scrolling, ever.** The layout is a fixed `100vh` grid. `test_render.py
  --shot` renders the mock board at 1080p and 4K and prints any element whose
  `scrollHeight`/`scrollWidth` exceeds its box. Run it after ANY template change.
- **Order → Ship (TAT) lives in Network Lead Times**, not Manufacturing. It is a
  lead time, and four cards in one column cannot hold TV-legible type at 1080p.
  Manufacturing and Network Lead Times are 3 cards each; Refacing and Shipping 2.

### Anti-sleep
The TV blanks itself when nothing on screen changes, so the header carries three
independent sources of motion, cheapest first:
1. A **live clock** (`#clock-hm` / `#clock-sec` / `#clock-mer`) repainted every
   second by `setInterval`. The seconds are teal so the movement is visible from
   across the room.
2. Two **CSS keyframe animations** — the `livepulse` dot and the `sweep` gradient
   across the header — which keep the compositor busy between clock ticks.
3. A **Screen Wake Lock** request (`navigator.wakeLock`), re-acquired on
   `visibilitychange`. Silently no-ops where the TV browser doesn't support it.

The 5-minute `<meta http-equiv="refresh">` reloads the page, which re-arms all
three. Do not remove the clock — it is load-bearing, not decoration.

## Bonus-pace tracking — REMOVED (2026-08-14)
Per Mat: **no bonus indicators on any card, now or in future work.** That means
no status chips ("Behind" / "On Track" / "Ahead" / "Off Track"), no Projection or
Target figures, and no color-based card outlines. If you are adding a card, it
gets a label, a number, and (optionally) a period-over-period arrow. Nothing else.

What was deleted from `refresh.py`: the whole "BONUS PACE SCORING" section —
`BONUS_PERIOD_START` / `BONUS_PERIOD_END`, `BONUS_METRICS`, `bonus_pace`,
`bonus_tier`, `bonus_class`, `bonus_pill_html`, `fmt_bonus_value`,
`bonus_figures_html` — plus every `{{*_BONUS_CLASS}}` / `{{*_BONUS_PILL}}` /
`{{*_BONUS_FIGURES}}` token and its CSS. Recover from git history if a separate
bonus report is ever wanted; it does not belong on this board.

**Period-over-period arrows STAY, and stay colored.** They are trend context
(this window vs the prior window), not bonus tracking. `indicator_html()` is
untouched; its palette was nudged up in saturation for the TV.

Query cost went DOWN with the removal:
- `system_sales_windows()` dropped its third (H1-YTD) conditional SUM, so its
  scan shrank from "Jan 1 → today" to just the two R30 windows. Its SQL changed,
  so the first refresh after this fetches a new query hash.
- The two YTD pulls that existed only to feed pace — Refacing YTD
  (`run_refacing_revenue` over Jan 1 → today) and the H1 claim-% CSV pass — are
  gone. That is **two fewer Canvas/sheet round-trips per refresh**.
- `run_refacing_revenue()` is now unused by the dashboard but kept, since other
  tooling imports it.

## System Sales definition
The hero card. `system_sales_windows()` computes the R30 headline and the
R30-prior comparison in ONE query via conditional SUM: `SUM(order_total)` for NEW
jobs (`job_type_id = 1`), ILM-excluded, anchored on `j.date_added` (when the job
was sold/created).

We deliberately do NOT anchor on first-deposit date — that older approach
aggregated the giant `customer_payment` table and timed out on the Canvas
replica, which is why the revenue card used to render $0.

## Claims cards rework (2026-08-06)
Three changes to the Manufacturing column, per Mat:
- **Claims +** (was "Claims on Claims") is now a TABLE, not counts: every
  in-production claim whose parent order is itself a Claim **or a Reorder**,
  with Job #, Mfg, Location, and **Claim #** — the claim's position in the
  remake chain (claim-on-claim = 2, claim-on-reorder = 2, claim-on-claim-on-
  claim = 3; 3+ gets a red badge). The "all-time on tracker" footer was
  removed. Data: `fetch_nested_claim_rows()` from the Mfg Partner sheet, chain
  walked through both claims and reorders by numeric id. Table caps at 4 rows
  (3 + "+N more") to protect the fixed-height layout.
- **Installs w/ Claim or Reorder** now splits the headline: total % on top,
  "Claim X% · Reorder Y%" beneath. Claim wins on overlap (a job with both
  counts under Claim), so the two always sum to the headline.
  `claim_reorder_rate()` returns the split — note its SQL changed, so the
  first refresh after this change fetches a new query hash.
- **Top Claim Reasons · Last 30 Days** — top 3 Claim Type reasons weighted by
  line items (`top_claim_reasons()`). Moved 2026-08-10 to the right-hand slot of
  the Claim Line Items % card. The TV build (2026-08-14) renders the share % only
  — "41 items · 29%" clipped in that narrow slot at TV type sizes, and the share
  is what actually reads from across the room.
The sheet CSV is now fetched ONCE per refresh and cached in-process
(`_fetch_mfg_csv()`); it used to be downloaded ~6× per run.

## Column rework (2026-09-04)

Per Mat, three changes to the bottom half of the board.

**Network Lead Times** is now Measure→Install (one card, two halves) / Order→Ship
(TAT) / **Overdue Orders**. The median and the "% under 10 weeks" share a single
card via `.dual-main` — two equal halves, each with its own sub-label, number and
arrow, split by a hairline. The stack uses `.lead-stack` (1fr 1fr 1.32fr), the
same ratio as `.mfg-stack`, so the two table cards line up across the board.

**Overdue Orders** reads the *Overdue Orders* tab of the Mfg Partner Analysis
sheet — `fetch_overdue_rows()`. Notes on that data source:
- That tab is NOT part of the published-CSV export (only Orders Tracker, gid=0,
  is published), so it is read through the **gviz** endpoint
  (`/gviz/tq?tqx=out:csv&sheet=Overdue%20Orders`), which serves any tab of a
  link-shared sheet by name. Overridable via `AOD_MFG_SHEET_OVERDUE_CSV_URL`.
- The tab is a pivot: one six-column block per manufacturer (CCF, EAG, JB, GHWD,
  NASL, Dackor, RM). **The manufacturer comes from the block, not a column** —
  gviz folds the merged banner into the first header cell of each block
  ("Overdue Orders: CCF Job #"), which is what `_OVERDUE_BLOCK_RE` parses.
- The tab carries no location code, so rows are joined to the Orders Tracker tab
  on the numeric job number (`_tracker_locations()`). The tracker CSV is already
  fetched once per refresh, so this costs nothing extra.
- Do NOT "simplify" this to a `Days over Lead Time > 0` filter on the Orders
  Tracker tab. That sweep also returns Richelieu and VS, which the Overdue
  Orders tab deliberately excludes (60 rows vs 6 as of 2026-09-04).
- Card shows the 3 most overdue + "+N more · click for all"; the overlay
  (`#overdue-overlay`) lists all of them with Type and Est Ship. Days >= 7 gets
  the red badge. C/R prefixes are re-added from the Type column.

**Shipping** moved from R14 to **R30** so both its numbers tie back to the
Monday/Wednesday ops report, and is now a two-tile stat row (`.ship-row`:
Pallet % | Paid by Home Office) over a **Surcharges to Chase** table. The old
"Surcharge % (ex-fuel)" card is gone. `.ship-stack` is `2fr 1.32fr` — the stat
row is two cards tall and the chase card gets the same 1.32fr as Claims + and
Overdue Orders, so all three table cards line up across the board. Those stat
tiles override the card's default `auto 1fr auto` rows with
`auto auto auto` + `align-content: center`, or the double height flings the
label to the top and the arrow to the bottom.

**Percent-change pills** come in two forms. `indicator_html(..., compact=True)`
drops the "vs prior" tail and rounds to a whole number, for anywhere the pill
shares a card with another number — the dual Measure -> Install halves and the
two half-width Shipping tiles. The full pill is ~200px wide, which was the
entire width of those columns; the compact one is ~70px. Do not put a full pill
back in a half-width card.

**Surcharges Paid by Home Office** is everything that is neither fuel nor memo
pass-through to the Franchise Partner. `PASS_THROUGH_PATTERNS`, `FLAG_GROUPS`,
`INSURANCE_*` and `classify_surcharge()` are **copied verbatim** from
`daily-ops-report/generate_report.py`. If Mat reclassifies a surcharge, change it
in BOTH files or the card and the report will disagree.

**Surcharges to Chase** = packaging (Additional Handling / Large Package / Over
Dimension) totalling >= $75 on a single shipment, PLUS insurance/declared-value
lines whose estimated coverage clears $5,000 — combined and ranked by amount.
Card shows 4; the overlay (`#chase-overlay`) shows all, with the full tracking
number, the charge detail and the ship date. The card truncates tracking to the
last 9 characters (`_short_track`) — an 18-char UPS airbill will not fit a
quarter-column at TV type sizes, and the tail is what identifies the shipment.

`refresh.py` now imports the parser from **shipping-surcharge-analysis**, not
shipping-cost-analysis. Same `load_shipments()` signature, but each record also
carries airbill/pro/bol, invoice_no, job_ref and receiver city+state, which the
chase table needs. That parser's own folder search does not know this checkout's
mount point, so `_invoice_base_paths()` passes the roots explicitly.

### Surcharges to Chase — falloff (2026-09-04)

A chased surcharge has to stop appearing, or the card just accretes. The board
is a static page on GitHub Pages and cannot record anything itself, so "chased"
is written to a Google Form whose responses land in a tab of the Mfg Partner
sheet, and the next refresh drops those tracking numbers.

Two env vars, both optional — with neither set the board renders exactly as
before (no filtering, no links), so an unconfigured checkout still works:
- `AOD_SURCHARGE_CHASED_CSV_URL` — gviz CSV of the Form-response tab.
  `fetch_chased_tracking()` takes the column whose header mentions "tracking",
  falling back to column B (the Form's first question). Tracking numbers are
  compared stripped of punctuation (`_norm_track`), since they get pasted with
  spaces and dashes.
- `AOD_SURCHARGE_CHASE_FORM_URL` — the pre-filled Form URL with `{TRACK}`,
  `{MFG}` and `{AMT}` left as literal tokens where the pre-fill placeholders
  are. `chase_form_link()` substitutes and URL-encodes per row, and the overlay
  renders a "Mark chased" pill in the last column. The link carries
  `event.stopPropagation()` so clicking it does not also toggle the overlay.

Once chased, a line never comes back — Mat's call. Chasing a non-response is an
email problem, not a dashboard problem. There is no re-open window and no
status column; if that changes, the filter is the one place to change it.

TV check after this change: `python3 test_render.py --shot` reports no overflow
at 1080p or 4K. Three things were tuned to get there and should not be undone
casually: the chase card's label is `0.64rem` (the claims-card label is nowrap
and this is the widest line in the narrowest column), the chase table is
`table-layout: fixed` with per-column widths, and the ship-date span moved from
both stat tiles to the Pallet tile only.

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
