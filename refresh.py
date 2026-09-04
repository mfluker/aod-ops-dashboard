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
    "/Users/matfluker/Cowork/AoD/projects/AoD_Cowork",
    _AOD_COWORK_ROOT,
    (_sandbox_glob("canvas_data.py") or "").rsplit("/", 1)[0],
]
INSTALL_VS_DEPOSIT_CANDIDATES = [
    "/Users/matfluker/Cowork/AoD/projects/AoD_Cowork/" + _install_rel,
    os.path.join(_AOD_COWORK_ROOT, _install_rel),
    _sandbox_glob(_install_rel) or "",
]
REFACING_SALES_CANDIDATES = [
    "/Users/matfluker/Cowork/AoD/projects/AoD_Cowork/" + _refacing_rel,
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
# The Overdue Orders tab is NOT part of the published-CSV export (only
# Orders Tracker / gid=0 is published), so it is read through the gviz
# endpoint, which serves any tab of a link-shared sheet by name.
MFG_SHEET_ID = os.environ.get(
    "AOD_MFG_SHEET_ID", "10Riwpojj3pR_UVQlEtTsQE27eVygQyY6JmRO-ZK_u-c").strip()
MFG_SHEET_OVERDUE_CSV_URL = os.environ.get(
    "AOD_MFG_SHEET_OVERDUE_CSV_URL",
    f"https://docs.google.com/spreadsheets/d/{MFG_SHEET_ID}"
    "/gviz/tq?tqx=out:csv&sheet=Overdue%20Orders").strip()


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


def now_eastern_stamp():
    """Run time in US Eastern, labeled 'EST' per Mat's spec (2026-05-22).

    The previous stamp used a naive datetime.now() which, in the Cowork sandbox
    (UTC clock), rendered ~3-4 hours ahead of Eastern. We resolve the real
    Eastern wall-clock via zoneinfo and label it 'EST' as requested. (Note: in
    summer this is technically EDT, but the label is fixed to 'EST' by request.)
    """
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: assume the host clock is UTC (true in the Cowork sandbox)
        # and shift to Eastern (-4 for Mar–Nov DST, -5 otherwise).
        utc = datetime.datetime.utcnow()
        offset = 4 if 3 <= utc.month <= 11 else 5
        now = utc - datetime.timedelta(hours=offset)
    return now.strftime("%a %b %-d · %-I:%M %p EST")


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


def system_sales_windows(r30_current, r30_prior):
    """
    Compute System Sales for BOTH R30 windows (current + prior) in a SINGLE
    Canvas query using conditional SUM. Gives the rolling headline and the
    vs-prior comparison from one pull.

    Definition: SUM(order_total) for NEW jobs (job_type_id=1), ILM-excluded,
    anchored on the JOB's own date (j.date_added) — i.e. when the job was
    sold/created.

    NOTE: we deliberately do NOT anchor on first-deposit date. That older
    approach aggregated the giant customer_payment table (MIN(date_added) GROUP
    BY job_id), which times out / errors on the Canvas replica — that is why the
    revenue card had been rendering $0. j.date_added keeps it to the fast job
    table. Returns (r30_cur, r30_prior) floats, or (None, None).

    2026-08-14: the third (H1-YTD) column was removed with bonus-pace tracking.
    Dropping it also shrinks the scanned range from Jan 1 to just the two R30
    windows, so this query got materially cheaper.
    """
    def _excl(d):
        return _fmt_dt(d + datetime.timedelta(days=1))   # inclusive end -> exclusive
    r30c_s, r30c_e = r30_current
    r30p_s, r30p_e = r30_prior
    pull_start = min(r30c_s, r30p_s)
    pull_end   = max(r30c_e, r30p_e)
    sql = f"""
    SELECT
      SUM(CASE WHEN j.date_added >= '{_fmt_dt(r30c_s)}' AND j.date_added < '{_excl(r30c_e)}' THEN j.order_total ELSE 0 END) AS r30_cur,
      SUM(CASE WHEN j.date_added >= '{_fmt_dt(r30p_s)}' AND j.date_added < '{_excl(r30p_e)}' THEN j.order_total ELSE 0 END) AS r30_prior
    FROM job j
    INNER JOIN franchisee f ON f.id = j.franchisee_id
    WHERE 1=1
      {JOB_FILTER}
      AND j.job_type_id = 1   -- New orders only
      {FRANCHISEE_FILTER}
      AND j.date_added >= '{_fmt_dt(pull_start)}'
      AND j.date_added <  '{_excl(pull_end)}'
    """
    result = run_query(sql, output_format="json", max_rows=10)
    if result.get("error"):
        print(f"  ! system_sales_windows query error: {result['error']}", file=sys.stderr)
        return None, None
    rows = result.get("rows") or []
    if not rows:
        return 0.0, 0.0
    r = rows[0]
    return _to_float(r.get("r30_cur")), _to_float(r.get("r30_prior"))


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
    mac = "/Users/matfluker/Cowork/AoD/projects/AoD_Cowork/" + name
    sandbox = os.path.join(_AOD_COWORK_ROOT, name)
    if os.path.exists(mac):
        return mac
    if os.path.exists(sandbox):
        return sandbox
    # Prefer the path that matches the running environment.
    return mac if os.path.isdir("/Users/matfluker/Cowork/AoD/projects/AoD_Cowork") else sandbox


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


# Path to the SAME detail SQL the install-vs-deposit skill uses. The dashboard
# wraps it and aggregates IN-DATABASE (below) so each window pulls ONE summary
# row instead of ~180 detail rows through the live session. The standalone skill
# is untouched — it still reads this .sql to produce its detail CSV.
INSTALL_VS_DEPOSIT_SQL = os.path.join(
    os.path.dirname(INSTALL_VS_DEPOSIT_SCRIPT), "references", "install_vs_deposit.sql"
)

# Network Lead Times now anchor on the MEASUREMENT APPOINTMENT date (not deposit).
# This detail SQL lives WITH the dashboard (ops-dashboard/sql/) — it is dashboard-
# only and deliberately separate from the deposit-based install-vs-deposit skill,
# which other tools still depend on. See sql/measure_to_install.sql.
MEASURE_TO_INSTALL_SQL = os.path.join(HERE, "sql", "measure_to_install.sql")


def _load_ivd_detail_sql(start_date, end_date_inclusive):
    """Read the shared install-vs-deposit detail SQL and substitute the window."""
    end_exclusive = end_date_inclusive + datetime.timedelta(days=1)
    with open(INSTALL_VS_DEPOSIT_SQL) as fh:
        tmpl = fh.read()
    return (
        tmpl
        .replace("{start}", _fmt_dt(start_date))
        .replace("{end_exclusive}", _fmt_dt(end_exclusive))
    )


def install_vs_deposit_aggregated(start_date, end_date_inclusive):
    """Sold-to-Install median (days) + % under 10 weeks, computed ENTIRELY IN SQL.

    Wraps the SAME per-chain detail query the install-vs-deposit skill uses (one
    row per install chain, latest install wins) in a CTE, then aggregates so the
    query returns a SINGLE summary row instead of ~180 detail rows. The math is
    a faithful port of the old Python path (run_install_vs_deposit):

      * filtered set = chains with a real deposit date AND a non-negative
        deposit→install gap (mirrors: skip blank, skip negative);
      * median = average of the one/two middle values (identical to the old
        statistics-style median for both odd and even counts);
      * % under 10 weeks = share of the filtered set with gap < 70 days
        (the skill's Under_10_Weeks rule), as a percentage.

    Returns (median_days, pct_under_10_weeks, n) — same shape the subprocess
    path returned. (None, None, 0) on error or empty window.
    """
    detail = _load_ivd_detail_sql(start_date, end_date_inclusive)
    sql = f"""
    WITH ivd_detail AS (
    {detail}
    ),
    filtered AS (
      SELECT
        Days_Deposit_To_Install AS d,
        CASE WHEN Days_Deposit_To_Install < 70 THEN 1 ELSE 0 END AS under10
      FROM ivd_detail
      WHERE Days_Deposit_To_Install IS NOT NULL
        AND Days_Deposit_To_Install >= 0
    ),
    ivd_ranked AS (
      -- NOTE: the wrapped detail query already uses a derived-table alias named
      -- `ranked` internally; this CTE is deliberately named `ivd_ranked` to
      -- avoid a "not unique table/alias" collision on the Canvas MySQL replica.
      SELECT d,
             ROW_NUMBER() OVER (ORDER BY d) AS rn,
             COUNT(*)     OVER ()           AS cnt
      FROM filtered
    )
    SELECT
      (SELECT AVG(d) FROM ivd_ranked
         WHERE rn IN (FLOOR((cnt + 1) / 2), FLOOR((cnt + 2) / 2))) AS median_days,
      (SELECT AVG(under10) * 100 FROM filtered)                    AS pct_under_10wk,
      (SELECT COUNT(*) FROM filtered)                              AS n
    """
    result = run_query(sql, output_format="json", max_rows=10)
    if result.get("error"):
        print(f"  ! install_vs_deposit_aggregated error: {result['error']}", file=sys.stderr)
        return None, None, 0
    rows = result.get("rows") or []
    if not rows:
        return None, None, 0
    r = rows[0]
    n = _to_int(r.get("n"))
    if n == 0:
        # Empty window (or offline/emit pass) — no usable chains.
        return None, None, 0
    return _to_float(r.get("median_days")), _to_float(r.get("pct_under_10wk")), n


def _load_mti_detail_sql(start_date, end_date_inclusive):
    """Read the dashboard's measure-to-install detail SQL and substitute the window."""
    end_exclusive = end_date_inclusive + datetime.timedelta(days=1)
    with open(MEASURE_TO_INSTALL_SQL) as fh:
        tmpl = fh.read()
    return (
        tmpl
        .replace("{start}", _fmt_dt(start_date))
        .replace("{end_exclusive}", _fmt_dt(end_exclusive))
    )


def measure_to_install_aggregated(start_date, end_date_inclusive):
    """Measure-to-Install median (days) + % under 10 weeks, computed ENTIRELY IN SQL.

    Identical aggregation shape to install_vs_deposit_aggregated, but the wrapped
    detail query (sql/measure_to_install.sql) anchors on the first Measurement
    Appt (appointment_type_id=5) instead of the deposit date. One summary row per
    window. The filtered set excludes chains with no measurement appointment (NULL
    days) and any negative gap (install before measure — a data-quality artifact).

    Returns (median_days, pct_under_10_weeks, n) — (None, None, 0) on error/empty.
    """
    detail = _load_mti_detail_sql(start_date, end_date_inclusive)
    sql = f"""
    WITH mti_detail AS (
    {detail}
    ),
    filtered AS (
      SELECT
        Days_Measure_To_Install AS d,
        CASE WHEN Days_Measure_To_Install < 70 THEN 1 ELSE 0 END AS under10
      FROM mti_detail
      WHERE Days_Measure_To_Install IS NOT NULL
        AND Days_Measure_To_Install >= 0
    ),
    mti_ranked AS (
      SELECT d,
             ROW_NUMBER() OVER (ORDER BY d) AS rn,
             COUNT(*)     OVER ()           AS cnt
      FROM filtered
    )
    SELECT
      (SELECT AVG(d) FROM mti_ranked
         WHERE rn IN (FLOOR((cnt + 1) / 2), FLOOR((cnt + 2) / 2))) AS median_days,
      (SELECT AVG(under10) * 100 FROM filtered)                    AS pct_under_10wk,
      (SELECT COUNT(*) FROM filtered)                              AS n
    """
    result = run_query(sql, output_format="json", max_rows=10)
    if result.get("error"):
        print(f"  ! measure_to_install_aggregated error: {result['error']}", file=sys.stderr)
        return None, None, 0
    rows = result.get("rows") or []
    if not rows:
        return None, None, 0
    r = rows[0]
    n = _to_int(r.get("n"))
    if n == 0:
        return None, None, 0
    return _to_float(r.get("median_days")), _to_float(r.get("pct_under_10wk")), n


# -----------------------------------------------------------------------------
# 5b. ORDER -> SHIP TAT — status 5 (Submitted to Mfg Partner) -> status 6 (Shipped)
# -----------------------------------------------------------------------------

def order_to_ship_tat(start_date, end_date_inclusive):
    """Median Order -> Ship turnaround (days) for orders SHIPPED in the window.

    Start  = first time the job entered status 5 "Submitted to Manufacturing
             Partner" (job_status_update).
    End    = first time it entered status 6 "Order Shipped" (within the window).
    TAT    = whole days between the two (DATEDIFF), nearest day.

    Orders shipped in [start, end_inclusive] are the population. Jobs whose ship
    predates their submit (negative gap — a data artifact) are dropped. Median is
    computed in-SQL so one summary row comes back per window.

    NOTE on split orders: status 6 is stamped on the FIRST shipment, so for a
    split order this measures time-to-first-ship. Canvas has a "Partially
    Shipped" status (32) but it is unused (0 rows in 2026), so a true
    last-shipment date would require linking WWEX shipments back to Canvas jobs
    — tracked as a separate follow-up. Returns (median_days, n).
    """
    end_exclusive = end_date_inclusive + datetime.timedelta(days=1)
    sql = f"""
    WITH sub AS (
      SELECT job_id, MIN(date_added) AS d
      FROM job_status_update
      WHERE job_status_id = 5 AND active = 'y'
      GROUP BY job_id
    ),
    ship AS (
      SELECT job_id, MIN(date_added) AS d
      FROM job_status_update
      WHERE job_status_id = 6 AND active = 'y'
        AND date_added >= '{_fmt_dt(start_date)}'
        AND date_added <  '{_fmt_dt(end_exclusive)}'
      GROUP BY job_id
    ),
    tat AS (
      SELECT DATEDIFF(ship.d, sub.d) AS d
      FROM ship
      INNER JOIN sub        ON sub.job_id = ship.job_id
      INNER JOIN job j      ON j.id = ship.job_id
      INNER JOIN franchisee f ON f.id = j.franchisee_id
      WHERE 1=1
        {JOB_FILTER}
        {FRANCHISEE_FILTER}
        AND DATEDIFF(ship.d, sub.d) >= 0
    ),
    tat_ranked AS (
      SELECT d,
             ROW_NUMBER() OVER (ORDER BY d) AS rn,
             COUNT(*)     OVER ()           AS cnt
      FROM tat
    )
    SELECT
      (SELECT AVG(d) FROM tat_ranked
         WHERE rn IN (FLOOR((cnt + 1) / 2), FLOOR((cnt + 2) / 2))) AS median_days,
      (SELECT COUNT(*) FROM tat) AS n
    """
    result = run_query(sql, output_format="json", max_rows=10)
    if result.get("error"):
        print(f"  ! order_to_ship_tat error: {result['error']}", file=sys.stderr)
        return None, 0
    rows = result.get("rows") or []
    if not rows:
        return None, 0
    r = rows[0]
    n = _to_int(r.get("n"))
    if n == 0:
        return None, 0
    return _to_float(r.get("median_days")), n


# -----------------------------------------------------------------------------
# 5c. CLAIM / REORDER RATE — % of jobs installed in window with a claim/reorder
# -----------------------------------------------------------------------------

def claim_reorder_rate(start_date, end_date_inclusive):
    """Share of jobs INSTALLED in the window that have >=1 active claim or reorder,
    split into "had a claim" vs "had ONLY a reorder" (Mat 2026-08-06).

    Denominator = jobs with an Install Appt (appointment_type_id=7) in the window
                  (same install event the lead-time cards use), standard filters.
    Numerator   = those jobs that are the PARENT (claim.job_id / reorder.job_id)
                  of at least one active claim or reorder.
    Split rule  = claim wins: a job with BOTH a claim and a reorder counts under
                  Claim, so claim_pct + reorder_only_pct == total_pct exactly.

    Returns (pct_total, pct_claim, pct_reorder_only, installed_count, with_cr_count)
    — (None, None, None, 0, 0) on error/empty.
    Note: claims/reorders lag install, so very recent windows read slightly low;
    the R30-vs-prior comparison is still directional.
    """
    end_exclusive = end_date_inclusive + datetime.timedelta(days=1)
    sql = f"""
    SELECT COUNT(*) AS installed,
           COALESCE(SUM(has_claim), 0) AS with_claim,
           COALESCE(SUM(CASE WHEN has_claim = 0 AND has_reorder = 1 THEN 1 ELSE 0 END), 0) AS reorder_only
    FROM (
      SELECT j.id,
        CASE WHEN EXISTS (SELECT 1 FROM claim c   WHERE c.job_id = j.id AND c.active = 'y')
             THEN 1 ELSE 0 END AS has_claim,
        CASE WHEN EXISTS (SELECT 1 FROM reorder r WHERE r.job_id = j.id AND r.active = 'y')
             THEN 1 ELSE 0 END AS has_reorder
      FROM (
        SELECT a.job_id, MAX(a.date_and_time_starts) AS install_date
        FROM appointment a
        WHERE a.appointment_type_id = 7
          AND a.cancelled = 'n'
          AND a.active = 'y'
          AND a.job_id IS NOT NULL
          AND a.date_and_time_starts >= '{_fmt_dt(start_date)}'
          AND a.date_and_time_starts <  '{_fmt_dt(end_exclusive)}'
        GROUP BY a.job_id
      ) ap
      INNER JOIN job j        ON j.id = ap.job_id
      INNER JOIN franchisee f ON f.id = j.franchisee_id
      WHERE 1=1
        {JOB_FILTER}
        {FRANCHISEE_FILTER}
    ) t
    """
    result = run_query(sql, output_format="json", max_rows=10)
    if result.get("error"):
        print(f"  ! claim_reorder_rate error: {result['error']}", file=sys.stderr)
        return None, None, None, 0, 0
    rows = result.get("rows") or []
    if not rows:
        return None, None, None, 0, 0
    installed = _to_int(rows[0].get("installed"))
    with_claim = _to_int(rows[0].get("with_claim"))
    reorder_only = _to_int(rows[0].get("reorder_only"))
    if installed == 0:
        return None, None, None, 0, 0
    with_cr = with_claim + reorder_only
    return (
        with_cr / installed * 100.0,
        with_claim / installed * 100.0,
        reorder_only / installed * 100.0,
        installed,
        with_cr,
    )


# -----------------------------------------------------------------------------
# 6. REFACING REVENUE — calls the refacing-sales skill
# -----------------------------------------------------------------------------

def run_refacing_summary(start_date, end_date_inclusive):
    """
    Run refacing_sales.py for the window and return (revenue, job_count).

    Mat 2026-06-11 decoupled these two figures:
      * revenue   = ALL refacing-product revenue across EVERY refacing job
                    (the skill is run with --min-fronts 0 so no job is dropped).
      * job_count = the stricter count of jobs with >= 5 RF fronts
                    (SUMMARY_JOBS_5PLUS from the skill).

    Both come from a single skill run, parsed from its machine-readable SUMMARY_*
    lines (robust to where the CSV lands). Returns (None, None) on failure.
    """
    cmd = ["python3", REFACING_SALES_SCRIPT, str(start_date), str(end_date_inclusive),
           "--min-fronts", "0"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        print(f"  ! refacing_sales failed: {proc.stderr[:500]}", file=sys.stderr)
        return None, None

    # Primary path: the explicit SUMMARY_* lines the skill prints.
    total_rev = None
    jobs_5plus = None
    m = re.search(r"SUMMARY_REVENUE_ALL:\s*([\d.]+)", proc.stdout)
    if m:
        total_rev = float(m.group(1))
    m = re.search(r"SUMMARY_JOBS_5PLUS:\s*(\d+)", proc.stdout)
    if m:
        jobs_5plus = int(m.group(1))
    if total_rev is not None and jobs_5plus is not None:
        return total_rev, jobs_5plus

    # Fallback: derive from the CSV columns (revenue summed over all rows, jobs
    # counted where doors + drawers >= 5).
    csv_path = _refacing_csv_path(start_date, end_date_inclusive)
    if not os.path.exists(csv_path):
        # Last-ditch: legacy stdout lines.
        if total_rev is None:
            m = re.search(r"Total revenue:\s*\$?([\d,]+\.\d{2})", proc.stdout)
            if m:
                total_rev = float(m.group(1).replace(",", ""))
        return total_rev, jobs_5plus

    total_rev = 0.0
    jobs_5plus = 0
    with open(csv_path) as fh:
        for row in csv.DictReader(fh):
            jid = (row.get("job_id") or "").strip().upper()
            if jid == "TOTAL":
                continue
            try:
                total_rev += float(row.get("revenue") or 0)
            except (ValueError, TypeError):
                pass
            try:
                fronts = int(float(row.get("num_doors") or 0)) + int(float(row.get("num_drawers") or 0))
            except (ValueError, TypeError):
                fronts = 0
            if fronts >= 5:
                jobs_5plus += 1
    return total_rev, jobs_5plus


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


_mfg_csv_cache = None

def _fetch_mfg_csv():
    """
    Fetch the published-CSV version of the Mfg Partner Analysis sheet ONCE per
    refresh and cache the raw text (the sheet is hit ~6× per run otherwise).
    Returns the CSV text, or None when the URL isn't configured / fetch fails.
    """
    global _mfg_csv_cache
    if _mfg_csv_cache is not None:
        return _mfg_csv_cache
    if not MFG_SHEET_CSV_URL:
        print("  ! AOD_MFG_SHEET_CSV_URL not set — skipping Mfg sheet metric", file=sys.stderr)
        return None
    try:
        req = Request(MFG_SHEET_CSV_URL, headers={"User-Agent": "AoD-Dashboard/1.0"})
        resp = urlopen(req, timeout=30)
        _mfg_csv_cache = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ! Mfg sheet fetch error: {e}", file=sys.stderr)
        return None
    return _mfg_csv_cache


def fetch_mfg_claim_counts(start_date, end_date_inclusive):
    """
    Filter Mfg Partner Analysis rows whose inferred Order Date falls in the
    window and return (claim_line_items, total_line_items). Returns (None, None)
    if the sheet isn't available.

    A row's contribution to the totals is its 'Line Items Count' value. Blank/zero
    counts contribute zero — that's the "not enough data" case the indicator handles.
    """
    content = _fetch_mfg_csv()
    if content is None:
        return None, None

    today = datetime.date.today()
    claim_items = 0
    total_items = 0
    # The sheet emits ONE ROW PER CLAIM TYPE and repeats the FULL Line Items
    # Count on each, so a claim logged with two reasons would be counted twice
    # (Mat 2026-08-10). Collapse Claim rows that differ only by Claim Type.
    # NOTE: only Claim rows are deduped. "Job" rows legitimately repeat per
    # manufacturer with their own line items, so those must all be summed.
    seen_claim_rows = set()

    for row in csv.DictReader(io.StringIO(content)):
        order_type = (row.get("Type") or "").strip()
        if order_type not in ("Claim", "Reorder", "Job"):
            continue

        dt = _parse_mfg_date(row.get("Order Date"), today)
        if dt is None:
            continue
        if not (start_date <= dt <= end_date_inclusive):
            continue

        if order_type == "Claim":
            claim_key = (
                (row.get("Job #") or "").strip().upper(),
                (row.get("Manufacturer") or "").strip(),
                (row.get("Order #") or "").strip(),
                (row.get("Line Items Count") or "").strip(),
                (row.get("Order Date") or "").strip(),
            )
            if claim_key[0]:
                if claim_key in seen_claim_rows:
                    continue
                seen_claim_rows.add(claim_key)

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


def _mfg_num(ref):
    """Numeric id inside a Job#/Parent cell, e.g. 'C3841'->'3841', '3662'->'3662'."""
    m = re.search(r"(\d+)", ref or "")
    return m.group(1) if m else None


def _is_cr_ref(ref):
    """True when a Parent Job # points at a claim OR a reorder ('C'/'R' prefix)."""
    ref = (ref or "").strip().upper()
    return ref[:1] in ("C", "R") and any(ch.isdigit() for ch in ref)


def fetch_nested_claim_rows():
    """
    Fetch the Mfg Partner Analysis sheet and return EVERY claim whose parent
    order is itself a Claim OR a Reorder (Mat 2026-08-06 — the "Claims +"
    card is now a table, and claims-on-reorders count too).

    Each row: {"job", "parent", "mfg", "loc", "claim_no", "date", "prod"}
    where claim_no is the claim's position in the remake chain — a claim on a
    claim (or on a reorder) is #2, a claim on a claim on a claim is #3, etc.
    The chain is walked through BOTH claims and reorders by numeric id.
    prod=True means still in production (Complete != TRUE); the card shows
    only those, while the click-to-expand overlay lists all of them.

    Returns a list sorted newest-first, or None when the sheet isn't available.
    """
    content = _fetch_mfg_csv()
    if content is None:
        return None

    today = datetime.date.today()
    orders = {}   # numeric id -> parent ref, for every Claim/Reorder row
    claims = []   # Claim rows only
    # The Mfg Partner Analysis sheet emits ONE ROW PER CLAIM TYPE, so a claim
    # logged with two reasons (e.g. C7165 = Poor Quality + Missing Item) shows up
    # twice with otherwise identical fields. The Claims + table is a list of
    # claims, not of reasons, so collapse those here (Mat 2026-08-10).
    seen_claims = set()
    for row in csv.DictReader(io.StringIO(content)):
        order_type = (row.get("Type") or "").strip()
        if order_type not in ("Claim", "Reorder"):
            continue
        num = _mfg_num(row.get("Job #"))
        parent = (row.get("Parent Job #") or "").strip()
        if num and num not in orders:
            orders[num] = parent
        if order_type == "Claim":
            job_key = (row.get("Job #") or "").strip().upper()
            if job_key:
                if job_key in seen_claims:
                    continue
                seen_claims.add(job_key)
            claims.append({
                "job":    (row.get("Job #") or "").strip(),
                "parent": parent,
                "num":    num,
                "mfg":    (row.get("Manufacturer") or "").strip(),
                "loc":    (row.get("Franchisee") or "").strip(),
                "prod":   (row.get("Complete") or "").strip().upper() != "TRUE",
                "date":   _parse_mfg_date(row.get("Order Date"), today),
            })

    def chain_no(start_num, start_parent):
        """1 + number of C/R links above this order (cycle-safe)."""
        n, seen, parent = 1, {start_num}, start_parent
        while _is_cr_ref(parent):
            n += 1
            pnum = _mfg_num(parent)
            if not pnum or pnum in seen or pnum not in orders:
                break
            seen.add(pnum)
            parent = orders[pnum]
        return n

    rows = [
        {
            "job": c["job"], "parent": c["parent"], "mfg": c["mfg"],
            "loc": c["loc"], "date": c["date"], "prod": c["prod"],
            "claim_no": chain_no(c["num"], c["parent"]),
        }
        for c in claims
        if _is_cr_ref(c["parent"])
    ]
    rows.sort(key=lambda r: (r["date"] is None, r["date"]), reverse=False)
    rows.reverse()  # newest first, unknown dates last
    return rows


def top_claim_reasons(days=30, top_n=3):
    """
    Top claim reasons over the trailing `days`, weighted by LINE ITEMS (the
    same unit as the Claim Line Items % card). Reads the Claim Type column of
    the Mfg Partner Analysis sheet for Claim rows in the window.

    Returns a list of {"reason", "items", "share_pct"} sorted by items desc
    (ties broken by claim count), or None when the sheet isn't available.
    """
    content = _fetch_mfg_csv()
    if content is None:
        return None

    today = datetime.date.today()
    start = today - datetime.timedelta(days=days - 1)
    items = {}
    counts = {}
    for row in csv.DictReader(io.StringIO(content)):
        if (row.get("Type") or "").strip() != "Claim":
            continue
        dt = _parse_mfg_date(row.get("Order Date"), today)
        if dt is None or not (start <= dt <= today):
            continue
        reason = (row.get("Claim Type") or "").strip() or "Unspecified"
        raw_li = (row.get("Line Items Count") or "").strip()
        try:
            li = int(float(raw_li)) if raw_li else 0
        except ValueError:
            li = 0
        items[reason] = items.get(reason, 0) + li
        counts[reason] = counts.get(reason, 0) + 1

    total = sum(items.values())
    ranked = sorted(items, key=lambda r: (-items[r], -counts.get(r, 0), r))
    return [
        {
            "reason": r,
            "items": items[r],
            "share_pct": (items[r] / total * 100.0) if total else 0.0,
        }
        for r in ranked[:top_n]
    ]


COC_CARD_MAX_ROWS = 3  # card shows up to 3 in-production rows; 4+ enables click-to-expand

def coc_table_rows_html(rows, max_rows=COC_CARD_MAX_ROWS):
    """
    Render the Claims + card table body — IN-PRODUCTION rows only. `rows` comes
    from fetch_nested_claim_rows(). Degrades to a single muted row when the
    sheet is unavailable or the list is empty. Shows up to `max_rows` rows;
    with 4+ in production it shows the first 3 plus a "+N more — click for
    all" row, and the card becomes clickable (see coc_click_attrs).
    """
    if rows is None:
        return '<tr><td colspan="4" class="coc-empty">—</td></tr>'
    prod = [r for r in rows if r["prod"]]
    if not prod:
        return '<tr><td colspan="4" class="coc-empty">None in production</td></tr>'
    shown = prod[:max_rows]
    out = []
    for r in shown:
        badge_cls = "coc-no hot" if r["claim_no"] >= 3 else "coc-no"
        out.append(
            f'<tr><td class="coc-job">{r["job"]}</td>'
            f'<td>{r["mfg"]}</td><td>{r["loc"]}</td>'
            f'<td><span class="{badge_cls}">{r["claim_no"]}</span></td></tr>'
        )
    # NOTE: when there are more than max_rows in production, the "+N more —
    # click for all" hint renders in the card label (coc_more_note), not as a
    # table row, so the card never grows past 3 rows.
    return "".join(out)


def coc_more_note(rows, max_rows=COC_CARD_MAX_ROWS):
    """Header hint shown only when the card is clickable (4+ in production)."""
    if not coc_is_clickable(rows, max_rows):
        return ""
    n_hidden = sum(1 for r in rows if r["prod"]) - max_rows
    return f'<span class="coc-more">+{n_hidden} more · click for all</span>'


def coc_is_clickable(rows, max_rows=COC_CARD_MAX_ROWS):
    """Card opens the overlay only when there are 4+ in-production rows."""
    if not rows:
        return False
    return sum(1 for r in rows if r["prod"]) > max_rows


def coc_click_attrs(rows):
    """onclick/title attributes for the Claims + card — empty when not clickable."""
    if not coc_is_clickable(rows):
        return ""
    return ('onclick="document.getElementById(\'coc-overlay\').classList.add(\'open\')" '
            'title="Click to see all in-production claims"')


def coc_modal_rows_html(rows):
    """
    Render the click-to-expand overlay table body — every IN-PRODUCTION claim
    on a claim/reorder, newest first. (Only reachable when the card shows
    "+N more", i.e. 4+ in production.)
    """
    prod = [r for r in (rows or []) if r["prod"]]
    if not prod:
        return '<tr><td colspan="6" class="coc-empty">None in production</td></tr>'
    out = []
    for r in prod:
        badge_cls = "coc-no hot" if r["claim_no"] >= 3 else "coc-no"
        date_txt = r["date"].strftime("%m/%d/%y") if r["date"] else "—"
        out.append(
            f'<tr><td class="coc-job">{r["job"]}</td>'
            f'<td>{r["parent"]}</td>'
            f'<td>{r["mfg"]}</td><td>{r["loc"]}</td>'
            f'<td><span class="{badge_cls}">{r["claim_no"]}</span></td>'
            f'<td>{date_txt}</td></tr>'
        )
    return "".join(out)


# -----------------------------------------------------------------------------
# 7b-2. OVERDUE ORDERS — Mfg Partner sheet, "Overdue Orders" tab
# -----------------------------------------------------------------------------
#
# The Overdue Orders tab is a pivot: one six-column block per manufacturer
# (CCF, EAG, JB, GHWD, NASL, Dackor, RM), each block carrying Job #, Order #,
# Parent Job #, Type, Ship Est Date, Days Over. The manufacturer therefore comes
# from the BLOCK, not from a column. Deliberately narrower than a raw
# "Days over Lead Time > 0" filter on the Orders Tracker tab — that sweep also
# picks up Richelieu and VS, which this tab excludes.
#
# The tab carries no location code, so it is joined to the Orders Tracker tab on
# the numeric job number (Mat 2026-09-04).

_overdue_csv_cache = None

def _fetch_overdue_csv():
    """Fetch the Overdue Orders tab CSV once per refresh. None on failure."""
    global _overdue_csv_cache
    if _overdue_csv_cache is not None:
        return _overdue_csv_cache
    if not MFG_SHEET_OVERDUE_CSV_URL:
        print("  ! Overdue Orders CSV URL not set — skipping card", file=sys.stderr)
        return None
    try:
        req = Request(MFG_SHEET_OVERDUE_CSV_URL, headers={"User-Agent": "AoD-Dashboard/1.0"})
        _overdue_csv_cache = urlopen(req, timeout=30).read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ! Overdue Orders fetch error: {e}", file=sys.stderr)
        return None
    return _overdue_csv_cache


def _tracker_locations():
    """Numeric job # -> franchisee code, from the Orders Tracker tab."""
    content = _fetch_mfg_csv()
    if content is None:
        return {}
    locs = {}
    for row in csv.DictReader(io.StringIO(content)):
        num = _mfg_num(row.get("Job #"))
        loc = (row.get("Franchisee") or "").strip()
        if num and loc and num not in locs:
            locs[num] = loc
    return locs


# gviz folds the merged manufacturer banner into the first header cell of each
# block, e.g. "Overdue Orders: CCF Job #". This pulls the code back out.
_OVERDUE_BLOCK_RE = re.compile(r"Overdue Orders:\s*(.+?)\s+Job\s*#", re.I)

def fetch_overdue_rows():
    """
    Every overdue order on the Overdue Orders tab, most overdue first.

    Each row: {"job", "type", "mfg", "loc", "est_ship", "days"}.
    Returns None when the tab can't be read.
    """
    content = _fetch_overdue_csv()
    if content is None:
        return None

    reader = list(csv.reader(io.StringIO(content)))
    if not reader:
        return []
    header = reader[0]

    # Walk the header left to right; each block starts at a "…Job #" cell and
    # runs for six columns (Job #, Order #, Parent Job #, Type, Ship Est, Days).
    blocks = []
    for i, cell in enumerate(header):
        m = _OVERDUE_BLOCK_RE.search(cell or "")
        if m:
            blocks.append((i, m.group(1).strip()))

    locs = _tracker_locations()
    rows = []
    for start, mfg in blocks:
        for r in reader[1:]:
            def cell(off):
                idx = start + off
                return (r[idx] if idx < len(r) else "").strip()
            job = cell(0)
            if not job:
                continue
            try:
                days = int(float(cell(5) or 0))
            except ValueError:
                days = 0
            # The tab drops the C/R prefix; put it back so the card reads the
            # same way the Claims + card does.
            otype = cell(3) or "Job"
            if otype == "Claim" and not job.upper().startswith("C"):
                job = "C" + job
            elif otype == "Reorder" and not job.upper().startswith("R"):
                job = "R" + job
            rows.append({
                "job":      job,
                "type":     otype,
                "mfg":      mfg,
                "loc":      locs.get(_mfg_num(job), "—"),
                "est_ship": cell(4),
                "days":     days,
            })
    rows.sort(key=lambda x: -x["days"])
    return rows


OVERDUE_CARD_MAX_ROWS = 3  # card shows 3; 4+ enables click-to-expand

def overdue_table_rows_html(rows, max_rows=OVERDUE_CARD_MAX_ROWS):
    """Overdue Orders card body — the `max_rows` most overdue."""
    if rows is None:
        return '<tr><td colspan="4" class="coc-empty">—</td></tr>'
    if not rows:
        return '<tr><td colspan="4" class="coc-empty">Nothing overdue</td></tr>'
    out = []
    for r in rows[:max_rows]:
        badge_cls = "coc-no hot" if r["days"] >= 7 else "coc-no"
        out.append(
            f'<tr><td class="coc-job">{r["job"]}</td>'
            f'<td>{r["mfg"]}</td><td>{r["loc"]}</td>'
            f'<td><span class="{badge_cls}">{r["days"]}</span></td></tr>'
        )
    return "".join(out)


def overdue_is_clickable(rows, max_rows=OVERDUE_CARD_MAX_ROWS):
    return bool(rows) and len(rows) > max_rows


def overdue_more_note(rows, max_rows=OVERDUE_CARD_MAX_ROWS):
    if not overdue_is_clickable(rows, max_rows):
        return ""
    return f'<span class="coc-more">+{len(rows) - max_rows} more · click for all</span>'


def overdue_click_attrs(rows):
    if not overdue_is_clickable(rows):
        return ""
    return ('onclick="document.getElementById(\'overdue-overlay\').classList.add(\'open\')" '
            'title="Click to see every overdue order"')


def overdue_modal_rows_html(rows):
    """Overlay body — every overdue order, most overdue first."""
    if not rows:
        return '<tr><td colspan="6" class="coc-empty">Nothing overdue</td></tr>'
    out = []
    for r in rows:
        badge_cls = "coc-no hot" if r["days"] >= 7 else "coc-no"
        out.append(
            f'<tr><td class="coc-job">{r["job"]}</td>'
            f'<td>{r["type"]}</td><td>{r["mfg"]}</td><td>{r["loc"]}</td>'
            f'<td>{r["est_ship"] or "—"}</td>'
            f'<td><span class="{badge_cls}">{r["days"]}</span></td></tr>'
        )
    return "".join(out)


def claim_reasons_html(reasons):
    """Render the top-claim-reasons mini list. `reasons` from top_claim_reasons()."""
    if not reasons:
        return '<div class="coc-reason"><span class="name">—</span></div>'
    out = []
    for i, r in enumerate(reasons, start=1):
        out.append(
            f'<div class="coc-reason"><span class="rank">{i}</span>'
            f'<span class="name">{r["reason"]}</span>'
            # TV build (2026-08-14): share % only. "N items · X%" was clipping
            # in the narrow right-hand slot at TV type sizes, and the share is
            # the number people actually read from across the room.
            f'<span class="share">{r["share_pct"]:.0f}%</span></div>'
        )
    return "".join(out)


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

# The surcharge-analysis copy of the parser is a superset of the cost-analysis
# one — same load_shipments() signature, but each record also carries
# airbill / pro / bol / invoice_no / job_ref / receiver city+state, which the
# Surcharges to Chase card needs. Same parser the Mon/Wed report uses.
_shipping_parser_rel = "skills/shipping-surcharge-analysis/scripts"
SHIPPING_PARSER_CANDIDATES = [
    "/Users/matfluker/Cowork/AoD/projects/AoD_Cowork/" + _shipping_parser_rel,
    os.path.join(_AOD_COWORK_ROOT, _shipping_parser_rel),
    _sandbox_glob(_shipping_parser_rel) or "",
]

_shipping_loader_cached = None


def _invoice_base_paths():
    """
    Roots to search for FreightBrokerInvoices/. The surcharge-analysis parser's
    own default list does not know about this checkout's mount point, so the
    dashboard passes the roots explicitly — starting with the AoD_Cowork folder
    this script actually lives in.
    """
    paths = [_AOD_COWORK_ROOT,
             "/Users/matfluker/Cowork/AoD/projects/AoD_Cowork",
             os.path.expanduser("~/Documents/Claude/Projects/AoD_Cowork")]
    paths += _glob.glob("/sessions/*/mnt/AoD_Cowork")
    paths += _glob.glob("/sessions/*/mnt/Cowork/AoD/projects/AoD_Cowork")
    seen, out = set(), []
    for p in paths:
        if p and p not in seen and os.path.isdir(p):
            seen.add(p)
            out.append(p)
    return out

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


# ---------------------------------------------------------------- surcharge policy
# Lifted verbatim from daily-ops-report/generate_report.py so the card and the
# Monday/Wednesday HTML report always agree. If Mat reclassifies a surcharge,
# change it in BOTH places.
#
# Memo pass-through to Franchise Partners. Never itemised, never counted here.
PASS_THROUGH_PATTERNS = [
    "LIFTGATE", "LIMITED ACCESS", "RESIDENTIAL DELIVERY", "RESIDENTIAL SIGNATURE",
    "RESIDENTIAL SURCHARGE", "DEMAND SURCHARGE - RESIDENTIAL", "INSIDE DELIVERY",
    "INSIDE PICKUP", "PICKUP OR DELIVERY APPOINTMENT", "APPOINTMENT",
    "ADDRESS CORRECTION", "HOLD SHIPMENT AT TERMINAL", "HOLD AT TERMINAL",
]
# Paid by the Home Office. Order matters, first match wins.
FLAG_GROUPS = [
    ("Additional Handling", ["ADDITIONAL HANDLING"]),
    ("Large Package", ["LARGE PACKAGE"]),
    ("Over Dimension", ["OVER DIMENSION"]),
    ("High Cost Delivery", ["DELIVERY AREA", "HIGH COST"]),
    ("Grocery consolidation (GCD)", ["GCD", "GROCERY"]),
    ("Weight verification / inspection", ["WEIGHT VERIFICATION", "INSPECTION"]),
    ("Third-party billing", ["THIRD PARTY", "3RD PARTY"]),
    ("On-call pickup", ["ON CALL PICKUP", "ON-CALL PICKUP"]),
]
INSURANCE_GROUP = "Insurance / declared value"
INSURANCE_PATTERNS = ["INSURANCE", "DECLARED VALUE"]
PACKAGING_GROUPS = ("Additional Handling", "Large Package", "Over Dimension")
# The invoice carries the CHARGE, not the insured value; coverage is estimated
# at the carrier's published rate per $100.
INSURANCE_RATE_PER_100 = 1.05
INSURANCE_VALUE_ALERT = 5000.0
# Itemise packaging surcharges at or above this per shipment.
PACKAGING_ITEMISE_MIN = 75.0

# Short labels for the TV card — the report's full group names do not fit.
CHASE_SHORT_LABEL = {
    "Additional Handling": "Handling",
    "Large Package": "Large pkg",
    "Over Dimension": "Over dim",
    INSURANCE_GROUP: "Insurance",
}


def _norm_surcharge(t):
    return re.sub(r"\s+", " ", (t or "").upper().strip())


def classify_surcharge(ctype):
    """-> ('fuel'|'pass_through'|'insurance'|'flag'|'review', group_label)"""
    t = _norm_surcharge(ctype)
    if t and "FUEL" in t:
        return "fuel", "Fuel"
    for label, pats in FLAG_GROUPS:
        if any(pp in t for pp in pats):
            return "flag", label
    if any(pp in t for pp in INSURANCE_PATTERNS):
        return "insurance", INSURANCE_GROUP
    if any(pp in t for pp in PASS_THROUGH_PATTERNS):
        return "pass_through", "Memo pass-through"
    return "review", "Unclassified"


def _tracking_ref(s):
    """Small package carries an airbill, LTL carries a PRO or BOL."""
    for k in ("airbill", "pro", "bol"):
        v = (s.get(k) or "").strip()
        if v and v.upper() not in ("NA", "N/A", "0"):
            return v
    return "n/a"


def _city_state(s):
    c = (s.get("receiver_city") or "").strip().title()
    st = (s.get("receiver_state") or "").strip().upper()
    return ", ".join([x for x in (c, st) if x]) or "n/a"


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
        shipments = load(start_date=str(start), end_date=str(today),
                         base_paths=_invoice_base_paths())
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
            base_paths=_invoice_base_paths(),
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
    # Home Office lines: everything that is neither fuel nor memo-passed through
    # to the Franchise Partner. Same split as the Mon/Wed ops report.
    ho_total = 0.0
    ho_lines = []
    pack_by_shipment = []
    for s in shipments:
        pack_amt, pack_types = 0.0, []
        for sc in s.get("surcharges") or []:
            amt = sc.get("amount") or 0
            total_surcharges += amt
            kind, group = classify_surcharge(sc.get("type"))
            if kind == "fuel":
                fuel_surcharges += amt
                continue
            if kind == "pass_through":
                continue
            ho_total += amt
            ho_lines.append({
                "group": group, "s": s, "types": [sc.get("type")], "amt": amt,
                "est": (amt / INSURANCE_RATE_PER_100 * 100.0 if kind == "insurance" else None),
            })
            if group in PACKAGING_GROUPS:
                pack_amt += amt
                pack_types.append(sc.get("type"))
        if pack_amt > 0:
            pack_by_shipment.append({"group": "Packaging", "s": s,
                                     "types": pack_types, "amt": pack_amt, "est": None})

    # Cost per lb removed 2026-06-11 (Mat: arbitrary metric).
    pallet_pct = (n_pallet / n_total * 100.0) if n_total > 0 else None
    nonfuel_surcharges = total_surcharges - fuel_surcharges
    surcharge_pct_ex_fuel = (nonfuel_surcharges / total_cost * 100.0) if total_cost > 0 else None
    ho_pct = (ho_total / total_cost * 100.0) if total_cost > 0 else None

    # Surcharges to chase: packaging >= $75 on a single shipment, plus insurance
    # lines whose estimated coverage clears $5,000. Combined and ranked by amount
    # (Mat 2026-09-04).
    chase = [p for p in pack_by_shipment if p["amt"] >= PACKAGING_ITEMISE_MIN]
    chase += [f for f in ho_lines
              if f["group"] == INSURANCE_GROUP and (f["est"] or 0) >= INSURANCE_VALUE_ALERT]
    chase.sort(key=lambda x: -x["amt"])

    ship_dates = [s.get("ship_date") for s in shipments if s.get("ship_date")]
    earliest = min(ship_dates) if ship_dates else None
    latest = max(ship_dates) if ship_dates else None

    return {
        "pallet_pct": pallet_pct,
        "surcharge_pct_ex_fuel": surcharge_pct_ex_fuel,
        "ho_pct": ho_pct,
        "ho_total": ho_total,
        "chase": chase,
        "n_shipments": n_total,
        "total_cost": total_cost,
        "total_weight": total_weight,
        "earliest_ship": earliest,
        "latest_ship": latest,
    }


CHASE_CARD_MAX_ROWS = 4  # card shows 4; more than that enables click-to-expand


def _chase_why(rec):
    """Short 'why' label for the TV card."""
    if rec["group"] == "Packaging":
        # Name the specific packaging charge when there is only one on the shipment.
        labels = sorted({CHASE_SHORT_LABEL.get(classify_surcharge(t)[1], "Packaging")
                         for t in rec["types"]})
        return labels[0] if len(labels) == 1 else "Packaging"
    return CHASE_SHORT_LABEL.get(rec["group"], rec["group"])


def _short_track(ref):
    """UPS airbills are 18 chars — too wide for the card. The tail is the part
    that identifies the shipment; the overlay carries the full number."""
    ref = ref or "n/a"
    return ref if len(ref) <= 12 else "…" + ref[-9:]


def chase_table_rows_html(chase, max_rows=CHASE_CARD_MAX_ROWS):
    """Surcharges to Chase card body — the biggest `max_rows` lines."""
    if chase is None:
        return '<tr><td colspan="4" class="coc-empty">—</td></tr>'
    if not chase:
        return '<tr><td colspan="4" class="coc-empty">Nothing to chase</td></tr>'
    out = []
    for f in chase[:max_rows]:
        out.append(
            f'<tr><td class="coc-job c-track">{_short_track(_tracking_ref(f["s"]))}</td>'
            f'<td class="c-mfg">{f["s"].get("manufacturer") or "Other"}</td>'
            f'<td class="c-why">{_chase_why(f)}</td>'
            f'<td class="c-amt">{fmt_currency(f["amt"])}</td></tr>'
        )
    return "".join(out)


def chase_is_clickable(chase, max_rows=CHASE_CARD_MAX_ROWS):
    return bool(chase) and len(chase) > max_rows


def chase_more_note(chase, max_rows=CHASE_CARD_MAX_ROWS):
    if not chase_is_clickable(chase, max_rows):
        return ""
    # Shorter than the other cards' hint on purpose — the chase card's label is
    # nowrap and this is the widest line in the narrowest column.
    return f'<span class="coc-more">+{len(chase) - max_rows} · click for all</span>'


def chase_click_attrs(chase):
    if not chase_is_clickable(chase):
        return ""
    return ('onclick="document.getElementById(\'chase-overlay\').classList.add(\'open\')" '
            'title="Click to see every surcharge to chase"')


def chase_modal_rows_html(chase):
    """Overlay body — every chase line, biggest first, with the detail Mat needs
    to actually chase it (delivery city for high-cost, coverage for insurance)."""
    if not chase:
        return '<tr><td colspan="6" class="coc-empty">Nothing to chase</td></tr>'
    out = []
    for f in chase:
        if f["group"] == INSURANCE_GROUP:
            detail = f'est. coverage {fmt_currency(f["est"])}'
        else:
            detail = "; ".join(sorted({(t or "").title() for t in f["types"]})) or _city_state(f["s"])
        out.append(
            f'<tr><td class="coc-job">{_tracking_ref(f["s"])}</td>'
            f'<td>{f["s"].get("manufacturer") or "Other"}</td>'
            f'<td>{_chase_why(f)}</td>'
            f'<td>{detail}</td>'
            f'<td>{f["s"].get("ship_date") or "—"}</td>'
            f'<td>{fmt_currency(f["amt"])}</td></tr>'
        )
    return "".join(out)


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

    # 9a. System Sales — ONE pull covering both R30 windows via conditional SUM.
    print("→ System Sales (single pull, R30 current + prior)...")
    rev_cur, rev_prv = system_sales_windows(w["r30_current"], w["r30_prior"])
    print(f"   R30={fmt_currency(rev_cur)}  R30prior={fmt_currency(rev_prv)}")

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

    # 9c. Measure-to-Install (current + prior) — aggregated IN SQL (one summary
    # row per window). Mat 2026-06-11: anchored on the first MEASUREMENT APPT
    # (appt type 5), not the deposit date. The deposit-based install-vs-deposit
    # skill is intentionally left untouched (other tools depend on it).
    print("→ Measure-to-Install (R30 current)...")
    mti_med_cur, mti_pct_cur, mti_n_cur = measure_to_install_aggregated(*w["r30_current"])
    print(f"   median={mti_med_cur} days   pct<10wk={mti_pct_cur}   n={mti_n_cur}")
    print("→ Measure-to-Install (R30 prior)...")
    mti_med_prv, mti_pct_prv, mti_n_prv = measure_to_install_aggregated(*w["r30_prior"])
    print(f"   median={mti_med_prv} days   pct<10wk={mti_pct_prv}   n={mti_n_prv}")

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

    # 9e-0. Nested claims still in production — table of claims whose parent is
    # itself a Claim OR a Reorder (Mat 2026-08-06: table replaces the counts).
    print("→ Mfg sheet — claims on claim/reorder...")
    nested_rows = fetch_nested_claim_rows()
    _n_prod = None if nested_rows is None else sum(1 for r in nested_rows if r["prod"])
    print(f"   rows={'—' if nested_rows is None else len(nested_rows)}  in-production={_n_prod}")

    # 9e-0b. Top claim reasons, trailing 30 days, weighted by line items.
    print("→ Mfg sheet — top claim reasons (last 30 days)...")
    reasons = top_claim_reasons(days=30, top_n=3)
    if reasons:
        print("   " + " · ".join(f"{r['reason']} {r['items']} ({r['share_pct']:.0f}%)" for r in reasons))

    claim_pct_cur = (claim_cur / total_cur * 100) if total_cur else None
    claim_pct_prv = (claim_prv / total_prv * 100) if total_prv else None
    # "Not enough data" threshold: at least 10 line items in BOTH windows for a stable comparison.
    claim_insufficient = (
        total_cur is None or total_prv is None or total_cur < 10 or total_prv < 10
    )

    # 9e-2. Claim/Reorder rate — % of jobs INSTALLED in R30 that have a claim or
    # reorder attached (Canvas-native; Mat 2026-06-11). Backfilled by live
    # recompute each refresh.
    print("→ Claim/Reorder rate (R30 current)...")
    cr_pct_cur, cr_claim_cur, cr_reorder_cur, cr_inst_cur, cr_with_cur = claim_reorder_rate(*w["r30_current"])
    print(f"   {cr_with_cur}/{cr_inst_cur} = {cr_pct_cur}  (claim={cr_claim_cur}  reorder-only={cr_reorder_cur})")
    print("→ Claim/Reorder rate (R30 prior)...")
    cr_pct_prv, _cr_claim_prv, _cr_reorder_prv, cr_inst_prv, cr_with_prv = claim_reorder_rate(*w["r30_prior"])
    print(f"   {cr_with_prv}/{cr_inst_prv} = {cr_pct_prv}")
    # Need a meaningful denominator in both windows for a stable comparison.
    cr_insufficient = (cr_inst_cur or 0) < 10 or (cr_inst_prv or 0) < 10

    # 9e-3. Order → Ship TAT — status 5 (Submitted to Mfg
    # Partner) → status 6 (Order Shipped), nearest day, for orders shipped in the
    # window. Mat 2026-06-11. Backfilled by live recompute.
    print("→ Order→Ship TAT (R30 current)...")
    tat_med_cur, tat_n_cur = order_to_ship_tat(*w["r30_current"])
    print(f"   median={tat_med_cur} days   n={tat_n_cur}")
    print("→ Order→Ship TAT (R30 prior)...")
    tat_med_prv, tat_n_prv = order_to_ship_tat(*w["r30_prior"])
    print(f"   median={tat_med_prv} days   n={tat_n_prv}")

    # Shipping (R30 — Mat 2026-09-04: was R14, moved to R30 so both Shipping
    # cards tie back to the Monday/Wednesday ops report, which is R30).
    # Anchor the window to the most recent ship date in the invoices rather than
    # today's calendar date. Invoices lag the calendar, so anchoring to today
    # causes a sparse current bucket. Falls back to today when no invoices exist.
    ship_anchor = latest_invoice_ship_date(today=w["today"]) or w["today"]
    print(f"→ Shipping anchor date (most recent ship_date): {ship_anchor}")
    r30s_current = (ship_anchor - datetime.timedelta(days=30), ship_anchor)
    r30s_prior   = (ship_anchor - datetime.timedelta(days=60), ship_anchor - datetime.timedelta(days=30))
    print("→ Shipping (R30 current)...")
    ship_cur = shipping_window_summary(*r30s_current) or {}
    print(f"   pallet%={ship_cur.get('pallet_pct')}  HO surch%={ship_cur.get('ho_pct')}  n={ship_cur.get('n_shipments')}  ships {ship_cur.get('earliest_ship')}→{ship_cur.get('latest_ship')}")
    print("→ Shipping (R30 prior)...")
    ship_prv = shipping_window_summary(*r30s_prior) or {}
    print(f"   pallet%={ship_prv.get('pallet_pct')}  HO surch%={ship_prv.get('ho_pct')}  n={ship_prv.get('n_shipments')}  ships {ship_prv.get('earliest_ship')}→{ship_prv.get('latest_ship')}")
    chase = ship_cur.get("chase") or []
    print(f"→ Surcharges to chase: {len(chase)} lines, {sum(f['amt'] for f in chase):,.2f}")

    # Overdue orders (Mfg Partner sheet, Overdue Orders tab)
    print("→ Overdue orders...")
    overdue_rows = fetch_overdue_rows()
    print(f"   {len(overdue_rows) if overdue_rows is not None else 'n/a'} overdue")
    # 9g. Indicator HTML for every metric
    last_updated = now_eastern_stamp()

    ship_span = _fmt_ship_date_span(ship_cur.get("earliest_ship"), ship_cur.get("latest_ship"))

    replacements = {
        "{{LAST_UPDATED}}": last_updated,

        # AoD Network — System Sales (headline = R30, arrow = vs prior R30)
        "{{REVENUE_VALUE}}":     fmt_currency(rev_cur),
        "{{REVENUE_INDICATOR}}": indicator_html(pct_change(rev_cur, rev_prv), lower_is_better=False),

        "{{APPT_COUNT}}":     str(appt_next if appt_next is not None else "—"),
        "{{APPT_INDICATOR}}": indicator_html(pct_change(appt_next, appt_prev), lower_is_better=False),

        "{{TOP_LOCATIONS}}":  render_list_items(top_locs, "appts"),
        "{{TOP_DESIGNERS}}":  render_list_items(top_dsrs, "appts", show_iata=True),

        # Refacing — headline = R7, whole dollars (Mat 2026-05-22): $184,000 not $184K.
        "{{REFACING_VALUE}}":     fmt_currency(rf_cur),
        "{{REFACING_INDICATOR}}": indicator_html(pct_change(rf_cur, rf_prv), lower_is_better=False),

        "{{REFACING_JOBS_VALUE}}":     str(rfj_cur if rfj_cur is not None else "—"),
        "{{REFACING_JOBS_INDICATOR}}": indicator_html(pct_change(rfj_cur, rfj_prv), lower_is_better=False),

        # Network Lead Times — Measure → Install (Mat 2026-06-11: measurement-appt
        # anchored, was deposit-anchored). Tokens renamed S2I_* → MTI_*.
        "{{MTI_MEDIAN_VALUE}}":     fmt_weeks_days(mti_med_cur),
        "{{MTI_MEDIAN_INDICATOR}}": indicator_html(pct_change(mti_med_cur, mti_med_prv), lower_is_better=True),

        "{{MTI_PCT_VALUE}}":     fmt_pct(mti_pct_cur),
        "{{MTI_PCT_INDICATOR}}": indicator_html(pct_change(mti_pct_cur, mti_pct_prv), lower_is_better=False),

        # TAT (Order → Ship) — status 5 → status 6, nearest day. Lower is better.
        "{{TAT_VALUE}}":        (f"{round(tat_med_cur)}d" if tat_med_cur is not None else "—"),
        "{{TAT_INDICATOR}}":    indicator_html(pct_change(tat_med_cur, tat_med_prv), lower_is_better=True),

        # Manufacturing — Claim Line Items %
        "{{CLAIM_PCT_VALUE}}":     fmt_pct(claim_pct_cur, decimals=2),
        "{{CLAIM_PCT_INDICATOR}}": indicator_html(
            pct_change(claim_pct_cur, claim_pct_prv),
            lower_is_better=True,
            insufficient_data=claim_insufficient,
        ),

        # Manufacturing — % of R30 installs with a claim/reorder attached
        # (Mat 2026-06-11). Lower is better. Split (Mat 2026-08-06): headline is
        # the total; the split row shows Claim % vs Reorder-only %, which sum to it.
        "{{CLAIM_REORDER_VALUE}}":     (fmt_pct(cr_pct_cur, decimals=1) if cr_pct_cur is not None else "—"),
        "{{CLAIM_REORDER_SPLIT}}": (
            f'<span class="cr-part">Claim <b>{fmt_pct(cr_claim_cur, decimals=1)}</b></span>'
            f'<span class="cr-sep">·</span>'
            f'<span class="cr-part">Reorder <b>{fmt_pct(cr_reorder_cur, decimals=1)}</b></span>'
            if cr_claim_cur is not None else ""
        ),
        "{{CLAIM_REORDER_INDICATOR}}": indicator_html(
            pct_change(cr_pct_cur, cr_pct_prv),
            lower_is_better=True,
            insufficient_data=cr_insufficient,
        ),

        # Manufacturing — Claims + table: in-production claims whose parent is a
        # Claim or a Reorder, with the claim's chain position (Mat 2026-08-06).
        # With 4+ in production the card caps at 3 rows and becomes clickable,
        # opening an overlay listing every in-production one.
        "{{COC_TABLE_ROWS}}": coc_table_rows_html(nested_rows),
        "{{COC_MORE_NOTE}}": coc_more_note(nested_rows),
        "{{COC_CLICK_ATTRS}}": coc_click_attrs(nested_rows),
        "{{COC_CLICKABLE_CLASS}}": ("claims-clickable" if coc_is_clickable(nested_rows) else ""),
        "{{COC_MODAL_ROWS}}": coc_modal_rows_html(nested_rows),
        "{{COC_ALL_COUNT}}":  str(sum(1 for r in (nested_rows or []) if r["prod"])),

        # Manufacturing — top claim reasons, trailing 30 days (line items).
        "{{TOP_CLAIM_REASONS}}": claim_reasons_html(reasons),

        # Shipping (R14) — Cost per lb REMOVED 2026-06-11 (arbitrary metric).
        "{{PALLET_PCT_VALUE}}":     fmt_pct(ship_cur.get("pallet_pct")),
        # Pallet % — HIGHER is better (more pallet shipments = better packing/cost).
        # Increase → green, decrease → red.
        "{{PALLET_PCT_INDICATOR}}": indicator_html(
            pct_change(ship_cur.get("pallet_pct"), ship_prv.get("pallet_pct")),
            lower_is_better=False,
        ),

        # Surcharges the Home Office fronts — not fuel, not memo pass-through.
        "{{HO_SURCHARGE_PCT_VALUE}}":     fmt_pct(ship_cur.get("ho_pct")),
        "{{HO_SURCHARGE_PCT_INDICATOR}}": indicator_html(
            pct_change(ship_cur.get("ho_pct"), ship_prv.get("ho_pct")),
            lower_is_better=True),

        "{{CHASE_TABLE_ROWS}}":      chase_table_rows_html(chase),
        "{{CHASE_MORE_NOTE}}":       chase_more_note(chase),
        "{{CHASE_CLICK_ATTRS}}":     chase_click_attrs(chase),
        "{{CHASE_CLICKABLE_CLASS}}": ("claims-clickable" if chase_is_clickable(chase) else ""),
        "{{CHASE_MODAL_ROWS}}":      chase_modal_rows_html(chase),
        "{{CHASE_ALL_COUNT}}":       str(len(chase)),
        "{{CHASE_TOTAL}}":           fmt_currency(sum(f["amt"] for f in chase)),

        # Overdue orders
        "{{OVERDUE_TABLE_ROWS}}":      overdue_table_rows_html(overdue_rows),
        "{{OVERDUE_MORE_NOTE}}":       overdue_more_note(overdue_rows),
        "{{OVERDUE_CLICK_ATTRS}}":     overdue_click_attrs(overdue_rows),
        "{{OVERDUE_CLICKABLE_CLASS}}": ("claims-clickable" if overdue_is_clickable(overdue_rows) else ""),
        "{{OVERDUE_MODAL_ROWS}}":      overdue_modal_rows_html(overdue_rows),
        "{{OVERDUE_ALL_COUNT}}":       str(len(overdue_rows or [])),

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


GIT_LOCK_TRASH_DIRNAME = "_sandbox_trash"

def _cleanup_stale_git_locks(git_dir):
    """Clear stale *.lock litter inside .git/ so the next git command can run.

    Why this exists: Git creates short-lived lock files (.git/index.lock,
    .git/HEAD.lock, .git/refs/**/<ref>.lock, .git/objects/maintenance.lock)
    while it works, and normally deletes them in milliseconds. But inside the
    Cowork sandbox the FUSE mount refuses unlink() inside .git/ ("Operation
    not permitted"), so every git command leaves its lock behind and breaks
    the *next* command with "Unable to create '.git/index.lock': File exists."

    Two hard-won rules (both learned from the 2026-06 wedge):
      • Sweep RECURSIVELY. Locks appear under refs/ and objects/, not just at
        the top level. A single missed refs/**/.lock leaves the repo wedged.
      • QUARANTINE, never rename-in-place. The old code renamed a lock to
        "<name>.lock.old" right where it sat. A lock in refs/heads/ thus became
        refs/heads/main.lock.old — and git scans every file under refs/ as a
        branch, so that planted a phantom ref ("fatal: bad object
        refs/heads/main.lock.old") that broke fetch/reset outright. We instead
        MOVE each lock into .git/_sandbox_trash/ (never scanned by git, never
        in the working tree, so never published).

    This also mops up any legacy *.lock.old / *.lock.gone litter left by the
    old in-place-rename strategy. On Mat's Mac unlink works, so locks are just
    deleted outright. If every path fails it logs and continues — the GitHub
    API fallback still publishes the dashboard.
    """
    if not os.path.isdir(git_dir):
        return
    trash = os.path.join(git_dir, GIT_LOCK_TRASH_DIRNAME)
    targets = []
    for root, dirs, files in os.walk(git_dir):
        if os.path.basename(root) == GIT_LOCK_TRASH_DIRNAME:
            dirs[:] = []  # never descend into our own quarantine dir
            continue
        for fn in files:
            if fn.endswith((".lock", ".lock.old", ".lock.gone")):
                targets.append(os.path.join(root, fn))
    for path in targets:
        try:
            os.unlink(path)            # works on Mat's Mac; no-op cost is tiny
            continue
        except OSError:
            pass
        # Sandbox can't unlink — move it somewhere git never scans.
        try:
            os.makedirs(trash, exist_ok=True)
            flat = os.path.relpath(path, git_dir).replace(os.sep, "__")
            stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
            os.replace(path, os.path.join(trash, f"{flat}.{stamp}"))
        except OSError as e:
            print(f"  ! could not clear git lock {path}: {e}", file=sys.stderr)


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
        # In the sandbox, fetch leaves an undeletable objects/maintenance.lock
        # (and reset is about to write index.lock + HEAD.lock). Clear leftovers
        # before each step so the *next* command never hits "File exists".
        _cleanup_stale_git_locks(git_dir)
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
        # reset left an index.lock behind in the sandbox — clear it before add.
        _cleanup_stale_git_locks(git_dir)
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
            # add left an index.lock behind in the sandbox — clear it before commit.
            _cleanup_stale_git_locks(git_dir)
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
