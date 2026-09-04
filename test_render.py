#!/usr/bin/env python3
"""
Render the dashboard template with MOCK data so you can preview how it looks
without hitting Canvas, the Mfg Partner sheet, or the WWEX invoices.

    python3 test_render.py                 # writes preview.html
    python3 test_render.py --shot          # also screenshots it at 1920x1080

The screenshot path needs Playwright (`pip install playwright`); it is skipped
with a note if Playwright isn't installed.

This harness is the fastest way to check the TV layout after a template change.
It asserts that every token in template.html is filled, so a token added to the
template but not to refresh.py's replacements dict fails here first.
"""
import os
import re
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from refresh import (
    fmt_currency, fmt_pct, fmt_weeks_days, pct_change,
    indicator_html, render_list_items, _fmt_ship_date_span, now_eastern_stamp,
    claim_reasons_html, coc_table_rows_html, coc_more_note, coc_click_attrs,
    coc_is_clickable, coc_modal_rows_html,
    overdue_table_rows_html, overdue_more_note, overdue_click_attrs,
    overdue_is_clickable, overdue_modal_rows_html,
    chase_table_rows_html, chase_more_note, chase_click_attrs,
    chase_is_clickable, chase_modal_rows_html, INSURANCE_GROUP,
)

# === Mock numbers (chosen to exercise each indicator color tier) ===

# AoD Network — System Sales (R30 headline)
rev_cur, rev_prv = 1_284_970, 1_185_400
appt_next, appt_prev = 212, 221
top_locs = [
    {"name": "Charlotte", "count": 34},
    {"name": "Raleigh",   "count": 29},
    {"name": "Nashville", "count": 26},
]
top_dsrs = [
    {"name": "Jessica Barnhart",  "count": 14, "iata": "CLT"},
    {"name": "Michael Rodriguez", "count": 12, "iata": "RDU"},
    {"name": "Ashley Kim",        "count": 11, "iata": "BNA"},
]

# Refacing R7
rf_cur, rf_prv = 184_000, 150_100
rfj_cur, rfj_prv = 9, 8

# Network Lead Times (R30) — Measure -> Install, plus Order -> Ship TAT
mti_med_cur, mti_med_prv = 59, 63
mti_pct_cur, mti_pct_prv = 61.4, 59.1
tat_med_cur, tat_med_prv = 19, 21

# Manufacturing (R30)
claim_pct_cur, claim_pct_prv = 3.86, 4.35
claim_insufficient = False
cr_pct_cur, cr_pct_prv = 14.2, 14.5
cr_claim_cur, cr_reorder_cur = 9.8, 4.4

nested_rows = [
    {"job": "29714", "mfg": "EAG",  "loc": "CLT", "claim_no": 2, "prod": True,
     "parent": "28901", "date": datetime.date(2026, 7, 29)},
    {"job": "29688", "mfg": "CCF",  "loc": "RDU", "claim_no": 3, "prod": True,
     "parent": "28455", "date": datetime.date(2026, 7, 24)},
    {"job": "29610", "mfg": "J&B",  "loc": "ATL", "claim_no": 2, "prod": True,
     "parent": "29102", "date": datetime.date(2026, 7, 21)},
    {"job": "29544", "mfg": "EAG",  "loc": "DFW", "claim_no": 2, "prod": True,
     "parent": "28877", "date": datetime.date(2026, 7, 18)},
    {"job": "29501", "mfg": "NASL", "loc": "PHX", "claim_no": 2, "prod": True,
     "parent": "28712", "date": datetime.date(2026, 7, 15)},
]
reasons = [
    {"reason": "Wrong Size",         "items": 41, "share_pct": 29},
    {"reason": "Damaged in Transit", "items": 27, "share_pct": 19},
    {"reason": "Finish Defect",      "items": 18, "share_pct": 13},
]

# Overdue orders (Mfg Partner sheet, Overdue Orders tab)
overdue_rows = [
    {"job": "6896", "type": "Job",   "mfg": "RM",   "loc": "CLE", "est_ship": "08/18", "days": 13},
    {"job": "6880", "type": "Job",   "mfg": "RM",   "loc": "ETN", "est_ship": "08/18", "days": 13},
    {"job": "6652", "type": "Job",   "mfg": "RM",   "loc": "RAL", "est_ship": "08/25", "days": 8},
    {"job": "7080", "type": "Job",   "mfg": "CCF",  "loc": "NVL", "est_ship": "09/03", "days": 1},
    {"job": "C7344", "type": "Claim", "mfg": "CCF", "loc": "STL", "est_ship": "09/03", "days": 1},
]

# Shipping (R30)
ship_cur = {
    "pallet_pct": 38.5,
    "ho_pct": 8.4,
    "earliest_ship": "2026-07-12",
    "latest_ship": "2026-08-11",
}
ship_prv = {"pallet_pct": 36.6, "ho_pct": 9.1}


def _mock_ship(mfr, airbill, city, st, date):
    return {"manufacturer": mfr, "airbill": airbill, "pro": "", "bol": "",
            "receiver_city": city, "receiver_state": st, "ship_date": date}


chase = [
    {"group": "Packaging", "amt": 412.50, "est": None,
     "types": ["ADDITIONAL HANDLING LENGTH + GIRTH"],
     "s": _mock_ship("Eagle", "1ZR1B3690336355829", "Charlotte", "NC", "2026-08-06")},
    {"group": INSURANCE_GROUP, "amt": 94.50, "est": 9000.0, "types": ["DECLARED VALUE"],
     "s": _mock_ship("CCF", "5940948160", "Raleigh", "NC", "2026-08-04")},
    {"group": "Packaging", "amt": 188.00, "est": None, "types": ["LARGE PACKAGE SURCHARGE"],
     "s": _mock_ship("Eagle", "1ZR1B3690316418987", "Dallas", "TX", "2026-08-01")},
    {"group": "Packaging", "amt": 121.75, "est": None, "types": ["OVER DIMENSION FREIGHT"],
     "s": _mock_ship("JB", "1ZR1B3690328844120", "Phoenix", "AZ", "2026-07-30")},
    {"group": "Packaging", "amt": 96.00, "est": None, "types": ["ADDITIONAL HANDLING"],
     "s": _mock_ship("CCF", "1ZR1B3690311209887", "Tampa", "FL", "2026-07-28")},
    {"group": INSURANCE_GROUP, "amt": 78.75, "est": 7500.0, "types": ["INSURANCE"],
     "s": _mock_ship("Eagle", "1ZR1B3690399142230", "Denver", "CO", "2026-07-25")},
]
chase.sort(key=lambda x: -x["amt"])

ship_span = _fmt_ship_date_span(ship_cur["earliest_ship"], ship_cur["latest_ship"])

replacements = {
    "{{LAST_UPDATED}}": now_eastern_stamp(),

    # AoD Network — System Sales
    "{{REVENUE_VALUE}}":     fmt_currency(rev_cur),
    "{{REVENUE_INDICATOR}}": indicator_html(pct_change(rev_cur, rev_prv), lower_is_better=False),

    "{{APPT_COUNT}}":     str(appt_next),
    "{{APPT_INDICATOR}}": indicator_html(pct_change(appt_next, appt_prev), lower_is_better=False),

    "{{TOP_LOCATIONS}}": render_list_items(top_locs, "appts"),
    "{{TOP_DESIGNERS}}": render_list_items(top_dsrs, "appts", show_iata=True),

    # Refacing — headline in whole dollars (Mat 2026-05-22)
    "{{REFACING_VALUE}}":          fmt_currency(rf_cur),
    "{{REFACING_INDICATOR}}":      indicator_html(pct_change(rf_cur, rf_prv), lower_is_better=False),
    "{{REFACING_JOBS_VALUE}}":     str(rfj_cur),
    "{{REFACING_JOBS_INDICATOR}}": indicator_html(pct_change(rfj_cur, rfj_prv), lower_is_better=False),

    # Network Lead Times
    "{{MTI_MEDIAN_VALUE}}":     fmt_weeks_days(mti_med_cur),
    "{{MTI_MEDIAN_INDICATOR}}": indicator_html(pct_change(mti_med_cur, mti_med_prv), lower_is_better=True),
    "{{MTI_PCT_VALUE}}":        fmt_pct(mti_pct_cur),
    "{{MTI_PCT_INDICATOR}}":    indicator_html(pct_change(mti_pct_cur, mti_pct_prv), lower_is_better=False),
    "{{TAT_VALUE}}":            f"{tat_med_cur}d",
    "{{TAT_INDICATOR}}":        indicator_html(pct_change(tat_med_cur, tat_med_prv), lower_is_better=True),

    # Manufacturing
    "{{CLAIM_PCT_VALUE}}":     fmt_pct(claim_pct_cur, decimals=2),
    "{{CLAIM_PCT_INDICATOR}}": indicator_html(
        pct_change(claim_pct_cur, claim_pct_prv),
        lower_is_better=True,
        insufficient_data=claim_insufficient,
    ),
    "{{TOP_CLAIM_REASONS}}": claim_reasons_html(reasons),

    "{{CLAIM_REORDER_VALUE}}": fmt_pct(cr_pct_cur, decimals=1),
    "{{CLAIM_REORDER_SPLIT}}": (
        f'<span class="cr-part">Claim <b>{fmt_pct(cr_claim_cur, decimals=1)}</b></span>'
        f'<span class="cr-sep">·</span>'
        f'<span class="cr-part">Reorder <b>{fmt_pct(cr_reorder_cur, decimals=1)}</b></span>'
    ),
    "{{CLAIM_REORDER_INDICATOR}}": indicator_html(pct_change(cr_pct_cur, cr_pct_prv), lower_is_better=True),

    "{{COC_TABLE_ROWS}}":      coc_table_rows_html(nested_rows),
    "{{COC_MORE_NOTE}}":       coc_more_note(nested_rows),
    "{{COC_CLICK_ATTRS}}":     coc_click_attrs(nested_rows),
    "{{COC_CLICKABLE_CLASS}}": ("claims-clickable" if coc_is_clickable(nested_rows) else ""),
    "{{COC_MODAL_ROWS}}":      coc_modal_rows_html(nested_rows),
    "{{COC_ALL_COUNT}}":       str(sum(1 for r in nested_rows if r["prod"])),

    # Network Lead Times — overdue orders
    "{{OVERDUE_TABLE_ROWS}}":      overdue_table_rows_html(overdue_rows),
    "{{OVERDUE_MORE_NOTE}}":       overdue_more_note(overdue_rows),
    "{{OVERDUE_CLICK_ATTRS}}":     overdue_click_attrs(overdue_rows),
    "{{OVERDUE_CLICKABLE_CLASS}}": ("claims-clickable" if overdue_is_clickable(overdue_rows) else ""),
    "{{OVERDUE_MODAL_ROWS}}":      overdue_modal_rows_html(overdue_rows),
    "{{OVERDUE_ALL_COUNT}}":       str(len(overdue_rows)),

    # Shipping (R30)
    "{{PALLET_PCT_VALUE}}":     fmt_pct(ship_cur["pallet_pct"]),
    "{{PALLET_PCT_INDICATOR}}": indicator_html(
        pct_change(ship_cur["pallet_pct"], ship_prv["pallet_pct"]), lower_is_better=False),
    "{{HO_SURCHARGE_PCT_VALUE}}":     fmt_pct(ship_cur["ho_pct"]),
    "{{HO_SURCHARGE_PCT_INDICATOR}}": indicator_html(
        pct_change(ship_cur["ho_pct"], ship_prv["ho_pct"]), lower_is_better=True),

    "{{CHASE_TABLE_ROWS}}":      chase_table_rows_html(chase),
    "{{CHASE_MORE_NOTE}}":       chase_more_note(chase),
    "{{CHASE_CLICK_ATTRS}}":     chase_click_attrs(chase),
    "{{CHASE_CLICKABLE_CLASS}}": ("claims-clickable" if chase_is_clickable(chase) else ""),
    "{{CHASE_MODAL_ROWS}}":      chase_modal_rows_html(chase),
    "{{CHASE_ALL_COUNT}}":       str(len(chase)),
    "{{CHASE_TOTAL}}":           fmt_currency(sum(f["amt"] for f in chase)),

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

leftover = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", html)))
if leftover:
    print("UNFILLED TOKENS:", leftover)
    sys.exit(1)
print("UNFILLED TOKENS: none")

# --- Optional: screenshot + overflow audit at TV resolution ---------------
# The board is a fixed 100vh layout with no scrolling, so the only way a change
# "breaks" it is by overflowing a card. This catches that mechanically.
if "--shot" in sys.argv:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("(--shot needs Playwright: pip install playwright && playwright install chromium)")
        sys.exit(0)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, height, tag in [(1920, 1080, "1080p"), (3840, 2160, "4k")]:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto("file://" + out_path)
            page.wait_for_timeout(2500)          # let webfonts land
            overflow = page.evaluate("""() => {
                const bad = [];
                document.querySelectorAll('.card,.metric-card,.coc-table,.top-lists li,.header')
                    .forEach(el => {
                        if (el.scrollHeight - el.clientHeight > 3 || el.scrollWidth - el.clientWidth > 3)
                            bad.push(el.className.toString().slice(0, 44));
                    });
                return bad;
            }""")
            print(f"{tag}: overflow = {overflow or 'none'}")
            if tag == "1080p":
                shot = os.path.join(HERE, "preview.png")
                page.screenshot(path=shot)
                print(f"Wrote screenshot: {shot}")
            page.close()
        browser.close()
