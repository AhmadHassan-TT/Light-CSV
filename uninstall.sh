#!/usr/bin/env bash
# Removes the user-level CSV Viewer install. Safe to run again.
set -uo pipefail

appsDir="$HOME/.local/share/applications"
iconsRoot="$HOME/.local/share/icons/hicolor"

rm -f "$HOME/.local/bin/csvview"
rm -f "$iconsRoot/scalable/apps/csv-viewer.svg"
rm -f "$appsDir/csv-viewer.desktop"

command -v update-desktop-database >/dev/null && update-desktop-database "$appsDir" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t -q "$iconsRoot" || true

echo "CSV Viewer removed."
