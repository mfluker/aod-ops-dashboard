#!/bin/bash
# Run the AoD ops dashboard refresh.
#
# Usage:
#   ./run.sh             # uses default Mfg sheet URL from .env if present
#   AOD_MFG_SHEET_CSV_URL="https://..." ./run.sh
#
# Loads AOD_MFG_SHEET_CSV_URL from .env in this folder if it exists.
#
# Scheduled by macOS launchd via ~/Library/LaunchAgents/com.artofdrawers.opsdashboard.plist
# (M-F 9am/12pm/3pm ET). We pin Python to /usr/bin/python3 so the
# interactive run and the scheduled run use the exact same interpreter —
# previously launchd would use system Python while Terminal used conda,
# and packages installed in one wouldn't be visible to the other.

set -euo pipefail
cd "$(dirname "$0")"

# System Python — same one launchd will resolve, same one we install xlrd into.
PYTHON=/usr/bin/python3

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# Make sure Python dependencies (e.g. xlrd for the WWEX invoice parser) are
# installed before we run the refresh. Cheap when they're already present —
# pip just prints "Requirement already satisfied" and exits in well under a
# second. Without this, a missing dependency silently zeros-out the matching
# dashboard section (e.g. Shipping → "no prior data").
#
# --user installs into ~/Library/Python/<ver>/lib/python/site-packages, which
# is on Python's sys.path automatically and doesn't need sudo. Works on every
# pip version: avoids the PEP 668 --break-system-packages flag (Python 3.11+)
# which Apple's older CommandLineTools pip doesn't know about.
if [ -f requirements.txt ]; then
    "$PYTHON" -m pip install -q --user -r requirements.txt
fi

"$PYTHON" refresh.py
