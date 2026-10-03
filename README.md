# defold-mcp

Stdio MCP server that gives an MCP client (Claude Code, Claude Desktop, ...) control of the Defold editor. Tested with Defold 1.13.2 on Linux.

It talks to the HTTP server built into the editor. For every open project the editor writes its port to `<project>/.internal/editor.port` and a token to `.internal/editor.token`. The server re-reads both on every call, so restarting the editor needs no reconfiguration. Nothing has to be added to the Defold project.

## Tools

| Tool | Purpose |
|---|---|
| `defold_status` | Project in use, editor URL, token, editor version and project title |
| `defold_set_project` | Switch to another project |
| `defold_api` | Raw call to the editor HTTP API. `GET /openapi.json` lists everything: `/command/build`, `/command/run`, `/command/hot-reload`, `/bob`, `/prefs/...` |
| `defold_eval` | Runs Lua in the editor extension runtime (`POST /eval`) with the full editor API: `editor.get`, `editor.transact`, `editor.save`, `editor.execute`... Returned values come back as JSON. |
| `defold_console` | Last lines of the editor console, with an optional filter |
| `defold_preview` | Renders a scene resource (`.collection`, `.go`, `.gui`, `.atlas`...) to PNG with the editor's renderer (`GET /preview/{path}`) |
| `defold_screenshot` | Screenshot of the editor window (`editor`), the running game (`game`) or any other window. Linux with Hyprland only (`hyprctl` + `grim`); the other tools work everywhere. |

## Setup

Requires [uv](https://docs.astral.sh/uv/) and a Defold editor with the project open.

```sh
claude mcp add --scope user defold -e DEFOLD_PROJECT=/path/to/project -- uv run --script /path/to/defold_mcp.py
```

Dependencies (`mcp<2`) are declared in the script header and installed by `uv`. `mcp` 2.x renamed `FastMCP`, hence the pin.

## Security

`defold_eval` runs arbitrary Lua inside the editor. It can change or delete project files and run commands (`editor.execute`). The editor only listens locally, and `/eval`, `/bob` and the commands require the bearer token. The editor writes `.internal/editor.token` world-readable (`rw-r--r--` on 1.13.2), so anyone who can read the project folder can use it. Read-only routes such as `/console` and `/openapi.json` answer without a token.

Routes added by editor scripts (`get_http_server_routes`) are not token-protected (checked on 1.13.2). Do not add a Lua eval route of your own: any local process could call it. This server only uses the built-in, token-protected `/eval`.
