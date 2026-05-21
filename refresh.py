#!/usr/bin/env python3
"""
AoD Operations Dashboard — Refresh Script
==========================================

What this script does (in order):
1. Talks to Canvas to pull live numbers (Revenue R30, Appointments next 7 days,
   top locations, top designers).
2. Calls the existing install-vs-deposit skill to get Sold-to-Install median
   and % under 10 weeks for both the current 30 days and the prior 30 days.
3. Calls the existing refacing-sales skill to get Refacing Revenue R7 and prior 7.
4. Pulls the Mfg Partner Analysis Google Sheet (published-to-web CSV) and
   computes the Claim Line Items % for current 30 days and prior 30 days.
5. Computes percent-change indicators (up/down arrow + color tier).
6. Fills in the HTML template and writes the final dashboard file.

Run it manually:
    python3 refresh.py
(but the script will fail with a clear "no cached result" error unless the
Canvas MCP cache has been populated first — that's the dashboard's design.)

Scheduled refreshes run through the Cowork scheduled task "aod-ops-dashboard-
refresh" (daily 9:30am local). The task prompt orchestrates the emit → fetch
via Canvas MCP → compute flow described in canvas_data.py. The previous
launchd/run.sh setup has been retired.

Configure once (one-time setup):
    - Set AOD_MFG_SHEET_CSV_URL to the published-CSV URL of the Mfg Partner
      Analysis Google Sheet. (In Google Sheets: File → Share → Publish to web → CSV)
    - Optionally set AOD_DASHBOARD_OUT to override where index.html is written.
"""

import os
import sys
import re
import csv
import io
import math
import json
import datetime
import subprocess
import base64
import hashlib
from urllib.request import urlopen, Request
from urllib.error import HTTPError

# -----------------------------------------------------------------------------
# 1. PATHS & CONFIG
# -----------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(HERE, "template.html")
DEFAULT_OUTPUT_PATH = os.path.join(HERE, "index.html")
OUTPUT_PATH = os.environ.get("AOD_DASHBOARD_OUT", DEFAULT_OUTPUT_PATH)

# Several candidate paths — we try the local-machine path first, then sandbox paths.
# This lets the script run from Mat's Mac AND from the Cowork bash sandbox.
#
# The Cowork sandbox session ID changes every run (e.g. /sessions/<random-name>/...),
# so we resolve sandbox paths relative to THIS file's location whenever possible,
# and also glob /sessions/*/mnt/AoD_Cowork/... as a fallback.
import glob as _glob
_AOD_COWORK_ROOT = os.path.abspath(os.path.join(HERE, ".."))  # parent of ops-dashboard/

def _sandbox_glob(rel):
    """Find a file/dir under any /sessions/*/mnt/AoD_Cowork/<rel> mount."""
    matches = _glob.glob(f"/sessions/*/mnt/AoD_Cowork/{rel}")
    return matches[0] if matches else None

_install_rel = "skills/install-vs-deposit/install_vs_deposit.py"
_refacing_rel = "skills/refacing-sales/refacing_sales.py"

# The shared Canvas MCP bridge (canvas_data.py) lives at the AoD_Cowork root
# (parent of ops-dashboard/). Canvas data is pre-fetched into the MCP cache by
# Claude before refresh.py runs — see the aod-ops-dashboard-refresh task.
CANVAS_DATA_DIR_CANDIDATES = [
    "/Users/artofdrawersllc/Documents/Claude/Projects/AoD_Cowork",
    _AOD_COWORK_ROOT,
    (_sandbox_glob("canvas_data.py") or "").rsplit("/", 1)[0],
]
INSTALL_VS_DEPOSIT_CANDIDATES = [
    "/Users/artofdrawersllc/Documents/Claude/Projects/AoD_Cowork/" + _install_rel,
    os.path.join(_AOD_COWORK_ROOT, _install_rel),
    _sandbox_glob(_install_rel) or "",
]
REFACING_SALES_CANDIDATES = [
    "/Users/artofdrawersllc/Documents/Claude/Projects/AoD_Cowork/" + _refacing_rel,
    os.path.join(_AOD_COWORK_ROOT, _refacing_rel),
    _sandbox_glob(_refacing_rel) or "",
]

def _first_existing(candidates):
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]  # fall back to the first one so error messages point somewhere sensible

INSTALL_VS_DEPOSIT_SCRIPT = _first_existing(INSTALL_VS_DEPOSIT_CANDIDATES)
REFACING_SALES_SCRIPT = _first_existing(REFACING_SALES_CANDIDATES)

MFG_SHEET_CSV_URL = os.environ.get("AOD_MFG_SHEET_CSV_URL", "").strip()


_run_query_cached = None

def run_query(*args, **kwargs):
    """
    Lazy-loading wrapper for the shared Canvas MCP bridge (canvas_data.run_query).
    Canvas data is pre-fetched into the MCP cache by Claude before refresh.py runs
    (see the aod-ops-dashboard-refresh scheduled task). Imported lazily so
    test_render.py (which only uses formatting helpers) doesn't need the bridge.
    """
    global _run_query_cached
    if _run_query_cached is None:
        for d in CANVAS_DATA_DIR_CANDIDATES:
            if d and os.path.exists(d) and d not in sys.path:
                sys.path.insert(0, d)
        from canvas_data import run_query as _rq
        _run_query_cached = _rq
    return _run_query_cached(*args, **kwargs)


# -----------------------------------------------------------------------------
# 2. SMALL HELPERS — formatting + math
# -----------------------------------------------------------------------------

def fmt_currency(n, abbreviate=False, decimals=0):
    """Render a number like 1234567 as '$1,234,567', '$1.23M', or '$0.43' (cost per lb)."""
    if n is None:
        return "—"
    n = float(n)
    if abbreviate:
        if n >= 1_000_000:
            return f"${n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"${n/1_000:.0f}K"
    return f"${n:,.{decimals}f}"


def fmt_pct(n, decimals=1):
    """Render a percentage like 12.34 as '12.3%'."""
    if n is None:
        return "—"
    return f"{n:.{decimals}f}%"


def fmt_weeks_days(days):
    """
    Render a number of days like 47.5 as '6w 6d' — rounded UP to the nearest full day,
    then split into weeks + days. Used for Sold-to-Install median.
    """
    if days is None:
        return "—"
    d = math.ceil(float(days))
    weeks = d // 7
    rem = d % 7
    if weeks == 0:
        return f"{rem}d"
    if rem == 0:
        return f"{weeks}w"
    return f"{weeks}w {rem}d"


def _to_float(v, default=0.0):
    """
    Parse a number that may arrive as a comma-formatted string from Canvas
    (e.g. '1,110,809.90' or '$2,341,500'). Returns `default` on failure.
    """
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return default


def _to_int(v, default=0):
    """Parse an integer that may come back as a comma-formatted string."""
    return int(_to_float(v, default))


def pct_change(current, prior):
    """Returns (current - prior) / prior * 100. None when we can't compute it."""
    if current is None or prior is None:
        return None
    try:
        prior = float(prior)
        current = float(current)
    except (TypeError, ValueError):
        return None
    if prior == 0:
        return None
    return (current - prior) / prior * 100.0


def indicator_html(pct, lower_is_better=False, insufficient_data=False):
    """
    Build the HTML for the colored arrow indicator next to a stat.

    pct: percent change vs. prior period (can be negative). None = no comparison.
    lower_is_better: True for stats where down = good (e.g., Sold-to-Install median).
    insufficient_data: True to show the 'not enough data' state instead of an arrow.

    Color tiers (by |pct|):  <3% = light, 3-10% = medium, >=10% = deep.
    Green when improving, red when worsening.
    """
    if insufficient_data:
        return '<span class="indicator no-data">not enough data yet</span>'
    if pct is None:
        return '<span class="indicator no-data">no prior data</span>'

    abs_pct = abs(pct)
    if abs_pct < 3:
        tier = "light"
    elif abs_pct < 10:
        tier = "med"
    else:
        tier = "deep"

    going_up = pct >= 0
    improving = (going_up and not lower_is_better) or ((not going_up) and lower_is_better)
    color_class = ("up-" if improving else "down-") + tier

    arrow = "▲" if going_up else "▼"
    sign = "+" if pct >= 0 else ""
    return (
        f'<span class="indicator {color_class}">'
        f'<span class="arrow">{arrow}</span> {sign}{pct:.1f}%'
        f'<span class="vs-label">vs prior</span>'
        f'</span>'
    )


# -----------------------------------------------------------------------------
# 2a. BONUS PACE SCORING  (H1 2026 — source: H1_2026_Bonus_Update PDF, 2026-05-21)
# -----------------------------------------------------------------------------
#
# The total bonus splits 50/50 across an Overall and an Operations bucket.
# Payout per metric is stepwise on PACE-TO-PERIOD-END vs the H1 thresholds:
#   < 85%  = 0% payout   (Off Track)
#   85-99% = 85% payout   (Behind)
#   100-114% = 100% payout (On Track)
#   115%+  = 115% payout   (Beating / Max)
#
# Threshold columns from the PDF map to: Target = 100% line, MIN(85%) = 85% line,
# MAX(115%) = 115% line. For "lower is better" metrics (TAT days, Claim %) the
# MIN value is the HIGHEST number and MAX is the LOWEST — handled below.
#
# `cumulative=True` metrics are flows that accumulate over the period (System
# Sales, Refacing Revenue) — we linearly project YTD to period end. `cumulative=
# False` metrics are ratios/averages (Claim %, TAT) where the YTD value already
# IS the pace, so we use it as-is.
BONUS_PERIOD_START = datetime.date(2026, 1, 1)
BONUS_PERIOD_END   = datetime.date(2026, 6, 30)   # inclusive

BONUS_METRICS = {
    "system_sales":     {"target": 6_500_000, "min85": 5_500_000, "max115": 8_000_000, "lower_is_better": False, "cumulative": True},
    "refacing_revenue": {"target": 900_000,   "min85": 700_000,   "max115": 1_100_000, "lower_is_better": False, "cumulative": True},
    "claim_pct":        {"target": 4.0,       "min85": 5.0,       "max115": 3.0,       "lower_is_better": True,  "cumulative": False},
    "tat_days":         {"target": 18.0,      "min85": 25.0,      "max115": 12.0,      "lower_is_better": True,  "cumulative": False},
    # EBITDA Margin is a 5th bonus metric (25% weight) but is NOT on the dashboard
    # and is currently unscoreable (Jan-only, negative). Tracked as a future card.
}

_BONUS_TIER_LABELS = {
    "beating": "Beating", "ontrack": "On Track", "behind": "Behind", "offtrack": "Off Track",
}


def bonus_pace(metric_key, ytd_value, today=None):
    """Project a metric's pace to the end of the bonus period.

    Cumulative flows are linearly extrapolated from YTD; ratios/averages are
    returned as-is. Returns None when there's no value to score.
    """
    if ytd_value is None:
        return None
    m = BONUS_METRICS[metric_key]
    if not m.get("cumulative"):
        return float(ytd_value)
    today = today or datetime.date.today()
    end = min(today, BONUS_PERIOD_END)
    elapsed = (end - BONUS_PERIOD_START).days + 1
    total = (BONUS_PERIOD_END - BONUS_PERIOD_START).days + 1
    if elapsed <= 0:
        return None
    return float(ytd_value) / elapsed * total


def bonus_tier(metric_key, pace):
    """Return ('beating'|'ontrack'|'behind'|'offtrack', label) or (None, None)."""
    if pace is None:
        return None, None
    m = BONUS_METRICS[metric_key]
    if m["lower_is_better"]:
        if   pace <= m["max115"]: tier = "beating"
        elif pace <= m["target"]: tier = "ontrack"
        elif pace <= m["min85"]:  tier = "behind"
        else:                     tier = "offtrack"
    else:
        if   pace >= m["max115"]: tier = "beating"
        elif pace >= m["target"]: tier = "ontrack"
        elif pace >= m["min85"]:  tier = "behind"
        else:                     tier = "offtrack"
    return tier, _BONUS_TIER_LABELS[tier]


def bonus_class(metric_key, pace):
    """CSS class for the subtle card outline ('' when unscoreable)."""
    tier, _ = bonus_tier(metric_key, pace)
    return f"bonus-{tier}" if tier else ""


def bonus_pill_html(metric_key, pace):
    """Small status pill ('' when unscoreable)."""
    tier, label = bonus_tier(metric_key, pace)
    if not tier:
        return ""
    return f'<span class="bonus-pill {tier}">{label}</span>'


# -----------------------------------------------------------------------------
# 2b. CITY → AIRPORT CODE MAP  (extend this when new AoD locations come online)
# -----------------------------------------------------------------------------

# AoD canonical franchisee codes. These are AoD's internal abbreviations
# (sourced from franchisees.csv + Mat's 2026-05-11 fill-ins), NOT IATA airport codes.
# Constant name is kept as CITY_TO_IATA for backward compat with existing call sites.
# When a new franchisee opens, update this map AND the matching memory file
# `reference_aod_location_codes.md`.
CITY_TO_IATA = {
    # Texas
    "Austin":               "AUS",
    "Dallas":               "DAL",
    "Dallas Fort Worth":    "DFW",
    "Houston":              "HOU",
    "San Antonio":          "SAN",
    # Florida
    "Fort Lauderdale":      "FTL",
    "Miami":                "MIA",
    "North Florida":        "JAX",
    "Orlando":              "MCO",
    "Sarasota":             "SRQ",
    "Tampa":                "TPA",
    # Georgia
    "Central Atlanta":      "CATL",
    "North Atlanta":        "NATL",
    # Alabama
    "Birmingham":           "BMH",
    "Gulf Shores":          "GLF",
    # Tennessee
    "East Tennessee":       "ETN",
    "Nashville":            "NVL",
    # Carolinas
    "Charleston":           "CRL",
    "Charlotte":            "CLT",
    "Raleigh":              "RAL",
    "Upstate South Carolina": "USC",
    # Mid-Atlantic / Northeast
    "Buffalo":              "BFL",
    "Cedar Grove":          "CDG",
    "Connecticut":          "CTNY",
    "New York City":        "NYC",
    "Philadelphia":         "PHL",
    "Pittsburgh":           "PIT",
    # Midwest
    "Chicago":              "CHI",
    "Chicago North Shore":  "CNS",
    "Cincinnati":           "CIN",
    "Cleveland":            "CLE",
    "Columbus":             "COL",
    "Detroit":              "DET",
    "Indianapolis":         "IND",
    "Kansas City":          "KSMO",
    "Omaha":                "OMH",
    "St. Louis":            "STL",
    "St Louis":             "STL",
    "Twin Cities":          "MIN",
    "West Michigan":        "WMI",
    # Arkansas
    "Northwest Arkansas":   "NWA",
    # Mountain West
    "Boise":                "BOI",
    "Denver":               "DEN",
    "Idaho":                "IDH",
    "Phoenix":              "PHX",
    "Salt Lake City":       "SLC",
    # Pacific Northwest
    "Portland":             "PTL",
    "Seattle":              "SEA",
    # Closed / archived — left here for reference; excluded by f.exclude_from_reports='n'
    # "Milwaukee":          (closed),
    "Northern Colorado":    "NCO",
}

_AOD_PREFIX = re.compile(r"^Art of Drawers\s+", re.IGNORECASE)
_DIRECTIONAL_PREFIXES = ("North ", "South ", "East ", "West ", "Central ", "Greater ")

def location_to_iata(location_name):
    """Look up the airport code for a franchisee display_name (best-effort)."""
    if not location_name:
        return ""
    city = _AOD_PREFIX.sub("", location_name).strip()

    # 1. Exact match
    if city in CITY_TO_IATA:
        return CITY_TO_IATA[city]
    # 2. Strip directional prefix and retry
    for pref in _DIRECTIONAL_PREFIXES:
        if city.startswith(pref):
            stripped = city[len(pref):]
            if stripped in CITY_TO_IATA:
                return CITY_TO_IATA[stripped]
    # 3. Substring match (e.g. "Atlanta Northwest" → "Atlanta")
    for known_city, iata in CITY_TO_IATA.items():
        if known_city.lower() in city.lower():
            return iata
    # 4. Fallback: first 3 letters of city, uppercased
    fallback = re.sub(r"[^A-Za-z]", "", city)[:3].upper()
    print(f"  ! Unknown location for IATA mapping: '{location_name}' → using fallback '{fallback}'", file=sys.stderr)
    return fallback or "?"


# -----------------------------------------------------------------------------
# 2c. SPARKLINE — smooth SVG curve drawn into the card background
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# 3. DATE WINDOWS
# -----------------------------------------------------------------------------

def date_windows(today=None):
    """
    Build all date windows we need. End dates are INCLUSIVE for the user-facing
    "last X days" interpretation. SQL needs end-exclusive — we add a day later.
    """
    today = today or datetime.date.today()
    return {
        "today":        today,
        # R30 current and prior (adjacent 30-day windows).
        "r30_current": (today - datetime.timedelta(days=30), today),
        "r30_prior":   (today - datetime.timedelta(days=60), today - datetime.timedelta(days=30)),
        # R7 current and prior — used for refacing revenue.
        "r7_current":  (today - datetime.timedelta(days=7),  today),
        "r7_prior":    (today - datetime.timedelta(days=14), today - datetime.timedelta(days=7)),
        # Next 7 days — used for design appointment count + top locations/designers.
        "next7":       (today, today + datetime.timedelta(days=7)),
        # Previous 7 days that just passed — used for the appointments indicator comparison.
        "prev7":       (today - datetime.timedelta(days=7), today),
    }


# -----------------------------------------------------------------------------
# 4. CANVAS QUERIES
# -----------------------------------------------------------------------------

# Common franchisee filters — keep production locations only.
# These mirror canvas_data.STANDARD_FRANCHISEE_FILTER and now also exclude
# franchisee_id=1 (ILM, internal lab) per Canvas dev guidance (May 2026).
# See ../CANVAS-MCP-RULES.md.
FRANCHISEE_FILTER = """
  AND f.active = 'y'
  AND f.exclude_from_reports = 'n'
  AND f.id != 1                              -- ILM (internal lab) — excluded from prod reporting
  AND f.display_name NOT LIKE '%Test%'
  AND f.display_name NOT LIKE '%Training%'
"""

# Job filter — mirrors canvas_data.STANDARD_JOB_FILTER. Excludes deleted jobs
# (current_status_id=19), which previous versions of these queries did NOT
# filter out, slightly overstating revenue and counts vs. what Canvas shows.
JOB_FILTER = """
  AND j.active = 'y'
  AND j.current_status_id != 19              -- exclude deleted jobs
"""

# Customer-payment filter — mirrors canvas_data.STANDARD_PAYMENT_FILTER.
# include='y' is required on customer_payment when summing/counting; without
# it, deposit totals overshoot what Canvas reports.
PAYMENT_FILTER_INCLUDE = "AND include = 'y'"


def _fmt_dt(d):
    """Format a datetime.date as 'YYYY-MM-DD' for use in SQL."""
    return d.isoformat()


def revenue_in_window(start_date, end_date_inclusive):
    """
    Total job.order_total for NEW jobs (job_type_id=1) whose first
    customer_payment (deposit) date_added falls in [start, end_inclusive].
    Applies the standard AoD exclusions (see reference_aod_canvas_conventions).
    """
    end_exclusive = end_date_inclusive + datetime.timedelta(days=1)
    # KPI-TOOL TODO: when admin approval lands, replace this whole query with
    # mcp__<canvas>__get_revenue (date_start, date_end). The KPI tool already
    # bakes in the franchisee/job/payment filters and is one MCP call vs.
    # this multi-table aggregation. See ../CANVAS-MCP-RULES.md.
    sql = f"""
    SELECT COALESCE(SUM(j.order_total), 0) AS rev
    FROM job j
    INNER JOIN franchisee f ON f.id = j.franchisee_id
    INNER JOIN (
        SELECT job_id, MIN(date_added) AS first_payment
        FROM customer_payment
        WHERE active = 'y'
          {PAYMENT_FILTER_INCLUDE}
          AND job_id IS NOT NULL
        GROUP BY job_id
    ) cp ON cp.job_id = j.id
    WHERE 1=1
      {JOB_FILTER}
      AND j.job_type_id = 1   -- New orders only
      {FRANCHISEE_FILTER}
      AND cp.first_payment >= '{_fmt_dt(start_date)}'
      AND cp.first_payment <  '{_fmt_dt(end_exclusive)}'
    """
    result = run_query(sql, output_format="json", max_rows=10)
    if result.get("error"):
        print(f"  ! revenue query error: {result['error']}", file=sys.stderr)
        return None
    rows = result.get("rows") or []
    if not rows:
        return 0.0
    return _to_float(rows[0].get("rev"))


def appointment_count(start_date, end_date_exclusive):
    """
    Count active, non-cancelled DESIGN appointments where date_and_time_starts is
    in [start, end_exclusive). Design = appointment_type_id 4 (Designer Appt.)
    or 30 (Self Gen Design Appt).
    """
    # KPI-TOOL TODO: replace with mcp__<canvas>__get_appointment_count once
    # admin approval lands. Single MCP call vs. this aggregation.
    sql = f"""
    SELECT COUNT(*) AS cnt
    FROM appointment a
    INNER JOIN franchisee f ON f.id = a.franchisee_id
    WHERE a.appointment_type_id IN (4, 30)
      AND a.cancelled = 'n'
      AND a.active = 'y'
      AND a.date_and_time_starts >= '{_fmt_dt(start_date)}'
      AND a.date_and_time_starts <  '{_fmt_dt(end_date_exclusive)}'
      {FRANCHISEE_FILTER}
    """
    result = run_query(sql, output_format="json", max_rows=10)
    if result.get("error"):
        print(f"  ! appt count error: {result['error']}", file=sys.stderr)
        return None
    rows = result.get("rows") or []
    return _to_int(rows[0].get("cnt")) if rows else 0


def top_locations_for_appts(start_date, end_date_exclusive, limit=3):
    """Top N locations by design-appointment count in the window. Honors ties at the cutoff."""
    # KPI-TOOL TODO: no direct KPI tool — closest is list_franchisee +
    # get_appointment_count per franchisee, which would be more MCP calls than
    # this single GROUP BY. Custom SQL is the right call here.
    sql = f"""
    SELECT f.display_name AS location, COUNT(*) AS cnt
    FROM appointment a
    INNER JOIN franchisee f ON f.id = a.franchisee_id
    WHERE a.appointment_type_id IN (4, 30)
      AND a.cancelled = 'n'
      AND a.active = 'y'
      AND a.date_and_time_starts >= '{_fmt_dt(start_date)}'
      AND a.date_and_time_starts <  '{_fmt_dt(end_date_exclusive)}'
      {FRANCHISEE_FILTER}
    GROUP BY f.id, f.display_name
    ORDER BY cnt DESC, f.display_name ASC
    LIMIT {limit + 5}
    """
    result = run_query(sql, output_format="json", max_rows=50)
    if result.get("error"):
        print(f"  ! top locations error: {result['error']}", file=sys.stderr)
        return []
    rows = result.get("rows") or []
    cleaned = [{"name": r["location"], "count": _to_int(r["cnt"])} for r in rows]
    return _keep_top_with_ties(cleaned, limit)


def top_designers_for_appts(start_date, end_date_exclusive, limit=3):
    """
    Top N designers by design-appointment count in the window. Honors ties.
    Each designer's "home" franchisee (the one for the majority of their appts
    in the window) is returned so we can render their location's airport code.
    """
    # KPI-TOOL TODO: no direct "appointments by designer" KPI. Custom SQL stays.
    sql = f"""
    SELECT TRIM(CONCAT(COALESCE(su.firstname, ''), ' ', COALESCE(su.lastname, ''))) AS designer,
           f.display_name AS location,
           COUNT(*) AS cnt
    FROM appointment a
    INNER JOIN franchisee f ON f.id = a.franchisee_id
    INNER JOIN siteuser   su ON su.id = a.siteuser_id
    WHERE a.appointment_type_id IN (4, 30)
      AND a.cancelled = 'n'
      AND a.active = 'y'
      AND a.date_and_time_starts >= '{_fmt_dt(start_date)}'
      AND a.date_and_time_starts <  '{_fmt_dt(end_date_exclusive)}'
      AND su.active = 'y'
      {FRANCHISEE_FILTER}
    GROUP BY su.id, su.firstname, su.lastname, f.id, f.display_name
    ORDER BY cnt DESC, su.lastname ASC
    LIMIT 30
    """
    result = run_query(sql, output_format="json", max_rows=30)
    if result.get("error"):
        print(f"  ! top designers error: {result['error']}", file=sys.stderr)
        return []
    rows = result.get("rows") or []

    # A single designer can have appointments at multiple locations. Collapse on name,
    # summing counts and picking the location where they have the most appointments.
    by_designer = {}
    for r in rows:
        name = r["designer"]
        cnt = _to_int(r["cnt"])
        loc = r["location"]
        entry = by_designer.setdefault(name, {"name": name, "count": 0, "home_loc": loc, "home_loc_cnt": 0})
        entry["count"] += cnt
        if cnt > entry["home_loc_cnt"]:
            entry["home_loc"] = loc
            entry["home_loc_cnt"] = cnt

    designers = sorted(by_designer.values(), key=lambda d: (-d["count"], d["name"]))
    designers = _keep_top_with_ties(designers, limit)

    # Decorate each with the IATA code for their home location
    for d in designers:
        d["iata"] = location_to_iata(d["home_loc"])
    return designers


def _keep_top_with_ties(rows, limit):
    """
    Given rows sorted by count desc, keep all rows tied with the rank-N row.
    If there's a 3-way tie for 3rd place, we'll keep all of them rather than
    arbitrarily cutting off.
    """
    if len(rows) <= limit:
        return rows
    cutoff = rows[limit - 1]["count"]
    return [r for r in rows if r["count"] >= cutoff]


# -----------------------------------------------------------------------------
# 5. SOLD-TO-INSTALL — calls the install-vs-deposit skill
# -----------------------------------------------------------------------------

def _refacing_csv_path(start_date, end_date_inclusive):
    """
    refacing_sales.py writes its CSV into the AoD_Cowork root it can see —
    Mat's Mac path when available, otherwise the sandbox mount under
    /sessions/.../mnt/AoD_Cowork/. Return whichever exists; if neither
    exists yet, return the most likely candidate so the caller can probe.
    """
    name = f"Refacing_Sales_{start_date}_to_{end_date_inclusive}.csv"
    mac = "/Users/artofdrawersllc/Documents/Claude/Projects/AoD_Cowork/" + name
    sandbox = os.path.join(_AOD_COWORK_ROOT, name)
    if os.path.exists(mac):
        return mac
    if os.path.exists(sandbox):
        return sandbox
    # Prefer the path that matches the running environment.
    return mac if os.path.isdir("/Users/artofdrawersllc/Documents/Claude/Projects/AoD_Cowork") else sandbox


def run_install_vs_deposit(start_date, end_date_inclusive):
    """
    Spawn the install_vs_deposit.py skill, parse its CSV, return
    (median_days, pct_under_10_weeks, n_rows).
    """
    # Write the intermediate CSV next to the dashboard (on the same mount as
    # everything else) rather than to /tmp. The Cowork sandbox's /tmp is a
    # throwaway, per-invocation filesystem; if a write or the read-back below
    # ever fails there, the Sold-to-Install cards silently blank out. .cache/
    # is gitignored and always writable.
    cache_dir = os.path.join(HERE, ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    out_csv = os.path.join(cache_dir, f"aod_ivd_{start_date}_to_{end_date_inclusive}.csv")
    cmd = [
        "python3", INSTALL_VS_DEPOSIT_SCRIPT,
        "--start", str(start_date),
        "--end",   str(end_date_inclusive),
        "--output", out_csv,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    if proc.returncode != 0:
        # Bumped from [:500] to [:3000] — the truncated form was just printing the
        # traceback source line (e.g. line 113 of install_vs_deposit.py) and cutting
        # off the actual exception type/message, which made errors look like syntax bugs.
        print(f"  ! install_vs_deposit failed (rc={proc.returncode}): {proc.stderr[:3000]}", file=sys.stderr)
        return None, None, 0

    days_list = []
    under_count = 0
    n = 0
    # Diagnostics: count how rows were classified, so a silent "no data" outcome is loud.
    total_rows = 0
    skipped_blank = 0
    skipped_nonint = 0
    skipped_negative = 0
    try:
        with open(out_csv) as fh:
            for row in csv.DictReader(fh):
                total_rows += 1
                # The skill includes a Days_Deposit_To_Install int column and Under_10_Weeks Y/N.
                raw = (row.get("Days_Deposit_To_Install") or "").strip()
                if not raw:
                    skipped_blank += 1
                    continue
                try:
                    d = int(raw)
                except ValueError:
                    skipped_nonint += 1
                    continue
                if d < 0:
                    # Negative = install before deposit (data quality issue). Skip from median.
                    skipped_negative += 1
                    continue
                days_list.append(d)
                n += 1
                if (row.get("Under_10_Weeks") or "").strip() == "Y":
                    under_count += 1
    except FileNotFoundError:
        print(f"  ! install_vs_deposit CSV not found at {out_csv}", file=sys.stderr)
        return None, None, 0

    if not days_list:
        # SILENT-FAILURE GUARD — script returned rc=0 but produced no usable rows.
        # Print everything an operator needs to debug without re-running the job.
        print(
            f"  ! install_vs_deposit produced NO usable rows for {start_date}→{end_date_inclusive}: "
            f"csv={out_csv}, csv_rows={total_rows}, skipped_blank={skipped_blank}, "
            f"skipped_nonint={skipped_nonint}, skipped_negative={skipped_negative}",
            file=sys.stderr,
        )
        # Tail of stdout/stderr from the skill is often the clue (Canvas auth, timeout, empty SQL result, etc.)
        if proc.stdout:
            print(f"    stdout tail: {proc.stdout[-400:].strip()}", file=sys.stderr)
        if proc.stderr:
            print(f"    stderr tail: {proc.stderr[-400:].strip()}", file=sys.stderr)
        return None, None, 0

    days_list.sort()
    mid = len(days_list) // 2
    if len(days_list) % 2 == 1:
        median = days_list[mid]
    else:
        median = (days_list[mid - 1] + days_list[mid]) / 2.0
    pct = (under_count / n) * 100.0 if n else None
    return median, pct, n


# -----------------------------------------------------------------------------
# 6. REFACING REVENUE — calls the refacing-sales skill
# -----------------------------------------------------------------------------

def run_refacing_summary(start_date, end_date_inclusive):
    """
    Run refacing_sales.py for the window and return (revenue, job_count, None).
    job_count = number of distinct refacing jobs (matches the canonical definition:
    a job with at least 5 combined doors + drawers per the refacing-sales skill).
    Returns (None, None, None) on failure.
    """
    cmd = ["python3", REFACING_SALES_SCRIPT, str(start_date), str(end_date_inclusive)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        print(f"  ! refacing_sales failed: {proc.stderr[:500]}", file=sys.stderr)
        return None, None

    csv_path = _refacing_csv_path(start_date, end_date_inclusive)
    if not os.path.exists(csv_path):
        # Try to parse from stdout — total revenue + total jobs lines
        total_rev = None
        total_jobs = None
        m = re.search(r"Total revenue:\s*\$?([\d,]+\.\d{2})", proc.stdout)
        if m:
            total_rev = float(m.group(1).replace(",", ""))
        m = re.search(r"Total jobs:\s*(\d+)", proc.stdout)
        if m:
            total_jobs = int(m.group(1))
        return total_rev, total_jobs

    total_rev = 0.0
    total_jobs = 0
    with open(csv_path) as fh:
        for row in csv.DictReader(fh):
            jid = (row.get("job_id") or "").strip().upper()
            if jid == "TOTAL":
                continue
            try:
                total_rev += float(row.get("revenue") or 0)
            except ValueError:
                pass
            total_jobs += 1
    return total_rev, total_jobs


# Backward-compat shim — old callers expect a single float
def run_refacing_revenue(start_date, end_date_inclusive):
    rev, _ = run_refacing_summary(start_date, end_date_inclusive)
    return rev


# -----------------------------------------------------------------------------
# 7. MFG PARTNER SHEET — Claim Line Items %
# -----------------------------------------------------------------------------

def _infer_year(month, day, today):
    """
    Given a MM/DD and today's date, infer the most recent past year the
    date could refer to. e.g. on 2026-05-11: '04/15' -> 2026, '07/21' -> 2025.
    """
    try:
        candidate = datetime.date(today.year, month, day)
    except ValueError:
        return None
    if candidate > today:
        try:
            candidate = datetime.date(today.year - 1, month, day)
        except ValueError:
            return None
    return candidate


def _parse_mfg_date(raw, today):
    """
    Parse an Order Date cell from the Mfg Partner Analysis sheet. The sheet
    uses two formats interchangeably:
      - 'MM/DD/YYYY' (recent rows — explicit year, use as-is)
      - 'MM/DD'     (older historical rows — infer the most recent past year)
    Returns a datetime.date or None when the value is unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()
    # Try MM/DD/YYYY first
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    # Fall back to MM/DD (infer year)
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", raw)
    if m:
        return _infer_year(int(m.group(1)), int(m.group(2)), today)
    return None


def fetch_mfg_claim_counts(start_date, end_date_inclusive):
    """
    Fetch the published-CSV version of the Mfg Partner Analysis sheet, filter rows
    whose inferred Order Date falls in the window, and return
    (claim_line_items, total_line_items). Returns (None, None) if the URL isn't
    configured or the fetch fails.

    A row's contribution to the totals is its 'Line Items Count' value. Blank/zero
    counts contribute zero — that's the "not enough data" case the indicator handles.
    """
    if not MFG_SHEET_CSV_URL:
        print("  ! AOD_MFG_SHEET_CSV_URL not set — skipping Mfg sheet metric", file=sys.stderr)
        return None, None

    today = datetime.date.today()
    try:
        req = Request(MFG_SHEET_CSV_URL, headers={"User-Agent": "AoD-Dashboard/1.0"})
        resp = urlopen(req, timeout=30)
        content = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ! Mfg sheet fetch error: {e}", file=sys.stderr)
        return None, None

    claim_items = 0
    total_items = 0

    for row in csv.DictReader(io.StringIO(content)):
        order_type = (row.get("Type") or "").strip()
        if order_type not in ("Claim", "Reorder", "Job"):
            continue

        dt = _parse_mfg_date(row.get("Order Date"), today)
        if dt is None:
            continue
        if not (start_date <= dt <= end_date_inclusive):
            continue

        # Each row's Line Items Count is the contribution. Treat blank as 0.
        raw_li = (row.get("Line Items Count") or "").strip()
        try:
            li = int(float(raw_li)) if raw_li else 0
        except ValueError:
            li = 0

        total_items += li
        if order_type == "Claim":
            claim_items += li

    return claim_items, total_items


# -----------------------------------------------------------------------------
# 7c. SHIPPING — cost-per-lb, pallet %, surcharge % (R14)
# -----------------------------------------------------------------------------
#
# Reads the WWEX invoice .xls files using the parser script that lives under the
# shipping-cost-analysis skill. The parser yields one record per shipment with
# weight, total $, base_freight, is_pallet, ship_date, and a list of surcharges.
#
# The dashboard uses an R14 window for these metrics (Mat's choice — matches the
# biweekly invoice cycle). On Wednesdays, fresh invoices are downloaded BEFORE
# the refresh runs (see wwex-invoice-downloader skill).

_shipping_parser_rel = "skills/shipping-cost-analysis/scripts"
SHIPPING_PARSER_CANDIDATES = [
    "/Users/artofdrawersllc/Documents/Claude/Projects/AoD_Cowork/" + _shipping_parser_rel,
    os.path.join(_AOD_COWORK_ROOT, _shipping_parser_rel),
    _sandbox_glob(_shipping_parser_rel) or "",
]

_shipping_loader_cached = None

def _get_shipping_loader():
    """Lazy-import parse_wwex_invoices.load_shipments."""
    global _shipping_loader_cached
    if _shipping_loader_cached is None:
        for d in SHIPPING_PARSER_CANDIDATES:
            if os.path.exists(d) and d not in sys.path:
                sys.path.insert(0, d)
        try:
            from parse_wwex_invoices import load_shipments
            _shipping_loader_cached = load_shipments
        except ImportError as e:
            print(f"  ! shipping parser unavailable: {e}", file=sys.stderr)
            _shipping_loader_cached = lambda *a, **kw: []
    return _shipping_loader_cached


def _is_fuel_surcharge(name):
    """Surcharge name contains 'fuel' (case-insensitive)."""
    return name and "fuel" in name.lower()


def latest_invoice_ship_date(today=None, lookback_days=90):
    """
    Return the most recent ship_date present in the WWEX invoices (within the
    last `lookback_days`), or None if none can be found.

    Why this exists: invoices lag the calendar (e.g. on 5/12, the freshest
    invoice may only cover shipments through 4/30). Anchoring the R14 window
    to "today" causes the current bucket to be sparse. Instead, we anchor to
    the most recent ship date so R14 always spans 14 days of real data.
    """
    today = today or datetime.date.today()
    load = _get_shipping_loader()
    start = today - datetime.timedelta(days=lookback_days)
    try:
        shipments = load(start_date=str(start), end_date=str(today))
    except Exception as e:
        print(f"  ! latest_invoice_ship_date load error: {e}", file=sys.stderr)
        return None
    latest = None
    for s in shipments or []:
        sd_raw = s.get("ship_date") or ""
        try:
            sd = datetime.date.fromisoformat(sd_raw)
        except ValueError:
            continue
        if latest is None or sd > latest:
            latest = sd
    return latest


def shipping_window_summary(start_date, end_date_inclusive):
    """
    Returns a dict with cost_per_lb, pallet_pct, surcharge_pct_ex_fuel,
    earliest_ship, latest_ship, and n_shipments — for the given date window.
    Returns None on failure or empty data.
    """
    load = _get_shipping_loader()
    try:
        shipments = load(
            start_date=str(start_date),
            end_date=str(end_date_inclusive),
        )
    except Exception as e:
        print(f"  ! shipping load error: {e}", file=sys.stderr)
        return None
    if not shipments:
        return None

    total_cost = sum(s.get("total") or 0 for s in shipments)
    total_weight = sum(s.get("weight") or 0 for s in shipments)
    n_pallet = sum(1 for s in shipments if s.get("is_pallet"))
    n_total = len(shipments)

    total_surcharges = 0.0
    fuel_surcharges = 0.0
    for s in shipments:
        for sc in s.get("surcharges") or []:
            amt = sc.get("amount") or 0
            total_surcharges += amt
            if _is_fuel_surcharge(sc.get("type")):
                fuel_surcharges += amt

    cost_per_lb = (total_cost / total_weight) if total_weight > 0 else None
    pallet_pct = (n_pallet / n_total * 100.0) if n_total > 0 else None
    nonfuel_surcharges = total_surcharges - fuel_surcharges
    surcharge_pct_ex_fuel = (nonfuel_surcharges / total_cost * 100.0) if total_cost > 0 else None

    ship_dates = [s.get("ship_date") for s in shipments if s.get("ship_date")]
    earliest = min(ship_dates) if ship_dates else None
    latest = max(ship_dates) if ship_dates else None

    return {
        "cost_per_lb": cost_per_lb,
        "pallet_pct": pallet_pct,
        "surcharge_pct_ex_fuel": surcharge_pct_ex_fuel,
        "n_shipments": n_total,
        "total_cost": total_cost,
        "total_weight": total_weight,
        "earliest_ship": earliest,
        "latest_ship": latest,
    }


def _fmt_ship_date_span(earliest, latest):
    """Render a 'Ships M/D – M/D' string from two YYYY-MM-DD strings."""
    if not earliest or not latest:
        return ""
    try:
        e = datetime.date.fromisoformat(earliest)
        l = datetime.date.fromisoformat(latest)
        return f"Ships {e.month}/{e.day}–{l.month}/{l.day}"
    except ValueError:
        return ""


# -----------------------------------------------------------------------------
# 8. RENDER
# -----------------------------------------------------------------------------
#
# NOTE: All sparkline trendlines were removed 2026-05-21 (functions + the
# sparkline_svg renderer + the template backgrounds). Card numbers are now
# centered. The planned weekly deep-report skill is the right place to compute
# real trend history; revive the trend functions from git history if needed.

def _esc(s):
    """Minimal HTML escape."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_list_items(items, count_label, show_iata=False):
    """
    Render <li> rows for the top-3 lists.

    items: list of dicts with at least "name" and "count". If show_iata=True,
           each dict should also have an "iata" key with a 3-letter code.
    """
    if not items:
        return '<li><span class="name" style="color:var(--text-secondary)">No appointments scheduled</span><span class="count"></span></li>'
    parts = []
    for it in items:
        name = _esc(it["name"])
        if show_iata and it.get("iata"):
            iata_badge = f'<span class="iata">{_esc(it["iata"])}</span>'
        else:
            iata_badge = ""
        parts.append(
            f'<li><span class="name">{name}</span>{iata_badge}<span class="count">{it["count"]} {count_label}</span></li>'
        )
    return "\n            ".join(parts)


def render(replacements):
    with open(TEMPLATE_PATH) as fh:
        html = fh.read()
    for token, value in replacements.items():
        html = html.replace(token, value)
    with open(OUTPUT_PATH, "w") as fh:
        fh.write(html)


# -----------------------------------------------------------------------------
# 9. MAIN
# -----------------------------------------------------------------------------

def main():
    w = date_windows()
    started = datetime.datetime.now()
    print(f"== AoD Operations Dashboard Refresh — {started:%Y-%m-%d %H:%M:%S} ==")

    # 9a. Revenue R30 current + prior
    print("→ Revenue (R30 current)...")
    rev_cur = revenue_in_window(*w["r30_current"])
    print(f"   = {fmt_currency(rev_cur)}")
    print("→ Revenue (R30 prior)...")
    rev_prv = revenue_in_window(*w["r30_prior"])
    print(f"   = {fmt_currency(rev_prv)}")

    # 9b. Design Appointments — next 7 + prev 7
    next7_start, next7_end_excl = w["next7"]
    prev7_start, prev7_end_excl = w["prev7"]
    print("→ Design appointments (next 7)...")
    appt_next = appointment_count(next7_start, next7_end_excl)
    print(f"   = {appt_next}")
    print("→ Design appointments (prev 7)...")
    appt_prev = appointment_count(prev7_start, prev7_end_excl)
    print(f"   = {appt_prev}")

    print("→ Top locations (next 7)...")
    top_locs = top_locations_for_appts(next7_start, next7_end_excl, limit=3)
    print(f"   = {top_locs}")
    print("→ Top designers (next 7)...")
    top_dsrs = top_designers_for_appts(next7_start, next7_end_excl, limit=3)
    print(f"   = {top_dsrs}")

    # 9c. Sold-to-Install (current + prior)
    print("→ Install-vs-Deposit (R30 current)...")
    s2i_med_cur, s2i_pct_cur, _ = run_install_vs_deposit(*w["r30_current"])
    print(f"   median={s2i_med_cur} days   pct<10wk={s2i_pct_cur}")
    print("→ Install-vs-Deposit (R30 prior)...")
    s2i_med_prv, s2i_pct_prv, _ = run_install_vs_deposit(*w["r30_prior"])
    print(f"   median={s2i_med_prv} days   pct<10wk={s2i_pct_prv}")

    # 9d. Refacing Revenue + Jobs R7
    print("→ Refacing summary (R7 current)...")
    rf_cur, rfj_cur = run_refacing_summary(*w["r7_current"])
    print(f"   revenue={fmt_currency(rf_cur)}  jobs={rfj_cur}")
    print("→ Refacing summary (R7 prior)...")
    rf_prv, rfj_prv = run_refacing_summary(*w["r7_prior"])
    print(f"   revenue={fmt_currency(rf_prv)}  jobs={rfj_prv}")

    # 9e. Mfg Claim Line Items % (current + prior)
    print("→ Mfg sheet — claim % (R30 current)...")
    claim_cur, total_cur = fetch_mfg_claim_counts(*w["r30_current"])
    print(f"   claim_items={claim_cur}  total_items={total_cur}")
    print("→ Mfg sheet — claim % (R30 prior)...")
    claim_prv, total_prv = fetch_mfg_claim_counts(*w["r30_prior"])
    print(f"   claim_items={claim_prv}  total_items={total_prv}")

    claim_pct_cur = (claim_cur / total_cur * 100) if total_cur else None
    claim_pct_prv = (claim_prv / total_prv * 100) if total_prv else None
    # "Not enough data" threshold: at least 10 line items in BOTH windows for a stable comparison.
    claim_insufficient = (
        total_cur is None or total_prv is None or total_cur < 10 or total_prv < 10
    )

    # 9f. Bonus pace (H1 2026) for the bonus-tied cards.
    # The card headline stays the rolling window above; these compute the
    # period-to-date pace that drives the subtle outline + status pill.
    # Skill-based YTD calls (refacing) are skipped during the offline/emit pass
    # since they don't use the query cache and would just burn time there.
    offline = os.environ.get("AOD_CANVAS_OFFLINE", "") == "1"

    print("→ Bonus pace — System Sales (H1 YTD)...")
    ss_ytd = revenue_in_window(BONUS_PERIOD_START, w["today"])
    ss_pace = bonus_pace("system_sales", ss_ytd, today=w["today"])
    print(f"   YTD={fmt_currency(ss_ytd)}  pace={fmt_currency(ss_pace)}")

    print("→ Bonus pace — Refacing Revenue (H1 YTD)...")
    if offline:
        rf_ytd = None
    else:
        rf_ytd = run_refacing_revenue(BONUS_PERIOD_START, w["today"])
    rf_pace = bonus_pace("refacing_revenue", rf_ytd, today=w["today"])
    print(f"   YTD={fmt_currency(rf_ytd)}  pace={fmt_currency(rf_pace)}")

    print("→ Bonus pace — Claim % (H1 YTD)...")
    claim_ytd_items, total_ytd_items = fetch_mfg_claim_counts(BONUS_PERIOD_START, w["today"])
    claim_pct_ytd = (claim_ytd_items / total_ytd_items * 100) if total_ytd_items else None
    claim_bonus_pace = bonus_pace("claim_pct", claim_pct_ytd, today=w["today"])
    print(f"   YTD claim%={claim_pct_ytd}  (items={claim_ytd_items}/{total_ytd_items})")

    # Shipping (R14 — Mat's choice to match WWEX biweekly invoice cycle).
    # Anchor the window to the most recent ship date in the invoices rather than
    # today's calendar date. Invoices lag the calendar, so anchoring to today
    # causes a sparse current bucket. Falls back to today when no invoices exist.
    ship_anchor = latest_invoice_ship_date(today=w["today"]) or w["today"]
    print(f"→ Shipping anchor date (most recent ship_date): {ship_anchor}")
    r14_current = (ship_anchor - datetime.timedelta(days=14), ship_anchor)
    r14_prior   = (ship_anchor - datetime.timedelta(days=28), ship_anchor - datetime.timedelta(days=14))
    print("→ Shipping (R14 current)...")
    ship_cur = shipping_window_summary(*r14_current) or {}
    print(f"   cost/lb={ship_cur.get('cost_per_lb')}  pallet%={ship_cur.get('pallet_pct')}  surch%={ship_cur.get('surcharge_pct_ex_fuel')}  n={ship_cur.get('n_shipments')}  ships {ship_cur.get('earliest_ship')}→{ship_cur.get('latest_ship')}")
    print("→ Shipping (R14 prior)...")
    ship_prv = shipping_window_summary(*r14_prior) or {}
    print(f"   cost/lb={ship_prv.get('cost_per_lb')}  pallet%={ship_prv.get('pallet_pct')}  surch%={ship_prv.get('surcharge_pct_ex_fuel')}  n={ship_prv.get('n_shipments')}  ships {ship_prv.get('earliest_ship')}→{ship_prv.get('latest_ship')}")
    # 9g. Indicator HTML for every metric
    last_updated = datetime.datetime.now().strftime("%a %b %-d · %-I:%M %p ET")

    ship_span = _fmt_ship_date_span(ship_cur.get("earliest_ship"), ship_cur.get("latest_ship"))

    replacements = {
        "{{LAST_UPDATED}}": last_updated,

        # AoD Network — System Sales (bonus-tied: pill = H1 pace, headline = R30)
        "{{REVENUE_VALUE}}":     fmt_currency(rev_cur),
        "{{REVENUE_INDICATOR}}": indicator_html(pct_change(rev_cur, rev_prv), lower_is_better=False),
        "{{REVENUE_BONUS_CLASS}}": bonus_class("system_sales", ss_pace),
        "{{REVENUE_BONUS_PILL}}":  bonus_pill_html("system_sales", ss_pace),

        "{{APPT_COUNT}}":     str(appt_next if appt_next is not None else "—"),
        "{{APPT_INDICATOR}}": indicator_html(pct_change(appt_next, appt_prev), lower_is_better=False),

        "{{TOP_LOCATIONS}}":  render_list_items(top_locs, "appts"),
        "{{TOP_DESIGNERS}}":  render_list_items(top_dsrs, "appts", show_iata=True),

        # Refacing (bonus-tied: pill = H1 pace, headline = R7)
        "{{REFACING_VALUE}}":     fmt_currency(rf_cur, abbreviate=True),
        "{{REFACING_INDICATOR}}": indicator_html(pct_change(rf_cur, rf_prv), lower_is_better=False),
        "{{REFACING_BONUS_CLASS}}": bonus_class("refacing_revenue", rf_pace),
        "{{REFACING_BONUS_PILL}}":  bonus_pill_html("refacing_revenue", rf_pace),

        "{{REFACING_JOBS_VALUE}}":     str(rfj_cur if rfj_cur is not None else "—"),
        "{{REFACING_JOBS_INDICATOR}}": indicator_html(pct_change(rfj_cur, rfj_prv), lower_is_better=False),

        # Network Lead Times
        "{{S2I_MEDIAN_VALUE}}":     fmt_weeks_days(s2i_med_cur),
        "{{S2I_MEDIAN_INDICATOR}}": indicator_html(pct_change(s2i_med_cur, s2i_med_prv), lower_is_better=True),

        "{{S2I_PCT_VALUE}}":     fmt_pct(s2i_pct_cur),
        "{{S2I_PCT_INDICATOR}}": indicator_html(pct_change(s2i_pct_cur, s2i_pct_prv), lower_is_better=False),

        # TAT (Order → Ship) — bonus metric, placeholder until query/skill is wired.
        "{{TAT_VALUE}}":        "—",
        "{{TAT_BONUS_CLASS}}":  "",
        "{{TAT_BONUS_PILL}}":   '<span class="bonus-pill neutral">No data yet</span>',
        "{{TAT_SUBLABEL}}":     "H1 bonus · target 18d",

        # Manufacturing — Claim % (bonus-tied: pill = H1 YTD claim %)
        "{{CLAIM_PCT_VALUE}}":     fmt_pct(claim_pct_cur, decimals=2),
        "{{CLAIM_PCT_INDICATOR}}": indicator_html(
            pct_change(claim_pct_cur, claim_pct_prv),
            lower_is_better=True,
            insufficient_data=claim_insufficient,
        ),
        "{{CLAIM_BONUS_CLASS}}": bonus_class("claim_pct", claim_bonus_pace),
        "{{CLAIM_BONUS_PILL}}":  bonus_pill_html("claim_pct", claim_bonus_pace),

        # Shipping (R14)
        "{{COST_PER_LB_VALUE}}":     fmt_currency(ship_cur.get("cost_per_lb"), decimals=2) if ship_cur.get("cost_per_lb") is not None else "—",
        "{{COST_PER_LB_INDICATOR}}": indicator_html(
            pct_change(ship_cur.get("cost_per_lb"), ship_prv.get("cost_per_lb")),
            lower_is_better=True,
        ),

        "{{PALLET_PCT_VALUE}}":     fmt_pct(ship_cur.get("pallet_pct")),
        # Pallet % — HIGHER is better (more pallet shipments = better packing/cost).
        # Increase → green, decrease → red.
        "{{PALLET_PCT_INDICATOR}}": indicator_html(
            pct_change(ship_cur.get("pallet_pct"), ship_prv.get("pallet_pct")),
            lower_is_better=False,
        ),

        "{{SURCHARGE_PCT_VALUE}}":     fmt_pct(ship_cur.get("surcharge_pct_ex_fuel")),
        "{{SURCHARGE_PCT_INDICATOR}}": indicator_html(
            pct_change(ship_cur.get("surcharge_pct_ex_fuel"), ship_prv.get("surcharge_pct_ex_fuel")),
            lower_is_better=True,
        ),

        "{{SHIP_SPAN}}": ship_span or "—",
    }

    render(replacements)
    elapsed = (datetime.datetime.now() - started).total_seconds()
    print(f"\n✓ Wrote dashboard to {OUTPUT_PATH}  (took {elapsed:.1f}s)")

    # Push the fresh index.html to GitHub so the live dashboard updates.
    # No-op if this isn't a git repo, or if the push is suppressed via AOD_SKIP_GIT_PUSH=1.
    if os.environ.get("AOD_SKIP_GIT_PUSH") == "1":
        print("\n(Skipping git push — AOD_SKIP_GIT_PUSH=1)")
    else:
        push_to_github(HERE)


def _cleanup_stale_git_locks(git_dir):
    """Remove or rename any stale *.lock files inside .git/.

    Why this exists: Git creates short-lived lock files (.git/index.lock,
    .git/HEAD.lock) while it works, and normally deletes them in milliseconds.
    But when this script runs inside the Cowork sandbox, the FUSE mount
    refuses unlink() calls inside .git/ ("Operation not permitted"). Each
    git command therefore leaves a lock behind, breaking the *next* git
    command with "Unable to create '.git/index.lock': File exists."

    Strategy: try os.unlink() first (always works on Mat's Mac — no-op there
    if no locks exist). If unlink fails, rename the lock to .lock.old, which
    git ignores. If both fail, log and continue — the API fallback path will
    take over.
    """
    if not os.path.isdir(git_dir):
        return
    for name in ("index.lock", "HEAD.lock"):
        path = os.path.join(git_dir, name)
        if not os.path.exists(path):
            continue
        try:
            os.unlink(path)
        except OSError:
            # Sandbox can't unlink — try renaming instead. os.replace
            # overwrites any existing target on Unix.
            try:
                os.replace(path, path + ".old")
            except OSError as e:
                print(f"  ! could not clear {name}: {e}", file=sys.stderr)


def _push_via_github_api(repo_dir, message):
    """Publish index.html through GitHub's Contents API (no git needed).

    Used as a fallback when ``git push`` fails — typically because of stale
    .git/ locks in the sandbox that we couldn't clean up. Requires
    GITHUB_TOKEN to be set (loaded from .env). Returns True on success.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("  ! GITHUB_TOKEN not set — cannot use API fallback.", file=sys.stderr)
        return False

    # Figure out which repo to publish to, by reading the origin URL.
    try:
        remote_url = subprocess.run(
            ["git", "-C", repo_dir, "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        print("  ! Could not read origin URL — cannot use API fallback.", file=sys.stderr)
        return False

    m = re.match(r"https://github\.com/([^/]+/[^/.]+?)(?:\.git)?/?$", remote_url)
    if not m:
        print(f"  ! Origin URL not GitHub HTTPS: {remote_url}", file=sys.stderr)
        return False
    repo = m.group(1)  # e.g. "mfluker/aod-ops-dashboard"

    html_path = os.path.join(repo_dir, "index.html")
    with open(html_path, "rb") as f:
        local_bytes = f.read()

    # GitHub's "sha" for a file is the git blob SHA-1: sha1("blob " + len + "\0" + content).
    # Computing it locally lets us no-op if the live file already matches.
    blob_header = f"blob {len(local_bytes)}\0".encode()
    local_blob_sha = hashlib.sha1(blob_header + local_bytes).hexdigest()

    api_url = f"https://api.github.com/repos/{repo}/contents/index.html"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "aod-ops-dashboard-refresh",
    }

    # GET the current file SHA on the default branch
    remote_sha = None
    try:
        with urlopen(Request(api_url, headers=headers)) as resp:
            remote_sha = json.loads(resp.read().decode()).get("sha")
    except HTTPError as e:
        if e.code != 404:
            print(f"  ! API GET failed: HTTP {e.code}", file=sys.stderr)
            return False
        # 404 = file doesn't exist yet, that's fine — first publish.

    if remote_sha and remote_sha == local_blob_sha:
        print("\n(No change to index.html — nothing to push.)")
        return True

    payload = {
        "message": message,
        "content": base64.b64encode(local_bytes).decode(),
        "branch": "main",
    }
    if remote_sha:
        payload["sha"] = remote_sha

    req = Request(
        api_url,
        method="PUT",
        data=json.dumps(payload).encode(),
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            commit_sha = result.get("commit", {}).get("sha", "?")[:7]
            print(f"\n✓ Pushed via GitHub API  (commit {commit_sha} — {message})")
            return True
    except HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300] if e.fp else ""
        print(f"  ! API PUT failed: HTTP {e.code} {body}", file=sys.stderr)
        return False


def push_to_github(repo_dir):
    """Stage index.html, commit, and push. Safe to call repeatedly — quietly no-ops if there's nothing to push.

    Two safety nets are layered in:
      1. Stale-lock cleanup: leftover .git/*.lock files (common in the
         Cowork sandbox where unlink() in .git/ is forbidden) are renamed
         out of the way before any git command runs.
      2. GitHub API fallback: if the git path still fails — or the push
         itself errors out — index.html is published via the Contents API
         instead, so the live dashboard updates either way.
    """
    git_dir = os.path.join(repo_dir, ".git")
    if not os.path.isdir(git_dir):
        print(f"\n(No .git folder in {repo_dir} — skipping push.)")
        return

    # Safety guard — NEVER publish a file containing git conflict markers.
    #
    # This is a hard backstop: index.html is regenerated from template.html on
    # every run, so markers should be impossible. But this dashboard is a TV
    # display where a silently broken file is highly visible, so we refuse to
    # commit or push index.html if it contains a conflict marker for any reason.
    # A genuine separator line is exactly seven <, =, or > characters; the
    # `(?: |$)` anchor avoids false positives on decorative `========` rules.
    index_path = os.path.join(repo_dir, "index.html")
    try:
        with open(index_path, encoding="utf-8") as fh:
            index_html = fh.read()
    except OSError as e:
        print(f"\n! Could not read index.html for the conflict-marker check: {e}", file=sys.stderr)
        return
    if re.search(r"^(?:<{7}|={7}|>{7})(?: |$)", index_html, re.MULTILINE):
        print(
            "\n! ABORTING PUBLISH — conflict markers found in index.html.\n"
            "  Refusing to commit or push a poisoned file to the live dashboard.\n"
            "  Re-run the refresh to regenerate a clean index.html from template.html.",
            file=sys.stderr,
        )
        return

    # Safety net #1 — clean up any orphan locks from a previous crashed run.
    _cleanup_stale_git_locks(git_dir)

    msg = f"Auto-refresh {datetime.datetime.now():%Y-%m-%d %H:%M ET}"
    git_succeeded = False  # True after a successful `git push`
    git_no_op = False      # True if git determined there are no changes

    # Safety net #0 — fast-forward local main onto origin's tip WITHOUT merging.
    #
    # index.html is a fully regenerated artifact: every run rewrites it from
    # template.html, so it must NEVER be three-way merged. The old code ran
    # `git pull --rebase --autostash` here, which stashed our freshly rendered
    # index.html, rebased local onto origin, then popped the stash. Because BOTH
    # sides had rewritten the same single file, that pop produced a merge
    # conflict and wrote literal <<<<<<< / ======= / >>>>>>> markers straight
    # into index.html — which were then committed and published to the live TV
    # (the 2026-05 outage). We now resync with `fetch` + `reset --mixed`, which:
    #   • moves local main to exactly origin/main, so the next push is a clean
    #     fast-forward (this is what fixes the non-fast-forward rejection that
    #     happens after the API fallback in safety net #2 publishes to origin),
    #   • leaves the freshly rendered index.html untouched in the working tree
    #     (--mixed never touches working-tree files), and
    #   • never merges and never unlinks files, so it also works inside the
    #     Cowork sandbox, where unlink() in the repo is forbidden (which is why
    #     the old `git reset --hard` self-heal failed there).
    # Failures here are non-fatal — the API fallback overwrites origin directly.
    try:
        subprocess.run(
            ["git", "-C", repo_dir, "fetch", "origin", "main"],
            check=True, capture_output=True, text=True, timeout=60,
        )
        subprocess.run(
            ["git", "-C", repo_dir, "reset", "--mixed", "origin/main"],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        err = (e.stderr or str(e)).strip()
        print(f"\n! git fetch/reset sync failed (continuing anyway): {err}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("\n! git fetch/reset sync timed out (continuing anyway)", file=sys.stderr)

    try:
        # Add the freshly rendered HTML
        subprocess.run(["git", "-C", repo_dir, "add", "index.html"], check=True, capture_output=True, text=True)

        # Check if there's actually a change to commit
        status = subprocess.run(
            ["git", "-C", repo_dir, "status", "--porcelain", "index.html"],
            capture_output=True, text=True, check=True,
        )
        if not status.stdout.strip():
            print("\n(No change to index.html — nothing to push.)")
            git_no_op = True
        else:
            subprocess.run(
                ["git", "-C", repo_dir, "commit", "-m", msg],
                check=True, capture_output=True, text=True,
            )

            # If GITHUB_TOKEN is set in the env (e.g. via .env when running from
            # the Cowork sandbox), inject it into the push URL just for this push.
            # The token is NEVER written to .git/config — it lives only in this
            # subprocess invocation. On Mat's Mac the env var is normally unset,
            # so the existing remote (with macOS keychain auth) is used.
            push_target = ["origin", "main"]
            token = os.environ.get("GITHUB_TOKEN", "").strip()
            if token:
                # Resolve the current origin URL and rewrite it with the token.
                remote = subprocess.run(
                    ["git", "-C", repo_dir, "remote", "get-url", "origin"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
                if remote.startswith("https://github.com/"):
                    authed_url = remote.replace(
                        "https://github.com/",
                        f"https://x-access-token:{token}@github.com/",
                        1,
                    )
                    push_target = [authed_url, "main"]

            push = subprocess.run(
                ["git", "-C", repo_dir, "push", *push_target],
                capture_output=True, text=True, timeout=60,
            )
            if push.returncode != 0:
                # Scrub the token out of any error message before printing.
                err = push.stderr.strip()
                if token:
                    err = err.replace(token, "***GITHUB_TOKEN***")
                print(f"\n! git push failed:\n{err}", file=sys.stderr)
            else:
                print(f"\n✓ Pushed to GitHub  ({msg})")
                git_succeeded = True
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() if e.stderr else str(e)
        print(f"\n! git step failed: {err}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("\n! git push timed out", file=sys.stderr)

    # Safety net #2 — git failed somewhere along the way. Publish via the
    # GitHub API instead so the live dashboard still gets the fresh HTML.
    # (Skipped if git already succeeded, or if git confirmed nothing changed.)
    if not git_succeeded and not git_no_op:
        if not _push_via_github_api(repo_dir, msg):
            print("  ! All push paths failed — dashboard not published.", file=sys.stderr)


if __name__ == "__main__":
    main()
