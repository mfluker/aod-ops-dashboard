#!/bin/bash
# Run the AoD ops dashboard refresh.
#
# Usage:
#   ./run.sh             # uses default Mfg sheet URL from .env if present
#   AOD_MFG_SHEET_CSV_URL="https://..." ./run.sh
#
# Loads AOD_MFG_SHEET_CSV_URL from .env in this folder if it exists.

set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

python3 refresh.py
