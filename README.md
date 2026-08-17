# blender-lab-mcp-client

An MCP client that speaks the **official Blender.org "Blender Lab" MCP add-on's**
actual wire protocol, exposing the same 27 tools as
[djeada/blender-mcp-server](https://github.com/djeada/blender-mcp-server) —
but working against `blender.org/lab/mcp-server` instead of that project's
own bundled add-on.

## Why this exists

There are (at least) two unrelated "Blender + MCP" projects that happen to
share a name, a default port, and a similar pitch:

1. **`djeada/blender-mcp-server`** — ships a matched pair: its own Blender
   add-on *and* a Python MCP client, talking a newline-delimited
   `{"id", "command", "params"}` / `{"success", "result"}` protocol over
   `localhost:9876`.
2. **The official Blender Lab add-on** (`blender.org/lab/mcp-server`,
   maintainer "Blender Authors") — a completely separate project. It also
   listens on `localhost:9876`, but only ships the *add-on* side. It speaks
   null-byte-delimited `{"type": "execute", "code": ..., "strict_json": ...}`
   requests and `{"status": "ok"|"error", "result": ...}` responses, has no
   built-in named-command dispatcher (it just executes raw Python against
   `bpy`), and closes its TCP connection after every single request.

If you install the official Blender Lab add-on but configure an MCP client
pointed at `djeada/blender-mcp-server` (e.g. via `uvx blender-mcp-server`),
every tool call fails with something like:

```
Extra data: line 1 column 51 (char 50)
```

or, on the next call:

```
Lost connection to Blender: Blender connection closed
```

That's not a flaky connection, a stale process, or a Blender bug — it's two
unrelated protocols talking past each other. `djeada`'s client sends a
newline-terminated request the add-on never recognizes; the add-on times the
client out, sends back a small null-byte-terminated error, and the client's
`readline()` chokes on the stray null byte immediately after the JSON object.

This package re-implements each of the original 27 MCP tools by generating
the equivalent `bpy` Python and sending it through the Blender Lab add-on's
*actual* protocol, so the tool names, parameters, and behavior stay the same
— only the wire format underneath changes.

## Install

1. Install the official **Blender Lab MCP add-on** in Blender
   (Edit → Preferences → Add-ons → search "MCP", or via the Extensions
   platform) and confirm it's listening on `127.0.0.1:9876`
   (Add-on preferences → Start Server).

2. Install this package:

   ```bash
   git clone https://github.com/A-to-PC/blender-lab-mcp-client.git
   cd blender-lab-mcp-client
   pip install -e .
   ```

3. Point your MCP client at it. For a `mcp.json`-style config:

   ```json
   {
     "servers": {
       "Blender": {
         "type": "stdio",
         "command": "blender-lab-mcp-client"
       }
     }
   }
   ```

   Or run directly from source without installing, via `uv`:

   ```json
   {
     "servers": {
       "Blender": {
         "type": "stdio",
         "command": "uv",
         "args": ["run", "--project", "/absolute/path/to/blender-lab-mcp-client", "blender-lab-mcp-client"]
       }
     }
   }
   ```

## Tool reference

Same 27 tools as upstream — see
[djeada/blender-mcp-server's tool reference](https://github.com/djeada/blender-mcp-server#tool-reference)
for the full table (scene inspection, object manipulation, materials,
rendering/export, history, Python execution, async jobs). Names and
parameters are unchanged; only the transport underneath is different.

## Known limitations

- **`blender_python_exec_async` / `blender_job_status` / `blender_job_cancel`
  / `blender_job_list` (bridge transport only) are faked as synchronous.**
  The add-on's real deferred-job mechanism requires the executed code itself
  to set a `check_is_finished` callable, which isn't practical to synthesize
  generically from arbitrary submitted code. Async calls against the *live*
  Blender session actually run synchronously and are immediately reported as
  `"succeeded"`. For genuinely long-running work (physics bakes, heavy sims),
  use `transport="headless"` instead — that path runs a separate
  `blender -b` background process and is unaffected by this limitation.
- **One connection per request.** The add-on closes its socket after every
  response, so this client can't reuse a persistent connection — each tool
  call opens a fresh TCP connection. This matches the add-on's actual
  design; it isn't a performance shortcut that could be "fixed" client-side.
- Tested against Blender 5.2 LTS with the Blender Lab add-on. Object/material
  helper code uses standard `bpy.ops.*` calls and should work on any recent
  Blender version, but hasn't been verified across a version matrix.

## Credit

Forked from [djeada/blender-mcp-server](https://github.com/djeada/blender-mcp-server)
(MIT licensed) by Adam Djellouli — the MCP tool surface (names, parameters,
descriptions) and the `headless.py` background-execution transport are
carried over unchanged. Only `server.py`'s `BlenderConnection` and the
command-to-`bpy`-code translation layer are new, to target the official
Blender Lab add-on's protocol instead of upstream's own bundled add-on.

## License

MIT — see [LICENSE](LICENSE).
