#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
set -eu
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo "usage: scripts/install_isolated.sh <extension.oxt>" >&2
  exit 2
fi
case "$1" in
  *.oxt) ;;
  *) echo "extension must have an .oxt suffix" >&2; exit 2 ;;
esac
exec "$(dirname "$0")/launch_isolated.sh" "$(cd "$(dirname "$1")" && pwd -P)/$(basename "$1")"
