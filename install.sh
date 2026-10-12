#!/usr/bin/env bash
# Installs CSV Viewer for the current user (no sudo). Safe to run again.
set -euo pipefail

repo="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
binDir="$HOME/.local/bin"
appsDir="$HOME/.local/share/applications"
iconsRoot="$HOME/.local/share/icons/hicolor"
iconDir="$iconsRoot/scalable/apps"
desktopFile="$appsDir/csv-viewer.desktop"

# Check runtime dependencies
if ! python3 -c 'import PyQt6.QtWidgets, numpy' 2>/dev/null; then
    echo "Missing dependencies: python3 needs PyQt6 and numpy."
    echo "Install them with:"
    echo "    sudo apt install python3-pyqt6 python3-numpy"
    echo "(continuing with the install; the app will not start until these are present)"
fi

# Assistant packages (anthropic, duckdb) go in a .venv that still sees the system PyQt6 and numpy
if python3 -m venv --help >/dev/null 2>&1; then
    [ -x "$repo/.venv/bin/python" ] || python3 -m venv --system-site-packages "$repo/.venv"
    "$repo/.venv/bin/pip" install -q --upgrade anthropic duckdb || echo "Could not install the assistant packages; the viewer still works."
else
    echo "python3-venv is missing, so the Assistant panel is unavailable: sudo apt install python3-venv python3-pip"
fi

mkdir -p "$binDir" "$appsDir" "$iconDir"
chmod +x "$repo/csvview"

ln -sfn "$repo/csvview" "$binDir/csvview"
install -m 644 "$repo/assets/csv-viewer.svg" "$iconDir/csv-viewer.svg"

cat > "$desktopFile" <<DESKTOP
[Desktop Entry]
Type=Application
Name=CSV Viewer
GenericName=CSV File Viewer
Comment=View very large CSV files quickly
Exec="$repo/csvview" %f
Icon=csv-viewer
Terminal=false
Categories=Office;Viewer;
MimeType=text/csv;text/tab-separated-values;application/csv;text/x-csv;
StartupWMClass=csv-viewer
Keywords=csv;spreadsheet;viewer;
DESKTOP
chmod 644 "$desktopFile"

command -v update-desktop-database >/dev/null && update-desktop-database "$appsDir" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t -q "$iconsRoot" || true

echo "Installed:"
echo "  $binDir/csvview -> $repo/csvview"
echo "  $iconDir/csv-viewer.svg"
echo "  $desktopFile"
case ":$PATH:" in
    *":$binDir:"*) ;;
    *) echo "Note: $binDir is not on your PATH; add it to run 'csvview' from a terminal." ;;
esac
