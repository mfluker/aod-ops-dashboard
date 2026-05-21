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
    indicator_html, render_list_items, _fmt_ship_date_span,
    bonus_pace, bonus_class, bonus_pill_html,
)

# === Mock numbers (exercise each indicator color tier) ===

today = datetime.date(2026, 5, 21)

# AoD Network — System Sales (R30 headline)
rev_cur, rev_prv = 1_160_000, 1_080_000
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
claim_pct_cur, claim_pct_prv = 2.11, 2.30
claim_insufficient = False

# Shipping (R14)
ship_cur = {
    "cost_per_lb": 2.09,
    "pallet_pct": 21.0,
    "surcharge_pct_ex_fuel": 28.0,
    "earliest_ship": "2026-04-23",
    "latest_ship": "2026-05-07",
}
ship_prv = {
    "cost_per_lb": 2.34,
    "pallet_pct": 14.2,
    "surcharge_pct_ex_fuel": 26.4,
}

# === Bonus pace (mock YTD values matching the H1 2026 Bonus Update PDF) ===
ss_ytd = 5_330_000        # System Sales YTD -> pace ~$6.84M -> On Track
ss_pace = bonus_pace("system_sales", ss_ytd, today=today)
rf_ytd = 770_000          # Refacing YTD -> pace ~$988K -> On Track
rf_pace = bonus_pace("refacing_revenue", rf_ytd, today=today)
claim_pct_ytd = 2.11      # Claim % YTD -> Beating (<=3% max, lower better)
claim_bonus_pace = bonus_pace("claim_pct", claim_pct_ytd, today=today)

last_updated = datetime.datetime.now().strftime("%a %b %-d · %-I:%M %p ET")
ship_span = _fmt_ship_date_span(ship_cur["earliest_ship"], ship_cur["latest_ship"])

replacements = {
    "{{LAST_UPDATED}}": last_updated,

    # AoD Network — System Sales
    "{{REVENUE_VALUE}}":       fmt_currency(rev_cur),
    "{{REVENUE_INDICATOR}}":   indicator_html(pct_change(rev_cur, rev_prv), lower_is_better=False),
    "{{REVENUE_BONUS_CLASS}}": bonus_class("system_sales", ss_pace),
    "{{REVENUE_BONUS_PILL}}":  bonus_pill_html("system_sales", ss_pace),

    "{{APPT_COUNT}}":     str(appt_next),
    "{{APPT_INDICATOR}}": indicator_html(pct_change(appt_next, appt_prev), lower_is_better=False),

    "{{TOP_LOCATIONS}}":  render_list_items(top_locs, "appts"),
    "{{TOP_DESIGNERS}}":  render_list_items(top_dsrs, "appts", show_iata=True),

    # Refacing
    "{{REFACING_VALUE}}":       fmt_currency(rf_cur, abbreviate=True),
    "{{REFACING_INDICATOR}}":   indicator_html(pct_change(rf_cur, rf_prv), lower_is_better=False),
    "{{REFACING_BONUS_CLASS}}": bonus_class("refacing_revenue", rf_pace),
    "{{REFACING_BONUS_PILL}}":  bonus_pill_html("refacing_revenue", rf_pace),

    "{{REFACING_JOBS_VALUE}}":     str(rfj_cur),
    "{{REFACING_JOBS_INDICATOR}}": indicator_html(pct_change(rfj_cur, rfj_prv), lower_is_better=False),

    # Network Lead Times
    "{{S2I_MEDIAN_VALUE}}":     fmt_weeks_days(s2i_med_cur),
    "{{S2I_MEDIAN_INDICATOR}}": indicator_html(pct_change(s2i_med_cur, s2i_med_prv), lower_is_better=True),

    "{{S2I_PCT_VALUE}}":     fmt_pct(s2i_pct_cur),
    "{{S2I_PCT_INDICATOR}}": indicator_html(pct_change(s2i_pct_cur, s2i_pct_prv), lower_is_better=False),

    # TAT placeholder
    "{{TAT_VALUE}}":       "—",
    "{{TAT_BONUS_CLASS}}": "",
    "{{TAT_BONUS_PILL}}":  '<span class="bonus-pill neutral">No data yet</span>',
    "{{TAT_SUBLABEL}}":    "H1 bonus · target 18d",

    # Manufacturing
    "{{CLAIM_PCT_VALUE}}":     fmt_pct(claim_pct_cur, decimals=2),
    "{{CLAIM_PCT_INDICATOR}}": indicator_html(
        pct_change(claim_pct_cur, claim_pct_prv),
        lower_is_better=True,
        insufficient_data=claim_insufficient,
    ),
    "{{CLAIM_BONUS_CLASS}}": bonus_class("claim_pct", claim_bonus_pace),
    "{{CLAIM_BONUS_PILL}}":  bonus_pill_html("claim_pct", claim_bonus_pace),

    # Shipping (R14)
    "{{COST_PER_LB_VALUE}}":     fmt_currency(ship_cur["cost_per_lb"], decimals=2),
    "{{COST_PER_LB_INDICATOR}}": indicator_html(
        pct_change(ship_cur["cost_per_lb"], ship_prv["cost_per_lb"]),
        lower_is_better=True,
    ),

    "{{PALLET_PCT_VALUE}}":     fmt_pct(ship_cur["pallet_pct"]),
    "{{PALLET_PCT_INDICATOR}}": indicator_html(
        pct_change(ship_cur["pallet_pct"], ship_prv["pallet_pct"]),
        lower_is_better=False,
    ),

    "{{SURCHARGE_PCT_VALUE}}":     fmt_pct(ship_cur["surcharge_pct_ex_fuel"]),
    "{{SURCHARGE_PCT_INDICATOR}}": indicator_html(
        pct_change(ship_cur["surcharge_pct_ex_fuel"], ship_prv["surcharge_pct_ex_fuel"]),
        lower_is_better=True,
    ),

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

# Sanity: report computed bonus tiers + flag any unfilled tokens.
import re
print(f"System Sales pace = {ss_pace:,.0f}  -> {bonus_class('system_sales', ss_pace)}")
print(f"Refacing pace     = {rf_pace:,.0f}  -> {bonus_class('refacing_revenue', rf_pace)}")
print(f"Claim pace        = {claim_bonus_pace:.2f} -> {bonus_class('claim_pct', claim_bonus_pace)}")
leftover = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", html)))
print("UNFILLED TOKENS:", leftover or "none")
