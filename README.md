# CSV Viewer

A fast, read-only desktop viewer for very large CSV files (tested up to 11 GB / 160 million rows).
Built with PyQt6 for Ubuntu / GNOME. Rows appear immediately and you can scroll while the file is
still being indexed.

## Install

Requirements (system packages, no pip):

```bash
sudo apt install python3-pyqt6 python3-numpy
```

Then, from the repo folder:

```bash
./install.sh
```

This is user-level only (no sudo) and safe to re-run. It adds:

- `~/.local/bin/csvview` (symlink to `./csvview`)
- `~/.local/share/applications/csv-viewer.desktop` (app menu + "Open With")
- `~/.local/share/icons/hicolor/scalable/apps/csv-viewer.svg`

It does not change your default app for CSV files. Remove everything with `./uninstall.sh`.

## Opening files

- App menu: **CSV Viewer**
- Files: right-click a CSV → **Open With** → **CSV Viewer**
- Terminal: `csvview file.csv`
- Drag and drop a file onto the window
- **File ▸ Open** (Ctrl+O)
- The **Explorer** side panel (F4): double-click a CSV

Each file opens in its own tab.

## Features

- **Tabs** — several files open at once.
- **Explorer side panel** — browse folders, filter by name, "CSV only" toggle; double-click opens
  a file in a new tab.
- **Frozen header** — the header row always stays visible.
- **Freeze columns** — keep the first N columns in view: toolbar spin box, or right-click a column
  header ▸ **Freeze columns up to here**.
- **Search the whole file** — optional column scope and **Match case** (case folding is ASCII-only).
  **Show only matches** filters the view to matching rows; **Stop** cancels a running search.
- **Export matching rows** to a new CSV.
- **Cell detail panel** — shows the full value of the current cell.
- **Copy** — selection as tab-separated text, optionally with the header (max 200,000 cells).
- **Hide / show columns** — right-click a header, or **View ▸ Columns…**
- **Fit column widths** to content.
- **Reopen with options** — delimiter, encoding, header row on/off, quoted line breaks.

## Keyboard shortcuts

**Files and tabs**

| Shortcut | Action |
|---|---|
| Ctrl+O / Ctrl+T | Open a file in a new tab |
| Ctrl+W | Close tab |
| Ctrl+Tab / Ctrl+Shift+Tab | Next / previous tab |
| Ctrl+Shift+O | Reopen with options |
| F4 | Toggle Explorer panel |

**Search**

| Shortcut | Action |
|---|---|
| Ctrl+F | Focus the search box |
| Enter / F3 | Find / next match |
| Shift+Enter / Shift+F3 | Previous match |
| Ctrl+E | Export matching rows |

**Navigation and view**

| Shortcut | Action |
|---|---|
| Ctrl+G | Go to row |
| Ctrl+Home / Ctrl+End | First / last row |
| Ctrl+D or double-click a cell | Cell detail panel |
| Ctrl+C | Copy selection (tab-separated) |
| Ctrl+Shift+C | Copy selection with header |
| Ctrl+Shift+H | Show / hide columns |
| Ctrl+R | Fit column widths |

## Performance

Measured on an 11 GB file with 160.6 million rows:

| | |
|---|---|
| Indexing | ~20 s (rows are viewable and scrollable immediately) |
| Random row access | ~0.3 ms |
| Whole-file search | 4–8 s |
| Memory | ~230 MB |

## Limitations

- Read-only; no editing.
- No sorting.
- Search is available once indexing has finished.
- Files over 50 million rows are shown 50 million rows at a time; switch with the "Rows …" selector at the bottom right. Go to row, search and Ctrl+End switch automatically.
