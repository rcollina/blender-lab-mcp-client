"""blender-lab-mcp-client — MCP server that bridges an AI assistant to the
official Blender.org "Blender Lab" MCP add-on (blender.org/lab/mcp-server).

This is a fork of djeada/blender-mcp-server (MIT), whose client and add-on
were built as a matched pair speaking a newline-delimited `{"id", "command",
"params"}` / `{"success", "result"}` protocol. The official Blender Lab
add-on is an unrelated project (also from "Blender Authors") that happens to
use the same default port and a similar name, but speaks a different wire
protocol: null-byte-delimited `{"type": "execute", "code": ..., "strict_json":
...}` requests, `{"status": "ok"|"error", "result": ...}` responses, and no
built-in named-command dispatcher — it only executes raw Python against
`bpy`, and it closes its TCP connection after every single request/response.

Everything below reimplements each of the original project's 27 MCP tools by
generating the equivalent `bpy` code and sending it through the Blender Lab
add-on's actual protocol, so the same tool names/parameters work unchanged
against the official add-on instead of djeada's bundled one.
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from blender_lab_mcp_client.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    MCP_BIND_HOST,
    ConfigError,
    Settings,
    load_settings,
)
from blender_lab_mcp_client.headless import HeadlessBlenderExecutor, HeadlessJobManager
from blender_lab_mcp_client.paths import READ, WRITE, _validate_path

logger = logging.getLogger(__name__)

HEADLESS_JOB_MANAGER = HeadlessJobManager()

# ---------------------------------------------------------------------------
# Command -> bpy code generation.
#
# The add-on's exec namespace starts as `{"result": {}}` with nothing else
# pre-injected, so every snippet must `import bpy` itself and set `result`
# to a JSON-serializable dict (or raise, which the add-on turns into a
# `{"status": "error", "message": <traceback>}` response automatically).
# ---------------------------------------------------------------------------

_CODE_SCENE_GET_INFO = """
scene = bpy.context.scene
result = {
    "name": scene.name,
    "frame_start": scene.frame_start,
    "frame_end": scene.frame_end,
    "frame_current": scene.frame_current,
    "render_engine": scene.render.engine,
    "resolution_x": scene.render.resolution_x,
    "resolution_y": scene.render.resolution_y,
    "object_count": len(scene.objects),
}
"""

_CODE_SCENE_LIST_OBJECTS = """
type_filter = params.get("type")
objs = []
for obj in bpy.context.scene.objects:
    if type_filter and obj.type != type_filter:
        continue
    objs.append({"name": obj.name, "type": obj.type})
result = {"objects": objs}
"""

_CODE_OBJECT_GET_TRANSFORM = """
obj = bpy.data.objects.get(params["name"])
if obj is None:
    raise RuntimeError("Object not found: {}".format(params["name"]))
result = {
    "location": list(obj.location),
    "rotation_euler": list(obj.rotation_euler),
    "scale": list(obj.scale),
}
"""

_CODE_OBJECT_GET_HIERARCHY = """
name = params.get("name")

def _build(obj):
    return {"name": obj.name, "type": obj.type, "children": [_build(c) for c in obj.children]}

if name:
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise RuntimeError("Object not found: {}".format(name))
    result = {"hierarchy": _build(obj)}
else:
    roots = [o for o in bpy.context.scene.objects if o.parent is None]
    result = {"hierarchy": [_build(o) for o in roots]}
"""

_CODE_MATERIAL_LIST = """
result = {"materials": [m.name for m in bpy.data.materials]}
"""

_CODE_OBJECT_CREATE_MESH = """
mesh_type = params.get("type", "cube")
size = params.get("size", 2.0)
name = params.get("name")
location = tuple(params.get("location") or (0.0, 0.0, 0.0))

if mesh_type == "cube":
    bpy.ops.mesh.primitive_cube_add(size=size, location=location)
elif mesh_type == "sphere":
    bpy.ops.mesh.primitive_uv_sphere_add(radius=size / 2, location=location)
elif mesh_type == "cylinder":
    bpy.ops.mesh.primitive_cylinder_add(radius=size / 2, depth=size, location=location)
elif mesh_type == "plane":
    bpy.ops.mesh.primitive_plane_add(size=size, location=location)
elif mesh_type == "cone":
    bpy.ops.mesh.primitive_cone_add(radius1=size / 2, depth=size, location=location)
elif mesh_type == "torus":
    bpy.ops.mesh.primitive_torus_add(major_radius=size / 2, minor_radius=size / 8, location=location)
else:
    raise RuntimeError("Unsupported mesh_type: {}".format(mesh_type))

obj = bpy.context.active_object
if name:
    obj.name = name
result = {"name": obj.name, "type": obj.type}
"""

_CODE_OBJECT_DELETE = """
obj = bpy.data.objects.get(params["name"])
if obj is None:
    raise RuntimeError("Object not found: {}".format(params["name"]))
bpy.data.objects.remove(obj, do_unlink=True)
result = {"deleted": params["name"]}
"""

_CODE_OBJECT_TRANSLATE = """
obj = bpy.data.objects.get(params["name"])
if obj is None:
    raise RuntimeError("Object not found: {}".format(params["name"]))
if params.get("location") is not None:
    obj.location = params["location"]
elif params.get("offset") is not None:
    off = params["offset"]
    obj.location = (
        obj.location[0] + off[0],
        obj.location[1] + off[1],
        obj.location[2] + off[2],
    )
result = {"name": obj.name, "location": list(obj.location)}
"""

_CODE_OBJECT_ROTATE = """
import math
obj = bpy.data.objects.get(params["name"])
if obj is None:
    raise RuntimeError("Object not found: {}".format(params["name"]))
rot = params["rotation"]
if params.get("degrees", True):
    rot = [math.radians(v) for v in rot]
obj.rotation_euler = rot
result = {"name": obj.name, "rotation_euler": list(obj.rotation_euler)}
"""

_CODE_OBJECT_SCALE = """
obj = bpy.data.objects.get(params["name"])
if obj is None:
    raise RuntimeError("Object not found: {}".format(params["name"]))
obj.scale = params["scale"]
result = {"name": obj.name, "scale": list(obj.scale)}
"""

_CODE_OBJECT_DUPLICATE = """
obj = bpy.data.objects.get(params["name"])
if obj is None:
    raise RuntimeError("Object not found: {}".format(params["name"]))
new_obj = obj.copy()
if obj.data is not None:
    new_obj.data = obj.data.copy()
bpy.context.collection.objects.link(new_obj)
if params.get("new_name"):
    new_obj.name = params["new_name"]
result = {"name": new_obj.name}
"""

_CODE_MATERIAL_CREATE = """
name = params.get("name", "Material")
mat = bpy.data.materials.new(name=name)
mat.use_nodes = True
color = params.get("color")
if color:
    principled = mat.node_tree.nodes.get("Principled BSDF")
    if principled:
        principled.inputs["Base Color"].default_value = (color[0], color[1], color[2], 1.0)
result = {"name": mat.name}
"""

_CODE_MATERIAL_ASSIGN = """
obj = bpy.data.objects.get(params["object"])
mat = bpy.data.materials.get(params["material"])
if obj is None:
    raise RuntimeError("Object not found: {}".format(params["object"]))
if mat is None:
    raise RuntimeError("Material not found: {}".format(params["material"]))
if obj.data.materials:
    obj.data.materials[0] = mat
else:
    obj.data.materials.append(mat)
result = {"object": obj.name, "material": mat.name}
"""

_CODE_MATERIAL_SET_COLOR = """
mat = bpy.data.materials.get(params["name"])
if mat is None:
    raise RuntimeError("Material not found: {}".format(params["name"]))
color = params["color"]
if not mat.use_nodes:
    mat.use_nodes = True
principled = mat.node_tree.nodes.get("Principled BSDF")
if principled:
    principled.inputs["Base Color"].default_value = (color[0], color[1], color[2], 1.0)
mat.diffuse_color = (color[0], color[1], color[2], 1.0)
result = {"name": mat.name, "color": color}
"""

_CODE_MATERIAL_SET_TEXTURE = """
mat = bpy.data.materials.get(params["name"])
if mat is None:
    raise RuntimeError("Material not found: {}".format(params["name"]))
if not mat.use_nodes:
    mat.use_nodes = True
nodes = mat.node_tree.nodes
links = mat.node_tree.links
principled = nodes.get("Principled BSDF")
img = bpy.data.images.load(params["filepath"], check_existing=True)
tex_node = nodes.new("ShaderNodeTexImage")
tex_node.image = img
if principled:
    links.new(tex_node.outputs["Color"], principled.inputs["Base Color"])
result = {"name": mat.name, "filepath": params["filepath"]}
"""

# Rendering must not leave the user's open scene changed: every setting we
# touch is saved first and restored in `finally`, even if the render fails.
_CODE_RENDER_STILL = """
scene = bpy.context.scene
_orig = {
    "filepath": scene.render.filepath,
    "engine": scene.render.engine,
    "resolution_x": scene.render.resolution_x,
    "resolution_y": scene.render.resolution_y,
}
try:
    scene.render.filepath = params["output_path"]
    if params.get("resolution_x"):
        scene.render.resolution_x = params["resolution_x"]
    if params.get("resolution_y"):
        scene.render.resolution_y = params["resolution_y"]
    if params.get("engine"):
        scene.render.engine = params["engine"]
    bpy.ops.render.render(write_still=True)
    result = {
        "output_path": scene.render.filepath,
        "engine": scene.render.engine,
        "resolution_x": scene.render.resolution_x,
        "resolution_y": scene.render.resolution_y,
    }
finally:
    scene.render.filepath = _orig["filepath"]
    scene.render.engine = _orig["engine"]
    scene.render.resolution_x = _orig["resolution_x"]
    scene.render.resolution_y = _orig["resolution_y"]
"""

_CODE_RENDER_ANIMATION = """
scene = bpy.context.scene
_orig = {
    "filepath": scene.render.filepath,
    "engine": scene.render.engine,
    "frame_start": scene.frame_start,
    "frame_end": scene.frame_end,
}
try:
    scene.render.filepath = params["output_path"]
    if params.get("frame_start") is not None:
        scene.frame_start = params["frame_start"]
    if params.get("frame_end") is not None:
        scene.frame_end = params["frame_end"]
    if params.get("engine"):
        scene.render.engine = params["engine"]
    bpy.ops.render.render(animation=True)
    result = {
        "output_path": scene.render.filepath,
        "engine": scene.render.engine,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
    }
finally:
    scene.render.filepath = _orig["filepath"]
    scene.render.engine = _orig["engine"]
    scene.frame_start = _orig["frame_start"]
    scene.frame_end = _orig["frame_end"]
"""

_CODE_EXPORT_GLTF = """
bpy.ops.export_scene.gltf(filepath=params["filepath"])
result = {"filepath": params["filepath"]}
"""

_CODE_EXPORT_OBJ = """
bpy.ops.wm.obj_export(filepath=params["filepath"])
result = {"filepath": params["filepath"]}
"""

_CODE_EXPORT_FBX = """
bpy.ops.export_scene.fbx(filepath=params["filepath"])
result = {"filepath": params["filepath"]}
"""

_CODE_HISTORY_UNDO = """
bpy.ops.ed.undo()
result = {"status": "undone"}
"""

_CODE_HISTORY_REDO = """
bpy.ops.ed.redo()
result = {"status": "redone"}
"""

# Result shape matches headless.py: result / stdout / stderr / error /
# duration_seconds / timed_out / cancelled. Output capture is Python-level
# only (redirect_stdout/stderr); C-level writes by Blender are not captured.
_CODE_PYTHON_EXECUTE = """
import io
import time
import traceback
import mathutils
from contextlib import redirect_stdout, redirect_stderr

_LIMIT = 50000

def _cap(text):
    if len(text) <= _LIMIT:
        return text
    return text[:_LIMIT] + "\\n... (truncated, {} total chars)".format(len(text))

def _safe(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)

_user_code = params.get("code")
_script_path = params.get("script_path")
_args = params.get("args") or {}
_out = io.StringIO()
_err = io.StringIO()
_error = None
_ns = {"bpy": bpy, "mathutils": mathutils, "args": _args, "__result__": None}
_t0 = time.monotonic()
try:
    if _user_code and _script_path:
        raise ValueError("Provide either 'code' or 'script_path', not both")
    if not _user_code and not _script_path:
        raise ValueError("Either 'code' or 'script_path' must be provided")
    if _script_path:
        with open(_script_path, "r", encoding="utf-8") as _f:
            _user_code = _f.read()
    with redirect_stdout(_out), redirect_stderr(_err):
        exec(compile(_user_code, "<mcp-bridge-script>", "exec"), _ns)
except (Exception, SystemExit):
    _error = traceback.format_exc().strip()
result = {
    "result": None if _error else _safe(_ns.get("__result__")),
    "stdout": _cap(_out.getvalue()),
    "stderr": _cap(_err.getvalue()),
    "error": _error,
    "duration_seconds": round(time.monotonic() - _t0, 4),
    "timed_out": False,
    "cancelled": False,
}
"""

_COMMAND_CODE: dict[str, str] = {
    "scene.get_info": _CODE_SCENE_GET_INFO,
    "scene.list_objects": _CODE_SCENE_LIST_OBJECTS,
    "object.get_transform": _CODE_OBJECT_GET_TRANSFORM,
    "object.get_hierarchy": _CODE_OBJECT_GET_HIERARCHY,
    "material.list": _CODE_MATERIAL_LIST,
    "object.create_mesh": _CODE_OBJECT_CREATE_MESH,
    "object.delete": _CODE_OBJECT_DELETE,
    "object.translate": _CODE_OBJECT_TRANSLATE,
    "object.rotate": _CODE_OBJECT_ROTATE,
    "object.scale": _CODE_OBJECT_SCALE,
    "object.duplicate": _CODE_OBJECT_DUPLICATE,
    "material.create": _CODE_MATERIAL_CREATE,
    "material.assign": _CODE_MATERIAL_ASSIGN,
    "material.set_color": _CODE_MATERIAL_SET_COLOR,
    "material.set_texture": _CODE_MATERIAL_SET_TEXTURE,
    "render.still": _CODE_RENDER_STILL,
    "render.animation": _CODE_RENDER_ANIMATION,
    "export.gltf": _CODE_EXPORT_GLTF,
    "export.obj": _CODE_EXPORT_OBJ,
    "export.fbx": _CODE_EXPORT_FBX,
    "history.undo": _CODE_HISTORY_UNDO,
    "history.redo": _CODE_HISTORY_REDO,
    "python.execute": _CODE_PYTHON_EXECUTE,
}

# In-memory fake job store for "python.execute_async" / "job.*": the add-on's
# real deferred-job mechanism requires the executed code itself to set a
# `check_is_finished` callable, which isn't practical to synthesize
# generically here. Instead, async jobs run synchronously and are recorded as
# already finished so `blender_job_status` still behaves sensibly for
# callers. Use `transport="headless"` (see below) for genuinely long-running
# work — that path already runs as a real background process.
# Only the most recent _MAX_FAKE_JOBS records are kept.
_MAX_FAKE_JOBS = 100
_FAKE_JOBS: "OrderedDict[str, dict[str, object]]" = OrderedDict()


def _record_fake_job(job: dict[str, object]) -> None:
    _FAKE_JOBS[str(job["job_id"])] = job
    while len(_FAKE_JOBS) > _MAX_FAKE_JOBS:
        _FAKE_JOBS.popitem(last=False)


def _build_header(params: dict) -> str:
    """The snippet prologue: imports plus `params`, passed as a JSON literal.

    json.dumps output is pure ASCII (ensure_ascii), so its repr() is a safe
    Python string literal whatever quotes, backslashes or newlines `params`
    contains; user values are never spliced into code.
    """
    return "import bpy\nimport json\nparams = json.loads({!r})\n".format(json.dumps(params))


def _build_code(command: str, params: dict) -> str:
    body = _COMMAND_CODE.get(command)
    if body is None:
        raise RuntimeError("Unsupported command: {!r}".format(command))
    return _build_header(params) + body


class BlenderConnection:
    """Async TCP client that communicates with the Blender Lab add-on."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, settings: Settings | None = None):
        self.host = host
        self.port = port
        self.settings = settings or Settings(host=host, port=port)
        self._lock = asyncio.Lock()

    async def connect(self):
        # Fail fast if Blender isn't reachable at all on startup. The add-on
        # closes its socket after every single request/response, so this
        # connection is not reused by `_send_execute` below — every call
        # opens its own.
        _reader, writer = await asyncio.open_connection(self.host, self.port)
        writer.close()
        await writer.wait_closed()
        logger.info(f"Connected to Blender at {self.host}:{self.port}")

    async def disconnect(self):
        pass

    async def _send_execute(self, code: str) -> Any:
        """Send a raw `type: execute` request using the add-on's actual
        null-byte-delimited protocol, and return the decoded `result`."""
        request = {"type": "execute", "code": code, "strict_json": True}

        async with self._lock:
            writer = None
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                writer.write(json.dumps(request).encode() + b"\0")
                await writer.drain()

                data = await reader.readuntil(b"\0")
                response = json.loads(data[:-1])
                if response.get("status") != "ok":
                    raise RuntimeError(response.get("message", "Unknown error from Blender"))
                return response.get("result")
            except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
                raise ConnectionError(f"Lost connection to Blender: {e}") from e
            finally:
                if writer is not None:
                    writer.close()

    async def _run_python(self, params: dict) -> dict:
        """Run user Python in Blender, enforcing `timeout_seconds` client-side.

        On timeout we stop *waiting* and close the socket, but Blender keeps
        executing whatever it already received: the add-on offers no way to
        interrupt running code.
        """
        timeout = params.get("timeout_seconds")
        start = time.monotonic()
        try:
            return await asyncio.wait_for(
                self._send_execute(_build_code("python.execute", params)),
                timeout=(timeout if timeout and timeout > 0 else None),
            )
        except asyncio.TimeoutError:
            return {
                "result": None,
                "stdout": "",
                "stderr": "",
                "error": (
                    f"Timed out after {timeout}s waiting for Blender. The code was NOT stopped: "
                    "it may still be running inside Blender, and Blender may stay busy until it finishes."
                ),
                "duration_seconds": round(time.monotonic() - start, 4),
                "timed_out": True,
                "cancelled": False,
            }

    async def send_command(self, command: str, params: dict | None = None) -> Any:
        """Send a command to Blender and return the result."""
        params = params or {}

        if command == "python.execute":
            return await self._run_python(params)

        if command == "python.execute_async":
            exec_result = await self._run_python(params)
            job_id = "bridge-sync-" + str(uuid.uuid4())
            failed = bool(exec_result.get("error"))
            _record_fake_job(
                {
                    "job_id": job_id,
                    "status": "failed" if failed else "succeeded",
                    "result": exec_result,
                }
            )
            return {"job_id": job_id}

        if command == "job.status":
            job = _FAKE_JOBS.get(params.get("job_id"))
            if job is None:
                raise RuntimeError("Unknown job_id: {}".format(params.get("job_id")))
            return job

        if command == "job.cancel":
            job_id = params.get("job_id")
            job = _FAKE_JOBS.get(job_id)
            if job is None:
                raise RuntimeError("Unknown job_id: {}".format(job_id))
            # Bridge "jobs" have always finished by the time they are recorded,
            # so there is nothing to cancel; don't rewrite their real outcome.
            return {"job_id": job_id, "status": job["status"]}

        if command == "job.list":
            return {"jobs": list(_FAKE_JOBS.values())}

        return await self._send_execute(_build_code(command, params))


def _get_conn(ctx: Context) -> BlenderConnection:
    return ctx.request_context.lifespan_context  # type: ignore[no-any-return]


def _safe_path(ctx: Context, value: str, mode: str, param: str) -> str:
    return _validate_path(value, _get_conn(ctx).settings, mode=mode, param=param)


def _safe_optional_path(ctx: Context, value: str | None, mode: str, param: str) -> str | None:
    return None if value is None else _safe_path(ctx, value, mode, param)


def _check_transport(transport: str) -> None:
    # A typo must not silently fall through to the live Blender session.
    if transport not in ("bridge", "headless"):
        raise ValueError(f"Unknown transport {transport!r}: expected 'bridge' or 'headless'")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    description: str
    # "read" (never changes anything), "python" (arbitrary code execution) or
    # "write" (every other tool).
    category: str = "write"
    # Hard to undo or with effects outside the scene (deletion, undo/redo,
    # writing files, running arbitrary code, killing a job).
    destructive: bool = False

    @property
    def annotations(self) -> ToolAnnotations:
        read = self.category == "read"
        return ToolAnnotations(
            readOnlyHint=read,
            destructiveHint=False if read else self.destructive,
            openWorldHint=False,
        )


# Filled by the `_tool` decorator below; `build_server` registers from it so a
# server instance only ever exposes the tools its settings allow.
_TOOLS: list[ToolSpec] = []


def _tool(*, name: str, description: str, category: str = "write", destructive: bool = False):
    def decorator(fn):
        _TOOLS.append(
            ToolSpec(name=name, fn=fn, description=description, category=category, destructive=destructive)
        )
        return fn

    return decorator


def _is_enabled(spec: ToolSpec, settings: Settings) -> bool:
    if settings.readonly:
        return spec.category == "read"
    if settings.disable_python:
        return spec.category != "python"
    return True


# -- Scene tools --


@_tool(
    name="blender_scene_get_info",
    category="read",
    description="Get information about the current Blender scene including name, frame range, render engine, resolution, and object count.",
)
async def scene_get_info(ctx: Context) -> str:
    result = await _get_conn(ctx).send_command("scene.get_info")
    return json.dumps(result, indent=2)


@_tool(
    name="blender_scene_list_objects",
    category="read",
    description="List all objects in the current Blender scene. Optionally filter by type (MESH, CAMERA, LIGHT, EMPTY, CURVE, etc.).",
)
async def scene_list_objects(ctx: Context, type: str | None = None) -> str:
    params = {}
    if type:
        params["type"] = type
    result = await _get_conn(ctx).send_command("scene.list_objects", params)
    return json.dumps(result, indent=2)


@_tool(
    name="blender_object_get_transform",
    category="read",
    description="Get the position, rotation, and scale of a Blender object by name.",
)
async def object_get_transform(ctx: Context, name: str) -> str:
    result = await _get_conn(ctx).send_command("object.get_transform", {"name": name})
    return json.dumps(result, indent=2)


@_tool(
    name="blender_object_get_hierarchy",
    category="read",
    description="Get the parent/child hierarchy of objects. If name is provided, returns the subtree for that object. Otherwise returns the full scene hierarchy.",
)
async def object_get_hierarchy(ctx: Context, name: str | None = None) -> str:
    params = {}
    if name:
        params["name"] = name
    result = await _get_conn(ctx).send_command("object.get_hierarchy", params)
    return json.dumps(result, indent=2)


@_tool(
    name="blender_material_list",
    category="read",
    description="List all materials in the Blender file.",
)
async def material_list(ctx: Context) -> str:
    result = await _get_conn(ctx).send_command("material.list")
    return json.dumps(result, indent=2)


# -- Object mutation tools --


@_tool(
    name="blender_object_create",
    description="Create a new mesh object in Blender. Supported types: cube, sphere, cylinder, plane, cone, torus.",
)
async def object_create(
    ctx: Context,
    mesh_type: str = "cube",
    name: str | None = None,
    location: list[float] | None = None,
    size: float = 2.0,
) -> str:
    params: dict[str, Any] = {"type": mesh_type, "size": size}
    if name:
        params["name"] = name
    if location:
        params["location"] = location
    result = await _get_conn(ctx).send_command("object.create_mesh", params)
    return json.dumps(result, indent=2)


@_tool(
    name="blender_object_delete",
    destructive=True,
    description="Delete an object from the Blender scene by name.",
)
async def object_delete(ctx: Context, name: str) -> str:
    result = await _get_conn(ctx).send_command("object.delete", {"name": name})
    return json.dumps(result, indent=2)


@_tool(
    name="blender_object_translate",
    description="Move an object. Provide either 'location' for absolute positioning or 'offset' for relative movement.",
)
async def object_translate(
    ctx: Context,
    name: str,
    location: list[float] | None = None,
    offset: list[float] | None = None,
) -> str:
    params: dict[str, Any] = {"name": name}
    if location:
        params["location"] = location
    if offset:
        params["offset"] = offset
    result = await _get_conn(ctx).send_command("object.translate", params)
    return json.dumps(result, indent=2)


@_tool(
    name="blender_object_rotate",
    description="Set the rotation of an object. Provide rotation as [x, y, z] angles. By default angles are in degrees.",
)
async def object_rotate(
    ctx: Context,
    name: str,
    rotation: list[float],
    degrees: bool = True,
) -> str:
    result = await _get_conn(ctx).send_command(
        "object.rotate", {"name": name, "rotation": rotation, "degrees": degrees}
    )
    return json.dumps(result, indent=2)


@_tool(
    name="blender_object_scale",
    description="Set the scale of an object. Provide scale as [x, y, z].",
)
async def object_scale(ctx: Context, name: str, scale: list[float]) -> str:
    result = await _get_conn(ctx).send_command("object.scale", {"name": name, "scale": scale})
    return json.dumps(result, indent=2)


@_tool(
    name="blender_object_duplicate",
    description="Duplicate an object in the Blender scene. Optionally provide a new name.",
)
async def object_duplicate(ctx: Context, name: str, new_name: str | None = None) -> str:
    params: dict[str, Any] = {"name": name}
    if new_name:
        params["new_name"] = new_name
    result = await _get_conn(ctx).send_command("object.duplicate", params)
    return json.dumps(result, indent=2)


# -- Material tools --


@_tool(
    name="blender_material_create",
    description="Create a new material. Optionally set an initial base color as [r, g, b] with values 0-1.",
)
async def material_create(ctx: Context, name: str = "Material", color: list[float] | None = None) -> str:
    params: dict[str, Any] = {"name": name}
    if color:
        params["color"] = color
    result = await _get_conn(ctx).send_command("material.create", params)
    return json.dumps(result, indent=2)


@_tool(
    name="blender_material_assign",
    description="Assign an existing material to an object.",
)
async def material_assign(ctx: Context, object: str, material: str) -> str:
    result = await _get_conn(ctx).send_command("material.assign", {"object": object, "material": material})
    return json.dumps(result, indent=2)


@_tool(
    name="blender_material_set_color",
    description="Set the base color of a material. Color is [r, g, b] with values 0-1.",
)
async def material_set_color(ctx: Context, name: str, color: list[float]) -> str:
    result = await _get_conn(ctx).send_command("material.set_color", {"name": name, "color": color})
    return json.dumps(result, indent=2)


@_tool(
    name="blender_material_set_texture",
    description="Set an image texture as the base color of a material. Provide the file path to the image.",
)
async def material_set_texture(ctx: Context, name: str, filepath: str) -> str:
    filepath = _safe_path(ctx, filepath, READ, "filepath")
    result = await _get_conn(ctx).send_command("material.set_texture", {"name": name, "filepath": filepath})
    return json.dumps(result, indent=2)


# -- Render tools --


@_tool(
    name="blender_render_still",
    destructive=True,
    description=(
        "Render the current scene as a still image. Optionally set output path, resolution, and render engine "
        "(BLENDER_EEVEE, CYCLES, etc.). Use transport='bridge' for the live Blender add-on session, or "
        "transport='headless' with blend_file='/path/to/file.blend' to render in a separate background Blender process."
    ),
)
async def render_still(
    ctx: Context,
    output_path: str = "//render.png",
    resolution_x: int | None = None,
    resolution_y: int | None = None,
    engine: str | None = None,
    transport: str = "bridge",
    blend_file: str | None = None,
    factory_startup: bool | None = None,
) -> str:
    _check_transport(transport)
    output_path = _safe_path(ctx, output_path, WRITE, "output_path")
    blend_file = _safe_optional_path(ctx, blend_file, READ, "blend_file")
    if transport == "headless":
        code = """
import bpy
scene = bpy.context.scene
scene.render.filepath = args["output_path"]
if args.get("resolution_x") is not None:
    scene.render.resolution_x = args["resolution_x"]
if args.get("resolution_y") is not None:
    scene.render.resolution_y = args["resolution_y"]
if args.get("engine"):
    scene.render.engine = args["engine"]
bpy.ops.render.render(write_still=True)
__result__ = {
    "output_path": scene.render.filepath,
    "engine": scene.render.engine,
    "resolution_x": scene.render.resolution_x,
    "resolution_y": scene.render.resolution_y,
}
"""
        executor = HeadlessBlenderExecutor(settings=_get_conn(ctx).settings)
        result = await executor.execute(
            code=code,
            args={
                "output_path": output_path,
                "resolution_x": resolution_x,
                "resolution_y": resolution_y,
                "engine": engine,
            },
            blend_file=blend_file,
            factory_startup=factory_startup,
        )
    else:
        params: dict[str, Any] = {"output_path": output_path}
        if resolution_x:
            params["resolution_x"] = resolution_x
        if resolution_y:
            params["resolution_y"] = resolution_y
        if engine:
            params["engine"] = engine
        result = await _get_conn(ctx).send_command("render.still", params)
    return json.dumps(result, indent=2)


@_tool(
    name="blender_render_animation",
    destructive=True,
    description=(
        "Render an animation. Optionally set output path, frame range, and render engine. "
        "Use transport='bridge' for the live Blender add-on session, or transport='headless' with a blend_file "
        "to render in a separate background Blender process."
    ),
)
async def render_animation(
    ctx: Context,
    output_path: str = "//render_",
    frame_start: int | None = None,
    frame_end: int | None = None,
    engine: str | None = None,
    transport: str = "bridge",
    blend_file: str | None = None,
    factory_startup: bool | None = None,
) -> str:
    _check_transport(transport)
    output_path = _safe_path(ctx, output_path, WRITE, "output_path")
    blend_file = _safe_optional_path(ctx, blend_file, READ, "blend_file")
    if transport == "headless":
        code = """
import bpy
scene = bpy.context.scene
scene.render.filepath = args["output_path"]
if args.get("frame_start") is not None:
    scene.frame_start = args["frame_start"]
if args.get("frame_end") is not None:
    scene.frame_end = args["frame_end"]
if args.get("engine"):
    scene.render.engine = args["engine"]
bpy.ops.render.render(animation=True)
__result__ = {
    "output_path": scene.render.filepath,
    "engine": scene.render.engine,
    "frame_start": scene.frame_start,
    "frame_end": scene.frame_end,
}
"""
        executor = HeadlessBlenderExecutor(settings=_get_conn(ctx).settings)
        result = await executor.execute(
            code=code,
            args={
                "output_path": output_path,
                "frame_start": frame_start,
                "frame_end": frame_end,
                "engine": engine,
            },
            blend_file=blend_file,
            factory_startup=factory_startup,
        )
    else:
        params: dict[str, Any] = {"output_path": output_path}
        if frame_start is not None:
            params["frame_start"] = frame_start
        if frame_end is not None:
            params["frame_end"] = frame_end
        if engine:
            params["engine"] = engine
        result = await _get_conn(ctx).send_command("render.animation", params)
    return json.dumps(result, indent=2)


# -- Export tools --


@_tool(
    name="blender_export_gltf",
    destructive=True,
    description="Export the scene as glTF/GLB. Provide the output file path.",
)
async def export_gltf(ctx: Context, filepath: str) -> str:
    filepath = _safe_path(ctx, filepath, WRITE, "filepath")
    result = await _get_conn(ctx).send_command("export.gltf", {"filepath": filepath})
    return json.dumps(result, indent=2)


@_tool(
    name="blender_export_obj",
    destructive=True,
    description="Export the scene as OBJ. Provide the output file path.",
)
async def export_obj(ctx: Context, filepath: str) -> str:
    filepath = _safe_path(ctx, filepath, WRITE, "filepath")
    result = await _get_conn(ctx).send_command("export.obj", {"filepath": filepath})
    return json.dumps(result, indent=2)


@_tool(
    name="blender_export_fbx",
    destructive=True,
    description="Export the scene as FBX. Provide the output file path.",
)
async def export_fbx(ctx: Context, filepath: str) -> str:
    filepath = _safe_path(ctx, filepath, WRITE, "filepath")
    result = await _get_conn(ctx).send_command("export.fbx", {"filepath": filepath})
    return json.dumps(result, indent=2)


# -- History tools --


@_tool(
    name="blender_history_undo",
    destructive=True,
    description="Undo the last operation in Blender.",
)
async def history_undo(ctx: Context) -> str:
    result = await _get_conn(ctx).send_command("history.undo")
    return json.dumps(result, indent=2)


@_tool(
    name="blender_history_redo",
    destructive=True,
    description="Redo the last undone operation in Blender.",
)
async def history_redo(ctx: Context) -> str:
    result = await _get_conn(ctx).send_command("history.redo")
    return json.dumps(result, indent=2)


# -- Python execution tools --


@_tool(
    name="blender_python_exec",
    category="python",
    destructive=True,
    description=(
        "Execute a Python script in Blender's context synchronously. "
        "Provide either 'code' (inline Python string) or 'script_path' (path to a .py file), not both. "
        "The script has access to 'bpy', 'mathutils', and an 'args' dict with your supplied arguments. "
        "Set '__result__' in the script to return a JSON-serializable value. "
        "Returns {result, stdout, stderr, error, duration_seconds, timed_out, cancelled}; stdout/stderr capture "
        "Python-level output only and are capped at 50000 characters. "
        "Use transport='bridge' for the live Blender add-on session (blocks Blender until the code finishes), or "
        "transport='headless' to run the script in a separate `blender -b` process. "
        "timeout_seconds: with transport='headless' the process is killed on timeout. With transport='bridge' it is "
        "enforced client-side only: this tool returns a timeout error, but code already running inside Blender is "
        "NOT stopped and Blender may stay busy until it finishes. "
        "SECURITY: this code runs with the full privileges of the Blender process. The path restrictions "
        "(BLENDER_LAB_MCP_WRITE_DIRS / READ_DIRS) guard only the structured tools; code executed here can read "
        "and write anywhere and bypass them. Real containment requires BLENDER_LAB_MCP_DISABLE_PYTHON=1."
    ),
)
async def python_exec(
    ctx: Context,
    code: str | None = None,
    script_path: str | None = None,
    args: dict | None = None,
    timeout_seconds: int | None = None,
    transport: str = "bridge",
    blend_file: str | None = None,
    factory_startup: bool | None = None,
) -> str:
    _check_transport(transport)
    script_path = _safe_optional_path(ctx, script_path, READ, "script_path")
    blend_file = _safe_optional_path(ctx, blend_file, READ, "blend_file")
    if transport == "headless":
        executor = HeadlessBlenderExecutor(settings=_get_conn(ctx).settings)
        result = await executor.execute(
            code=code,
            script_path=script_path,
            args=args,
            timeout_seconds=timeout_seconds,
            blend_file=blend_file,
            factory_startup=factory_startup,
        )
    else:
        params: dict[str, Any] = {}
        if code is not None:
            params["code"] = code
        if script_path is not None:
            params["script_path"] = script_path
        if args is not None:
            params["args"] = args
        if timeout_seconds is not None:
            params["timeout_seconds"] = timeout_seconds
        result = await _get_conn(ctx).send_command("python.execute", params)
    return json.dumps(result, indent=2)


@_tool(
    name="blender_python_exec_async",
    category="python",
    destructive=True,
    description=(
        "Start a Python script as a job. Same parameters as blender_python_exec. Returns a job_id. "
        "IMPORTANT: with transport='bridge' (the default) this is NOT asynchronous: the script runs synchronously "
        "inside the live Blender session, this call returns only after it has finished, and the job is already "
        "complete (succeeded/failed) when you poll it. Real background execution requires transport='headless', "
        "which runs the job in a separate `blender -b` process and returns immediately; poll it with "
        "blender_job_status. blender_job_cancel terminates a headless job's process (scripts get no cancellation "
        "signal). Headless suits fluid baking, rigid body simulation, or heavy scene generation. "
        "SECURITY: the script runs with full privileges and can bypass the path restrictions "
        "(BLENDER_LAB_MCP_WRITE_DIRS / READ_DIRS), which guard only the structured tools; real containment "
        "requires BLENDER_LAB_MCP_DISABLE_PYTHON=1."
    ),
)
async def python_exec_async(
    ctx: Context,
    code: str | None = None,
    script_path: str | None = None,
    args: dict | None = None,
    timeout_seconds: int | None = None,
    transport: str = "bridge",
    blend_file: str | None = None,
    factory_startup: bool | None = None,
) -> str:
    _check_transport(transport)
    script_path = _safe_optional_path(ctx, script_path, READ, "script_path")
    blend_file = _safe_optional_path(ctx, blend_file, READ, "blend_file")
    if transport == "headless":
        executor = HeadlessBlenderExecutor(settings=_get_conn(ctx).settings)
        job_id = await HEADLESS_JOB_MANAGER.create_job(
            executor,
            code=code,
            script_path=script_path,
            args=args,
            timeout_seconds=timeout_seconds,
            blend_file=blend_file,
            factory_startup=factory_startup,
        )
        result = {"job_id": job_id}
    else:
        params: dict[str, Any] = {}
        if code is not None:
            params["code"] = code
        if script_path is not None:
            params["script_path"] = script_path
        if args is not None:
            params["args"] = args
        if timeout_seconds is not None:
            params["timeout_seconds"] = timeout_seconds
        result = await _get_conn(ctx).send_command("python.execute_async", params)
    return json.dumps(result, indent=2)


@_tool(
    name="blender_job_status",
    category="read",
    description=(
        "Get the status of an async Blender job. Returns job_id, status "
        "(queued/running/succeeded/failed/cancelled), timestamps, result, stdout, stderr, and error. "
        "Poll this after starting a job with blender_python_exec_async."
    ),
)
async def job_status(ctx: Context, job_id: str) -> str:
    if job_id.startswith("headless-job-"):
        result = HEADLESS_JOB_MANAGER.get_status(job_id)
    else:
        result = await _get_conn(ctx).send_command("job.status", {"job_id": job_id})
    return json.dumps(result, indent=2)


@_tool(
    name="blender_job_cancel",
    destructive=True,
    description=(
        "Cancel a running or queued async Blender job. "
        "For headless jobs the background Blender process is terminated; scripts get no chance to clean up. "
        "Bridge jobs run synchronously and have already finished, so cancelling them changes nothing."
    ),
)
async def job_cancel(ctx: Context, job_id: str) -> str:
    if job_id.startswith("headless-job-"):
        result = await HEADLESS_JOB_MANAGER.cancel(job_id)
    else:
        result = await _get_conn(ctx).send_command("job.cancel", {"job_id": job_id})
    return json.dumps(result, indent=2)


@_tool(
    name="blender_job_list",
    category="read",
    description="List known async Blender jobs with their IDs, statuses, and creation timestamps.",
)
async def job_list(ctx: Context) -> str:
    headless_jobs = HEADLESS_JOB_MANAGER.list_jobs()["jobs"]
    try:
        bridge_result = await _get_conn(ctx).send_command("job.list")
        bridge_jobs = bridge_result.get("jobs", [])
    except (ConnectionError, RuntimeError):
        bridge_jobs = []
    result = {"jobs": bridge_jobs + headless_jobs}
    return json.dumps(result, indent=2)


def build_server(settings: Settings | None = None) -> FastMCP:
    """Create a FastMCP server whose tools and connection follow `settings`."""
    settings = settings or load_settings()

    @asynccontextmanager
    async def blender_lifespan(server: FastMCP):
        """Manage the Blender connection lifecycle."""
        conn = BlenderConnection(settings.host, settings.port, settings=settings)
        try:
            await conn.connect()
        except OSError:
            logger.warning("Could not connect to Blender on startup. Will retry on first tool call.")
        yield conn
        await conn.disconnect()

    # host/port only matter for streamable-http; the host is never configurable.
    # FastMCP turns on DNS-rebinding protection for loopback hosts by itself.
    server = FastMCP(
        "Blender Lab MCP Client",
        lifespan=blender_lifespan,
        log_level="INFO",
        host=MCP_BIND_HOST,
        port=settings.mcp_port,
    )
    for spec in _TOOLS:
        if _is_enabled(spec, settings):
            server.add_tool(
                spec.fn, name=spec.name, description=spec.description, annotations=spec.annotations
            )
    return server


def _log_startup(settings: Settings) -> None:
    logger.info("Using Blender add-on at %s:%s; mode: %s", settings.host, settings.port, settings.mode)
    if not settings.paths_restricted:
        logger.warning(
            "Path arguments are NOT restricted: set BLENDER_LAB_MCP_WRITE_DIRS (and optionally "
            "BLENDER_LAB_MCP_READ_DIRS) to confine render/export/texture/script/.blend paths."
        )
        return
    logger.info("Path restrictions: write=%s read=%s", list(settings.write_dirs), list(settings.read_dirs))
    for d in (*settings.write_dirs, *settings.read_dirs):
        if not os.path.isdir(d):
            logger.warning("Configured directory does not exist: %s", d)
    if settings.python_enabled:
        logger.warning(
            "Python execution is enabled: code run through blender_python_exec can bypass the path "
            "restrictions. Set BLENDER_LAB_MCP_DISABLE_PYTHON=1 for real containment."
        )


def main():
    # stdout carries the MCP protocol: everything we log goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"blender-lab-mcp-client: configuration error: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    server = build_server(settings)
    _log_startup(settings)
    if settings.transport == "streamable-http":
        logger.warning(
            "Serving MCP over HTTP on http://%s:%s/mcp with NO authentication: any local process can "
            "drive Blender through it.",
            MCP_BIND_HOST,
            settings.mcp_port,
        )
    server.run(transport=settings.transport)


if __name__ == "__main__":
    main()
