# Light-CSV MCP server

Light-CSV includes an [MCP](https://modelcontextprotocol.io) server, so other applications (Claude Code, Claude Desktop, Cursor, VS Code, your own scripts) can drive the viewer: open files, read and search rows, run SQL over whole files, and control the window.

- **Full control, except the Assistant.** External apps get the viewer and its data; the Assistant chat, its keys and its history are never exposed.
- **Local only.** The server listens on `127.0.0.1` (not your network) and every request needs a secret token that lives in your user config folder. Requests from web pages are refused (Origin and Host checks).
- **On by default** while Light-CSV is open. Turn it off or on in **Tools ▸ MCP server**. **Tools ▸ Connect an app…** shows copy-ready setup for your machine.

## Connect an app

Open **Tools ▸ Connect an app…** - it fills in the real paths, URL and token. The shapes are:

### Claude Code

```sh
# Direct HTTP (Light-CSV must be open)
claude mcp add --transport http light-csv http://127.0.0.1:8765/mcp --header "Authorization: Bearer <TOKEN>"

# Or through the bridge, which starts Light-CSV if it is closed
claude mcp add light-csv -- /path/to/csv-viewer/.venv/bin/python /path/to/csv-viewer/csvview_mcp.py
```

### Claude Desktop and other apps that start servers as a command (stdio)

Add to `claude_desktop_config.json` (Claude Desktop ▸ Settings ▸ Developer ▸ Edit Config):

```json
{
  "mcpServers": {
    "light-csv": {
      "command": "/path/to/csv-viewer/.venv/bin/python",
      "args": ["/path/to/csv-viewer/csvview_mcp.py"]
    }
  }
}
```

With the standalone Windows build, the bridge is `csvview-mcp.exe` next to `CSV Viewer.exe`:

```json
{ "mcpServers": { "light-csv": { "command": "C:\\Apps\\CSV Viewer\\csvview-mcp.exe", "args": [] } } }
```

### Apps that connect to an MCP URL (HTTP)

```json
{
  "mcpServers": {
    "light-csv": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer <TOKEN>" }
    }
  }
}
```

The port is 8765, or the next free one if it is taken - the dialog always shows the current URL.

## Tools

Tabs are numbered by position (`tabId` 1, 2, …). Rows are 1-based data rows, as in the viewer's row header. Each open tab is also a DuckDB SQL view `t1`, `t2`, … (plus `t1_text`, which reads every column as text).

| Tool | What it does |
|---|---|
| `get_app_state` | Window, theme, every tab (file, size, rows, columns, indexing progress, frozen/hidden columns, active search) and the current cell |
| `open_file` | Open a CSV in a new tab (delimiter, encoding, header, quoted line breaks; optionally wait for indexing) |
| `close_tab` / `switch_tab` | Close or focus a tab |
| `reopen_tab` | Reopen a tab with different delimiter / encoding / header options |
| `list_tabs` / `describe_tab` | Open tabs; columns plus sample rows from the start, middle and end |
| `get_rows` | Read up to 100 rows from any position |
| `search_rows` | Search the whole file (optionally one column, case-sensitive) without touching the UI |
| `run_sql` | Read-only DuckDB SQL over the full files (`SELECT`, `DESCRIBE`, `SUMMARIZE`, …), up to 200 result rows |
| `go_to_row` | Select a row (and column) in the viewer and scroll to it |
| `set_search` / `clear_search` | Drive the tab's search bar, optionally "only matches", and wait for the result |
| `set_frozen_columns` | Pin the first N columns |
| `set_column_visibility` / `show_all_columns` | Hide or show columns |
| `export_matches` | Write the header plus the current search's matching rows to a new CSV |
| `get_selection` | The current cell and selected range (up to 2,000 cells) |
| `set_theme` | System, light or dark |

## Details

- **Transport:** MCP Streamable HTTP at `/mcp` (JSON responses, protocol versions 2024-11-05 to 2025-11-25). The bridge (`csvview_mcp.py`, built as `csvview-mcp`) converts stdio to that HTTP endpoint. Connecting never opens a window: the bridge answers the handshake itself and lists tools from a copy the app saves; it starts the app (with `--mcp`) only when a tool is actually called.
- **Token and state:** `mcp.json` in `~/.config/csv-viewer/` (Linux), `%APPDATA%\csv-viewer\` (Windows) or `~/Library/Application Support/csv-viewer/` (macOS); file mode 600 on Linux/macOS. Delete the file to issue a new token (reconnect your apps afterwards).
- Long operations (indexing an 11 GB file, a full-file search or SQL scan) run off the UI thread, so the window stays responsive while an app works with it.
