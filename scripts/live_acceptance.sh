#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
# Live acceptance against an already running, GUI-installed isolated
# LibreOffice. It never launches, installs or kills LibreOffice and runs no
# model.
set -eu
usage() {
  echo "usage: scripts/live_acceptance.sh <output-dir>" >&2
  echo "  requires: isolated LibreOffice running with calc-a.ods, calc-b.ods, writer-proof.odt, impress-proof.odp" >&2
}
if [ "$#" -ne 1 ] || [ "$1" = "--help" ] || [ "$1" = "-h" ]; then usage; [ "$#" -eq 1 ] && exit 0; exit 2; fi
OUT=$1
HERE=$(cd "$(dirname "$0")" && pwd -P)
REPO=$(dirname "$HERE")
# Isolated profile and temp directory: see scripts/dev_paths.py.
DEV_ROOT=${HERMES_LO_DEV_ROOT:-$HOME/.cache/hermes-libreoffice-dev}
PROFILE=${HERMES_LO_PROFILE:-$DEV_ROOT/profile}
TOKEN=$PROFILE/user/hermes-bridge/token
export TMPDIR=$DEV_ROOT/tmp
mkdir -p "$TMPDIR"
mkdir -p "$OUT"
python3 "$HERE/real_profile_sentinel.py" snapshot "$OUT/real-profile-before.json"
python3 "$REPO/scripts/build_oxt.py" > "$OUT/build.txt"
python3 "$HERE/verify_isolated_install.py" "$REPO/dist/sidecar-libreoffice.oxt" > "$OUT/install-verify.json"
python3 "$HERE/live_client.py" --token-file "$TOKEN" health > "$OUT/health.json"
python3 "$HERE/live_binding_check.py" --token-file "$TOKEN" --output-dir "$OUT"
python3 "$HERE/live_calc_sweep.py" --token-file "$TOKEN" --output-dir "$OUT"
python3 "$HERE/live_generalization_check.py" --token-file "$TOKEN" --output-dir "$OUT"
python3 "$HERE/real_profile_sentinel.py" snapshot "$OUT/real-profile-after.json"
python3 "$HERE/real_profile_sentinel.py" compare "$OUT/real-profile-before.json" "$OUT/real-profile-after.json"
echo "LIVE_ACCEPTANCE PASS"
