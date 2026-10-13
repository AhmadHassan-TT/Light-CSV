# CSV Viewer

A fast, read-only desktop viewer for very large CSV files (tested up to 11 GB / 160 million rows).
Built with PyQt6 for Linux (Ubuntu / GNOME) and Windows. Rows appear immediately and you can
scroll while the file is still being indexed.

## Install

### Linux

**Option A: single binary (no Python needed).** Take `CSV-Viewer-linux-x86_64.tar.gz` from the
GitHub Actions build artifacts (or a release), then:

```bash
tar -xzf CSV-Viewer-linux-x86_64.tar.gz
./csv-viewer file.csv
```

It is built on Ubuntu 22.04 and runs on that or newer (glibc 2.35+). If it exits with a Qt "xcb"
platform plugin error, install `sudo apt install libxcb-cursor0`. To build it yourself on Linux:
`pip install -r requirements.txt pyinstaller && python build.py --onefile`.

**Option B: from source.** Requirements (system packages, no pip):

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

### Windows

**Option A: standalone app (no Python needed).** Take the `CSV-Viewer-windows-*.zip` from the
GitHub Actions build artifacts (or a release), or build it yourself on a Windows machine:

```powershell
pip install -r requirements.txt pyinstaller
python build.py
```

Unzip `dist\CSV-Viewer-windows-*.zip` anywhere and run `CSV Viewer.exe`. The exe is unsigned, so
Windows SmartScreen may warn on first launch: **More info → Run anyway**. PyInstaller cannot
cross-compile; `build.py` builds for the OS it runs on.

To share a single file instead of a zip, build with `python build.py --onefile`. This gives you
`dist\CSV Viewer.exe` (about 45 MB). It runs without Python or anything else installed, but each
launch takes a few seconds longer because it unpacks itself to a temp folder first.

**Option B: from source.** Install Python 3.12 (`winget install Python.Python.3.12`, or from
python.org), then from the repo folder:

```powershell
powershell -ExecutionPolicy Bypass -File install-windows.ps1
```

This is user-level only (no admin) and safe to re-run. It installs PyQt6 and numpy with
`pip --user`, adds a Start Menu shortcut **CSV Viewer**, adds CSV Viewer to **Open with** for
`.csv .tsv .txt .psv .dat` (your default app is not changed), and writes `csvview.cmd` for
terminal use. Remove it with `uninstall-windows.ps1`.

## Opening files

- App menu / Start Menu: **CSV Viewer**
- Files: right-click a CSV → **Open With** → **CSV Viewer**
- Terminal: `csvview file.csv` (Linux) or `csvview.cmd file.csv` (Windows, from source)
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
- **Light / dark theme** — **View ▸ Theme**: System, Light or Dark (Ctrl+Shift+L toggles). The
  choice is remembered.
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
| Ctrl+Shift+W | Close all tabs (right-click a tab for Close others / Close tabs to the right) |
| Ctrl+Shift+Q | Query builder panel (Ctrl+Shift+A: Assistant panel) |
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
| Ctrl+Shift+L | Toggle light / dark theme |

## Performance

Measured on an 11 GB file with 160.6 million rows:

| | |
|---|---|
| Indexing | ~20 s (rows are viewable and scrollable immediately) |
| Random row access | ~0.3 ms |
| Whole-file search | 4–8 s |
| Memory | ~230 MB |

## Query builder

**⊞ Query** (top right) or **Ctrl+Shift+Q** opens the Query tab of the right-hand side panel (next to **✦ Assistant**, Ctrl+Shift+A; the same button or shortcut hides it again). It answers questions about a whole file - no SQL needed:

- **Drag a column header** from the table into the panel, like Studio 3T: drop it on **Filter rows** to filter by it, on **Group & summarize** to group by it, or on **Show columns** to include it (from another tab, the panel switches to that file). Right-click a header ▸ **Filter by / Group by this column in Query** does the same.

- **File:** pick any open tab.
- **Show columns:** tick the columns you want.
- **Filter rows:** e.g. *status equals inService*, *amount ≥ 100*, *city contains calg*, *note is empty*; match **all** or **any** of the filters. Text matching ignores case.
- **Group & summarize:** one row per group with Count, Count distinct, Sum, Average, Min or Max.
- **Sort by** a column or summary, **Limit rows**, **Only distinct rows**.
- **Run** (Ctrl+Enter) runs it with DuckDB over the full file - multi-GB files take seconds; **Stop** cancels.
- **Export results…** writes the full result to a CSV; **Open results as new tab** opens it in the viewer.
- The generated SQL is shown live; **Edit SQL** lets you change it by hand (read-only queries only; tab N is view `tN`).

Needs the `duckdb` package (installed by `./install.sh` / `install-windows.ps1`, included in the standalone builds).

## Assistant (Claude, or free Google Gemini / Groq)

Ask questions about the open tabs in plain language: **✦ Assistant** (top right) or **Ctrl+Shift+A** opens the panel on the right. Pick the AI at the top of the panel:

- **Google Gemini (free):** press **Get a free key** (aistudio.google.com, Google account, no payment card), paste the key, done. Free-tier limits apply (per minute and per day), and Google may use free-tier prompts to improve its products. Default model Gemini 3.8 Flash; Gemini 3.5 Flash-Lite is faster with higher free limits.
- **Groq (free):** press **Get a free key** (console.groq.com, sign up with email, Google or GitHub, no payment card), paste the key. Models: GPT-OSS 120B (default), GPT-OSS 20B (faster), Llama 3.3 70B. The free plan allows about 30 requests a minute and 1,000 a day with a small per-minute token budget, so long questions may pause for a minute.
- **Claude:** sign in with your own Claude API account: press **Sign in with Claude** (opens your browser; uses Anthropic's official `ant` CLI, downloaded automatically if missing), or paste an API key. Usage is billed to that account; the model is Claude Haiku 5.5 (low cost).
- View ▸ Sign out of the assistant forgets the current provider's key or sign-in.
- **History:** every chat is saved automatically. **◷ History** reopens one (you can keep asking in it), deletes the current chat, or clears all. Chats are stored on this PC only.
- **⤢** opens the assistant in a larger window for long reports; **⤡** puts it back. You can also drag the panel's edge to make it wider.
- **What leaves your PC:** only small samples and query results - never the file. Claude works through local tools: list tabs, sample rows, read rows, search, **SQL over the whole file (DuckDB; tab N is view `tN`)**, and jump to rows. Each step shows as a chip you can expand.
- Answers link rows like *row 7*: click to jump there. **Stop** cancels a running query; **New chat** starts fresh.
- Requires the `anthropic` and `duckdb` packages: `./install.sh` puts them in `.venv` (Linux), `install-windows.ps1` installs them, and the standalone builds include them. Without them the viewer works and the panel says what is missing.

## MCP server (control from other apps)

Other applications - Claude Code, Claude Desktop, Cursor, VS Code or your own scripts - can control Light-CSV over [MCP](https://modelcontextprotocol.io): open files, read and search rows, run DuckDB SQL over whole files, jump to rows, filter, freeze and hide columns, export matches and switch the theme. Everything except the Assistant.

- On by default while the app is open, local to this PC only (`127.0.0.1` + a secret token). **Tools ▸ MCP server** turns it on or off.
- **Tools ▸ Connect an app…** shows copy-ready setup for Claude Code, Claude Desktop (stdio, via the `csvview-mcp` bridge that also starts the app) and HTTP clients.
- Full guide and tool list: [MCP.md](MCP.md).

## Limitations

- Read-only; no editing.
- No sorting.
- Search is available once indexing has finished.
- Files over 50 million rows are shown 50 million rows at a time; switch with the "Rows …" selector at the bottom right. Go to row, search and Ctrl+End switch automatically.
