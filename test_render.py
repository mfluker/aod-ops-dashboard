#!/usr/bin/env python3
"""
Render the dashboard template with MOCK data so you can preview how it looks
without hitting Canvas or reading shipping invoices.

Usage:
    python3 test_render.py
"""
import os
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from refresh import (
    fmt_currency, fmt_pct, fmt_weeks_days, pct_change,
    indicator_html, render_list_items, sparkline_svg,
    _fmt_ship_date_span,
)

# === Mock numbers (exercise each indicator color tier) ===

# AoD Network
rev_cur, rev_prv = 2_341_500, 2_180_000
appt_next, appt_prev = 142, 128
top_locs = [
    {"name": "Art of Drawers Atlanta",     "count": 14},
    {"name": "Art of Drawers Tampa",       "count": 11},
    {"name": "Art of Drawers Cincinnati",  "count": 10},
]
top_dsrs = [
    {"name": "Sarah Mitchell", "count": 9, "iata": "NATL"},
    {"name": "James Chen",     "count": 8, "iata": "HOU"},
    {"name": "Priya Patel",    "count": 7, "iata": "PHL"},
]

# Refacing R7
rf_cur, rf_prv = 184_000, 195_000
rfj_cur, rfj_prv = 12, 14

# Network Lead Times
s2i_med_cur, s2i_med_prv = 47, 53
s2i_pct_cur, s2i_pct_prv = 68.2, 64.1

# Manufacturing
claim_pct_cur, claim_pct_prv = 4.10, 4.25
claim_insufficient = False

# Shipping (R14)
ship_cur = {
    "cost_per_lb": 0.43,
    "pallet_pct": 22.7,
    "surcharge_pct_ex_fuel": 38.6,
    "earliest_ship": "2026-04-17",
    "latest_ship": "2026-04-30",
}
ship_prv = {
    "cost_per_lb": 0.47,
    "pallet_pct": 25.4,
    "surcharge_pct_ex_fuel": 41.2,
}

# Trendlines (oldest → newest, 5 values)
rev_trend       = [1_950_000, 2_010_000, 2_120_000, 2_180_000, 2_341_500]
appt_trend      = [118, 122, 130, 128, 142]
rf_trend        = [165_000, 172_000, 188_000, 195_000, 184_000]
rfj_trend       = [10, 11, 13, 14, 12]
s2i_med_trend   = [58, 56, 54, 53, 47]
s2i_pct_trend   = [60.1, 62.5, 63.0, 64.1, 68.2]
claim_trend     = [4.60, 4.45, 4.30, 4.25, 4.10]
cost_lb_trend   = [0.52, 0.49, 0.47, 0.46, 0.43]
pallet_trend    = [27.8, 26.5, 25.4, 24.0, 22.7]
surch_trend     = [44.2, 42.8, 41.2, 40.0, 38.6]

last_updated = datetime.datetime.now().strftime("%a %b %-d · %-I:%M %p ET")
ship_span = _fmt_ship_date_span(ship_cur["earliest_ship"], ship_cur["latest_ship"])

replacements = {
    "{{LAST_UPDATED}}": last_updated,

    # AoD Network
    "{{REVENUE_VALUE}}":     fmt_currency(rev_cur),
    "{{REVENUE_INDICATOR}}": indicator_html(pct_change(rev_cur, rev_prv), lower_is_better=False),
    "{{REVENUE_SPARK}}":     sparkline_svg(rev_trend, width=1200, height=300, opacity=0.32, stroke_width=5),

    "{{APPT_COUNT}}":     str(appt_next),
    "{{APPT_INDICATOR}}": indicator_html(pct_change(appt_next, appt_prev), lower_is_better=False),
    "{{APPT_SPARK}}":     sparkline_svg(appt_trend, width=600, height=400, opacity=0.42, stroke_width=5),

    "{{TOP_LOCATIONS}}":  render_list_items(top_locs, "appts"),
    "{{TOP_DESIGNERS}}":  render_list_items(top_dsrs, "appts", show_iata=True),

    # Refacing
    "{{REFACING_VALUE}}":     fmt_currency(rf_cur, abbreviate=True),
    "{{REFACING_INDICATOR}}": indicator_html(pct_change(rf_cur, rf_prv), lower_is_better=False),
    "{{REFACING_SPARK}}":     sparkline_svg(rf_trend, width=300, height=200, opacity=0.36),

    "{{REFACING_JOBS_VALUE}}":     str(rfj_cur),
    "{{REFACING_JOBS_INDICATOR}}": indicator_html(pct_change(rfj_cur, rfj_prv), lower_is_better=False),
    "{{REFACING_JOBS_SPARK}}":     sparkline_svg(rfj_trend, width=300, height=200, opacity=0.36),

    # Network Lead Times
    "{{S2I_MEDIAN_VALUE}}":     fmt_weeks_days(s2i_med_cur),
    "{{S2I_MEDIAN_INDICATOR}}": indicator_html(pct_change(s2i_med_cur, s2i_med_prv), lower_is_better=True),
    "{{S2I_MEDIAN_SPARK}}":     sparkline_svg(s2i_med_trend, width=300, height=200, opacity=0.36),

    "{{S2I_PCT_VALUE}}":     fmt_pct(s2i_pct_cur),
    "{{S2I_PCT_INDICATOR}}": indicator_html(pct_change(s2i_pct_cur, s2i_pct_prv), lower_is_better=False),
    "{{S2I_PCT_SPARK}}":     sparkline_svg(s2i_pct_trend, width=300, height=200, opacity=0.36),

    # Manufacturing
    "{{CLAIM_PCT_VALUE}}":     fmt_pct(claim_pct_cur, decimals=2),
    "{{CLAIM_PCT_INDICATOR}}": indicator_html(
        pct_change(claim_pct_cur, claim_pct_prv),
        lower_is_better=True,
        insufficient_data=claim_insufficient,
    ),
    "{{CLAIM_PCT_SPARK}}": sparkline_svg(claim_trend, width=300, height=200, opacity=0.36),

    # Shipping (R14)
    "{{COST_PER_LB_VALUE}}":     fmt_currency(ship_cur["cost_per_lb"], decimals=2),
    "{{COST_PER_LB_INDICATOR}}": indicator_html(
        pct_change(ship_cur["cost_per_lb"], ship_prv["cost_per_lb"]),
        lower_is_better=True,
    ),
    "{{COST_PER_LB_SPARK}}": sparkline_svg(cost_lb_trend, width=300, height=200, opacity=0.36),

    "{{PALLET_PCT_VALUE}}":     fmt_pct(ship_cur["pallet_pct"]),
    "{{PALLET_PCT_INDICATOR}}": indicator_html(
        pct_change(ship_cur["pallet_pct"], ship_prv["pallet_pct"]),
        lower_is_better=True,
    ),
    "{{PALLET_PCT_SPARK}}": sparkline_svg(pallet_trend, width=300, height=200, opacity=0.36),

    "{{SURCHARGE_PCT_VALUE}}":     fmt_pct(ship_cur["surcharge_pct_ex_fuel"]),
    "{{SURCHARGE_PCT_INDICATOR}}": indicator_html(
        pct_change(ship_cur["surcharge_pct_ex_fuel"], ship_prv["surcharge_pct_ex_fuel"]),
        lower_is_better=True,
    ),
    "{{SURCHARGE_PCT_SPARK}}": sparkline_svg(surch_trend, width=300, height=200, opacity=0.36),

    "{{SHIP_SPAN}}": ship_span,
}

with open(os.path.join(HERE, "template.html")) as fh:
    html = fh.read()
for k, v in replacements.items():
    html = html.replace(k, v)

out_path = os.path.join(HERE, "preview.html")
with open(out_path, "w") as fh:
    fh.write(html)

print(f"Wrote preview: {out_path}")
