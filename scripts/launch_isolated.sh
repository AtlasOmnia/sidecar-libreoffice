#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
set -eu
# Isolated profile and temp directory: see scripts/dev_paths.py.
DEV_ROOT=${HERMES_LO_DEV_ROOT:-$HOME/.cache/hermes-libreoffice-dev}
PROFILE=${HERMES_LO_PROFILE:-$DEV_ROOT/profile}
REAL_PROFILE="$HOME/Library/Application Support/LibreOffice"
SOFFICE_DEFAULT=/Applications/LibreOffice.app/Contents/MacOS/soffice
SOFFICE=${SOFFICE:-$SOFFICE_DEFAULT}
mkdir -p "$PROFILE" "$DEV_ROOT/tmp"
PROFILE_REAL=$(cd "$PROFILE" && pwd -P)
case "$PROFILE_REAL" in
  "$REAL_PROFILE"|"$REAL_PROFILE"/*)
    echo "refusing a profile under the real LibreOffice profile" >&2
    exit 2
    ;;
esac
TMPDIR="$DEV_ROOT/tmp" \
  "$SOFFICE" "-env:UserInstallation=file://$PROFILE_REAL" \
  --nologo --nofirststartwizard --norestore "$@" &
pid=$!
printf '%s\n' "$pid"
