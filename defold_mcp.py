#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2"]
# ///
"""MCP server for the Defold editor (stdio).

It talks to the HTTP server that every open Defold editor runs:
  - port in  <project>/.internal/editor.port
  - token in <project>/.internal/editor.token, sent as "Authorization: Bearer"
Both are re-read on every call, so restarting the editor needs no reconfiguration.
GET /openapi.json on that server lists every endpoint.

Env: DEFOLD_PROJECT = project directory (the one with game.project).
"""
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("defold")
STATE = {"project": Path(os.environ.get("DEFOLD_PROJECT", os.getcwd())).expanduser()}
MAX_TEXT = 60000
RESULT_MARK = "@@MCP_RESULT@@ "

# Runs the user's code and prints each returned value as JSON on a marked line,
# because the editor's /eval prints tables as "table: 0x...".
EVAL_WRAPPER = r"""
local __code = %s
local __fn, __err = load(__code, "eval", "t")
if not __fn then error(__err, 0) end
local function __plain(v, depth, seen)
  local t = type(v)
  if t == "nil" or t == "boolean" or t == "number" or t == "string" then return v end
  if t ~= "table" then return tostring(v) end
  if depth >= 8 then return "<max depth>" end
  if seen[v] then return "<cycle>" end
  seen[v] = true
  local n, is_array = 0, true
  for k in pairs(v) do
    n = n + 1
    if type(k) ~= "number" or k < 1 or k %% 1 ~= 0 then is_array = false end
  end
  if is_array and n ~= #v then is_array = false end
  local out = {}
  for k, x in pairs(v) do
    out[is_array and k or tostring(k)] = __plain(x, depth + 1, seen)
  end
  seen[v] = nil
  return out
end
local __r = table.pack(__fn())
for __i = 1, __r.n do
  print("%s" .. json.encode(__plain(__r[__i], 0, {})))
end
"""


def _lua_long_string(s: str) -> str:
    level = 0
    while f"]{'=' * level}]" in s:
        level += 1
    eq = "=" * level
    return f"[{eq}[\n{s}]{eq}]"


def _project() -> Path:
    return STATE["project"]


def _conn():
    internal = _project() / ".internal"
    try:
        port = (internal / "editor.port").read_text().strip()
    except FileNotFoundError:
        raise RuntimeError(f"No editor.port in {internal}: is the project open in the Defold editor?")
    token_file = internal / "editor.token"
    token = token_file.read_text().strip() if token_file.exists() else None
    return f"http://localhost:{port}", token


def _request(method: str, path: str, body=None, timeout: float = 120.0, content_type=None):
    base, token = _conn()
    data = None
    headers = {}
    if body is not None:
        if isinstance(body, (dict, list)):
            data, content_type = json.dumps(body).encode(), content_type or "application/json"
        else:
            data = body.encode() if isinstance(body, str) else body
            content_type = content_type or "application/json"
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()
    except urllib.error.URLError as e:
        raise RuntimeError(f"Editor not reachable at {base} ({e.reason}). Is the project open?")


def _text(status, ctype, raw) -> str:
    text = raw.decode("utf8", "replace")
    if "json" in ctype:
        try:
            text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        except ValueError:
            pass
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + f"\n... [truncated, {len(raw)} bytes]"
    return f"HTTP {status}\n{text}"


def _eval(code: str, timeout: float = 120.0):
    """Returns (ok, printed_lines, results)."""
    wrapped = EVAL_WRAPPER % (_lua_long_string(code), RESULT_MARK)
    st, ct, raw = _request("POST", "/eval", wrapped, timeout, content_type="text/plain")
    if st == 401:
        raise RuntimeError("Editor refused the token (HTTP 401)")
    printed, results = [], []
    for line in raw.decode("utf8", "replace").splitlines():
        if line.startswith(RESULT_MARK):
            results.append(json.loads(line[len(RESULT_MARK):]))
        elif line.startswith("=> "):
            continue  # the wrapper itself returns nothing
        else:
            printed.append(line)
    return st == 200, printed, results


@mcp.tool()
def defold_status() -> str:
    """Project in use, editor URL, token, and editor/project info."""
    lines = [f"project: {_project()}"]
    try:
        base, token = _conn()
        lines.append(f"editor: {base} (token {'present' if token else 'absent'})")
        ok, printed, res = _eval('return {version = editor.version, platform = editor.platform, '
                                 'title = editor.get("/game.project", "project.title")}', 10)
        lines.append(("info: " + json.dumps(res[0], ensure_ascii=False)) if ok and res else "eval failed: " + "\n".join(printed))
    except RuntimeError as e:
        lines.append(str(e))
    return "\n".join(lines)


@mcp.tool()
def defold_set_project(path: str) -> str:
    """Switch to another Defold project directory (the one containing game.project)."""
    p = Path(path).expanduser().resolve()
    if not (p / "game.project").exists():
        return f"No game.project in {p}"
    STATE["project"] = p
    return defold_status()


@mcp.tool()
def defold_api(method: str, path: str, body: str | None = None, timeout: float = 120.0) -> str:
    """Raw call to the editor HTTP API. GET /openapi.json lists every endpoint, e.g.
    POST /command/build, /command/run, /command/hot-reload, /command/compile,
    POST /bob {"options": {...}, "commands": ["build"]}, GET /prefs/{path}.
    body is a JSON string."""
    return _text(*_request(method, path, body, timeout))


@mcp.tool()
def defold_eval(lua_code: str, timeout: float = 120.0) -> str:
    """Run Lua in the editor extension runtime with the full editor API (editor.get,
    editor.transact, editor.tx.*, editor.save, editor.execute, ...). Use `return` to get
    values back (returned as JSON); print() output is included.
    Docs: GET /ref via defold_api, or https://defold.com/ref/editor-lua/"""
    ok, printed, results = _eval(lua_code, timeout)
    out = ["OK" if ok else "ERROR"]
    out += [f"print: {line}" for line in printed if line]
    for r in results:
        out.append(json.dumps(r, indent=1, ensure_ascii=False))
    return "\n".join(out)[:MAX_TEXT]


@mcp.tool()
def defold_console(tail: int = 100, grep: str | None = None) -> str:
    """Last lines of the editor console (build output, engine logs, print from scripts)."""
    st, ct, raw = _request("GET", "/console", timeout=30)
    if st != 200:
        return _text(st, ct, raw)
    lines = json.loads(raw).get("lines", [])
    if grep:
        lines = [l for l in lines if grep.lower() in l.lower()]
    return "\n".join(lines[-tail:]) or "(console empty)"


@mcp.tool()
def defold_preview(path: str, width: int = 800, height: int = 600):
    """Render a scene resource of the project (.collection, .go, .gui, .atlas, ...) as a PNG,
    using the editor's own renderer. path is a project path like /main/main.collection."""
    q = urllib.parse.urlencode({"width": width, "height": height})
    st, ct, raw = _request("GET", "/preview" + urllib.parse.quote(path) + "?" + q, timeout=60)
    if st != 200 or "image" not in ct:
        raise RuntimeError(_text(st, ct, raw))
    return Image(data=raw, format="png")


def _hypr_clients():
    return json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, check=True).stdout)


@mcp.tool()
def defold_screenshot(target: str = "editor"):
    """Screenshot of a window. target: "editor" (Defold editor), "game" (running game window,
    matched by project title), or any text matched against window titles/classes."""
    clients = _hypr_clients()
    title = None
    if target == "game":
        try:
            ok, _, res = _eval('return editor.get("/game.project", "project.title")', 10)
            title = res[0] if ok and res else None
        except RuntimeError:
            pass

    def match(c):
        t, cls = c.get("title", ""), c.get("class", "")
        if target == "editor":
            return ("defold" in cls.lower() or "Defold" in t) and "dmengine" not in cls.lower() and t != title
        if target == "game":
            return (title and t == title) or "dmengine" in cls.lower()
        return target.lower() in t.lower() or target.lower() in cls.lower()

    found = [c for c in clients if match(c) and c.get("mapped", True)]
    if not found:
        names = ", ".join(f"{c.get('class')}:{c.get('title')}" for c in clients)
        raise RuntimeError(f"No window for {target!r}. Windows: {names}")
    c = found[0]
    (x, y), (w, h) = c["at"], c["size"]
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    subprocess.run(["grim", "-g", f"{x},{y} {w}x{h}", path], check=True)
    data = Path(path).read_bytes()
    os.unlink(path)
    return Image(data=data, format="png")


if __name__ == "__main__":
    mcp.run()
