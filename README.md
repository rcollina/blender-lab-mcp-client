# blender-lab-mcp-client

An MCP client that speaks the **official Blender.org "Blender Lab" MCP add-on's**
actual wire protocol, exposing the same 27 tools as
[djeada/blender-mcp-server](https://github.com/djeada/blender-mcp-server) —
but working against `blender.org/lab/mcp-server` instead of that project's
own bundled add-on.

> **Read the [Security model](#security-model) before connecting this to an AI
> assistant.** The add-on executes arbitrary Python with no authentication.

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
unrelated protocols talking past each other.

This package re-implements each of the original 27 MCP tools by generating
the equivalent `bpy` Python and sending it through the Blender Lab add-on's
*actual* protocol, so the tool names, parameters, and behavior stay the same
— only the wire format underneath changes.

## Install

1. Install the official **Blender Lab MCP add-on** in Blender
   (Edit → Preferences → Add-ons → search "MCP", or via the Extensions
   platform) and confirm it's listening on `127.0.0.1:9876`
   (Add-on preferences → Start Server).

2. Get this package (Python ≥ 3.10, [uv](https://docs.astral.sh/uv/) recommended):

   ```bash
   git clone https://github.com/rcollina/blender-lab-mcp-client.git
   cd blender-lab-mcp-client
   uv sync
   ```

3. Point your MCP client at it — see [Client configuration](#client-configuration).

## Configuration

All settings are environment variables, set in your MCP client's server
entry. Invalid values stop the server at startup with a message on stderr.

| Variable | Default | Meaning |
| --- | --- | --- |
| `BLENDER_LAB_MCP_HOST` | `127.0.0.1` | Host of the Blender Lab add-on. Anything other than `localhost` / a loopback IP is refused unless `ALLOW_REMOTE=1`. |
| `BLENDER_LAB_MCP_PORT` | `9876` | Port of the add-on (integer 1–65535). |
| `BLENDER_LAB_MCP_ALLOW_REMOTE` | `0` | Set `1` to allow a non-loopback `HOST`. The add-on has no authentication, so this hands Blender (and the machine) to anyone who can reach that port. |
| `BLENDER_LAB_MCP_DISABLE_PYTHON` | `0` | Set `1` to not register `blender_python_exec` and `blender_python_exec_async`. |
| `BLENDER_LAB_MCP_READONLY` | `0` | Set `1` to register only the read tools: `blender_scene_get_info`, `blender_scene_list_objects`, `blender_object_get_transform`, `blender_object_get_hierarchy`, `blender_material_list`, `blender_job_status`, `blender_job_list`. Implies `DISABLE_PYTHON`. |
| `BLENDER_LAB_MCP_WRITE_DIRS` | — | `os.pathsep`-separated absolute directories (`;` on Windows, `:` elsewhere) where `output_path` / `filepath` of the render and export tools may point. |
| `BLENDER_LAB_MCP_READ_DIRS` | — | Extra directories that are **read-only**: `filepath` of `blender_material_set_texture`, `script_path`, and `blend_file`. Write dirs are implicitly readable. |
| `BLENDER_LAB_MCP_TRANSPORT` | `stdio` | How MCP clients reach *this* server: `stdio` or `streamable-http`. See the warning below. |
| `BLENDER_LAB_MCP_MCP_PORT` | `8765` | Port for `streamable-http` (always bound to `127.0.0.1`; must differ from `PORT`). |
| `BLENDER_BIN` | — | Path to the Blender executable for `transport="headless"`. If unset, `blender` is looked up on `PATH`. |

Disabled tools are never registered, so the client does not see them. The
active mode is logged to stderr at startup. Boolean variables accept
`1/0`, `true/false`, `yes/no`, `on/off`.

### Path restrictions

If neither `WRITE_DIRS` nor `READ_DIRS` is set, paths are passed to Blender
unchanged (and one warning is logged to stderr at startup). If either is set,
every path argument of the structured tools must resolve inside an allowed
directory, or the call is rejected before anything is sent to Blender:

| Argument | Tools | Mode |
| --- | --- | --- |
| `output_path` | `blender_render_still`, `blender_render_animation` | write |
| `filepath` | `blender_export_gltf/obj/fbx` | write |
| `filepath` | `blender_material_set_texture` | read |
| `script_path`, `blend_file` | `blender_python_exec`, `blender_python_exec_async` (also `blend_file` on the render tools) | read |

How paths are checked: symlinks and `..` are followed (`Path.resolve()`);
comparison is case-insensitive on Windows; paths on another drive and UNC /
network paths (`\\server\share`, `\\?\UNC\...`) are rejected; and the *resolved*
path is what is sent to Blender. Blender-relative paths (`//name`) and other
relative paths are resolved against the **first** directory of the list that
applies (the first write dir for writes; the first read dir, else the first
write dir, for reads), not against the `.blend` file's folder. So the default
`output_path` values (`//render.png`, `//render_`) land in the first write
directory. With only `READ_DIRS` set, nothing is writable and the render and
export tools refuse every path.

> **The path restrictions guard the structured tools only.** While
> `blender_python_exec` / `blender_python_exec_async` are enabled, the code you
> run through them can read and write anywhere and bypass these checks. Real
> containment needs `BLENDER_LAB_MCP_DISABLE_PYTHON=1`.

> **`streamable-http` is off by default for a reason.** Over HTTP there is no
> authentication: any process on this machine that can reach
> `http://127.0.0.1:<MCP_PORT>/mcp` can drive Blender through this server.
> The listener is hard-bound to loopback with no setting to change that, and
> the MCP SDK's DNS-rebinding protection is active, but local processes are
> not protected against. Prefer `stdio`.

## Security model

**The Blender Lab add-on executes whatever Python it is sent, with the full
privileges of the Blender process, and has no authentication.** Anything that
can open a TCP connection to its port can read or write your files, start
programs, and use your network. This client does not (and cannot) change that.

What this client does, and does not, protect:

| Protected | Not protected |
| --- | --- |
| Non-loopback add-on hosts are refused unless you opt in. | `blender_python_exec` / `_async` run arbitrary code in Blender: they can do anything the Blender process can. |
| `DISABLE_PYTHON` / `READONLY` remove tools before the client ever sees them. | The add-on's port itself: any other local process can still talk to it directly. |
| With `WRITE_DIRS` / `READ_DIRS` set, the structured tools can only touch files under those directories. | Any path used by code you run through the Python tools: **the allowlist is bypassable while Python exec is enabled.** |
| Structured tools pass parameters as JSON data, never spliced into code. | Prompt injection: an AI that reads untrusted content (web pages, files, scene names) can be steered into calling tools. |
| Tool annotations tell clients which tools are read-only or destructive. | Annotations are hints; clients may ignore them. |

Practical advice:

- **Work on a scratch `.blend`** (a copy), not your only copy of a project.
  Delete, undo/redo, render and export tools change or overwrite things, and
  there is no confirmation step.
- For anything beyond experiments, **run Blender (and this client) inside a VM
  or disposable user account**, so a mistaken or hijacked tool call cannot
  reach your real files and credentials.
- If you don't need scripting, set `BLENDER_LAB_MCP_DISABLE_PYTHON=1` (and
  `BLENDER_LAB_MCP_WRITE_DIRS` to a scratch folder); if you only need
  inspection, `BLENDER_LAB_MCP_READONLY=1`.
- Keep the add-on bound to `127.0.0.1` and don't forward its port.

## Client configuration

Replace `/absolute/path/to/blender-lab-mcp-client` with your clone. Add the
optional environment variables from the table above wherever shown.

### Claude Code

```bash
claude mcp add --transport stdio blender -- \
  uv run --project /absolute/path/to/blender-lab-mcp-client blender-lab-mcp-client

# with options:
claude mcp add --transport stdio --env BLENDER_LAB_MCP_DISABLE_PYTHON=1 blender -- \
  uv run --project /absolute/path/to/blender-lab-mcp-client blender-lab-mcp-client
```

### Codex (`~/.codex/config.toml`)

```toml
[mcp_servers.blender]
command = "uv"
args = ["run", "--project", "/absolute/path/to/blender-lab-mcp-client", "blender-lab-mcp-client"]

[mcp_servers.blender.env]
BLENDER_LAB_MCP_DISABLE_PYTHON = "1"
```

### OpenCode (`opencode.json`)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "blender": {
      "type": "local",
      "command": ["uv", "run", "--project", "/absolute/path/to/blender-lab-mcp-client", "blender-lab-mcp-client"],
      "enabled": true,
      "environment": {
        "BLENDER_LAB_MCP_DISABLE_PYTHON": "1"
      }
    }
  }
}
```

### Generic `mcp.json`-style clients

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
parameters are unchanged; new parameters (if any) are optional.

Behavior worth knowing:

- **`blender_python_exec`** returns
  `{result, stdout, stderr, error, duration_seconds, timed_out, cancelled}` on
  both transports. Output capture is Python-level only and capped at 50 000
  characters.
- **`blender_render_still` / `blender_render_animation`** on the bridge restore
  the scene's render path, engine, resolution and frame range afterwards, even
  if the render fails. (Rendered files are of course still written.)
- Every tool carries MCP annotations (`readOnlyHint`, `destructiveHint`,
  `openWorldHint=false`).

## Known limitations

- **Bridge "async" is synchronous.** `blender_python_exec_async` on the bridge
  runs the script to completion before returning; the returned job is already
  `succeeded`/`failed`, and `blender_job_cancel` cannot cancel it. The add-on's
  real deferred-job mechanism needs the executed code itself to provide a
  `check_is_finished` callable, which can't be synthesized generically. Real
  background execution needs `transport="headless"` (a separate `blender -b`
  process). Bridge job records are in memory only, last 100 kept.
- **`timeout_seconds` on the bridge cannot stop running code.** It is enforced
  client-side: the tool returns a timeout error, but whatever Blender already
  received keeps running, and Blender may stay unresponsive until it finishes.
  On `transport="headless"` the process is killed.
- **No cancellation signal for scripts.** Neither transport provides a
  `__cancel_event__`; cancelling a headless job terminates its process.
- **Path restrictions don't cover Python execution.** See above: with
  `blender_python_exec` enabled they can be bypassed. Also, with restrictions on,
  `//` means "relative to the first allowed directory", which differs from
  Blender's "relative to the .blend file".
- **Captured stdout/stderr is Python-level only.** Output written directly by
  Blender's C code is not included.
- **One request at a time, one connection per request.** The add-on closes its
  socket after every response, so each tool call opens a fresh connection and
  calls are serialized.
- **Headless jobs are in memory and unbounded** (lost on restart; finished
  jobs are not pruned).
- **Headless runs do not touch your open session** — they load `blend_file`
  (or factory startup) in their own process, and nothing is saved back.
- Tested by the upstream author against Blender 5.2 LTS with the Blender Lab
  add-on. The 0.2.0 changes are covered by automated tests against a fake
  add-on only and have not been exercised against a live Blender, so try them
  on a scratch `.blend` first. Helper code uses standard `bpy.ops.*` calls and
  should work on any recent Blender, but hasn't been verified across versions.

## Development

```bash
uv sync
uv run pytest
```

The suite never starts Blender or touches the network beyond loopback: it uses
a fake add-on (a small asyncio TCP server speaking the null-byte protocol) and
a stub `bpy`. Requires `mcp >= 1.23`.

## Credit

Forked from [djeada/blender-mcp-server](https://github.com/djeada/blender-mcp-server)
(MIT licensed) by Adam Djellouli — the MCP tool surface (names, parameters,
descriptions) and the `headless.py` background-execution transport are
carried over from it. `server.py`'s `BlenderConnection` and the
command-to-`bpy`-code translation layer are new, to target the official
Blender Lab add-on's protocol instead of upstream's own bundled add-on.
Also derived from [A-to-PC/blender-lab-mcp-client](https://github.com/A-to-PC/blender-lab-mcp-client).

## License

MIT — see [LICENSE](LICENSE).
