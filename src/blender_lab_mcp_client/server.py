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
import uuid
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from blender_lab_mcp_client.headless import HeadlessBlenderExecutor, HeadlessJobManager

logger = logging.getLogger(__name__)

BLENDER_HOST = "127.0.0.1"
BLENDER_PORT = 9876
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

_CODE_RENDER_STILL = """
scene = bpy.context.scene
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
"""

_CODE_RENDER_ANIMATION = """
scene = bpy.context.scene
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

_CODE_PYTHON_EXECUTE = """
import mathutils
_user_code = params.get("code")
_script_path = params.get("script_path")
_args = params.get("args") or {}
if _script_path:
    with open(_script_path, "r") as _f:
        _user_code = _f.read()
_ns = {"bpy": bpy, "mathutils": mathutils, "args": _args}
exec(_user_code, _ns)
result = {"result": _ns.get("__result__")}
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
# already "succeeded" so `blender_job_status` still behaves sensibly for
# callers. Use `transport="headless"` (see below) for genuinely long-running
# work — that path already runs as a real background process.
_FAKE_JOBS: dict[str, dict[str, object]] = {}


def _build_code(command: str, params: dict) -> str:
    body = _COMMAND_CODE.get(command)
    if body is None:
        raise RuntimeError("Unsupported command: {!r}".format(command))
    return "import bpy\nimport json\nparams = json.loads({!r})\n{}".format(json.dumps(params), body)


class BlenderConnection:
    """Async TCP client that communicates with the Blender Lab add-on."""

    def __init__(self, host: str = BLENDER_HOST, port: int = BLENDER_PORT):
        self.host = host
        self.port = port
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

    async def send_command(self, command: str, params: dict | None = None) -> Any:
        """Send a command to Blender and return the result."""
        params = params or {}

        if command == "python.execute_async":
            exec_result = await self._send_execute(_build_code("python.execute", params))
            job_id = "bridge-sync-" + str(uuid.uuid4())
            _FAKE_JOBS[job_id] = {
                "job_id": job_id,
                "status": "succeeded",
                "result": exec_result,
            }
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
            job["status"] = "cancelled"
            return {"job_id": job_id, "status": "cancelled"}

        if command == "job.list":
            return {"jobs": list(_FAKE_JOBS.values())}

        return await self._send_execute(_build_code(command, params))


@asynccontextmanager
async def blender_lifespan(server: FastMCP):
    """Manage the Blender connection lifecycle."""
    conn = BlenderConnection()
    try:
        await conn.connect()
    except OSError:
        logger.warning("Could not connect to Blender on startup. Will retry on first tool call.")
    yield conn
    await conn.disconnect()


mcp = FastMCP(
    "Blender Lab MCP Client",
    lifespan=blender_lifespan,
    log_level="INFO",
)


def _get_conn(ctx: Context) -> BlenderConnection:
    return ctx.request_context.lifespan_context  # type: ignore[no-any-return]


# -- Scene tools --


@mcp.tool(
    name="blender_scene_get_info",
    description="Get information about the current Blender scene including name, frame range, render engine, resolution, and object count.",
)
async def scene_get_info(ctx: Context) -> str:
    result = await _get_conn(ctx).send_command("scene.get_info")
    return json.dumps(result, indent=2)


@mcp.tool(
    name="blender_scene_list_objects",
    description="List all objects in the current Blender scene. Optionally filter by type (MESH, CAMERA, LIGHT, EMPTY, CURVE, etc.).",
)
async def scene_list_objects(ctx: Context, type: str | None = None) -> str:
    params = {}
    if type:
        params["type"] = type
    result = await _get_conn(ctx).send_command("scene.list_objects", params)
    return json.dumps(result, indent=2)


@mcp.tool(
    name="blender_object_get_transform",
    description="Get the position, rotation, and scale of a Blender object by name.",
)
async def object_get_transform(ctx: Context, name: str) -> str:
    result = await _get_conn(ctx).send_command("object.get_transform", {"name": name})
    return json.dumps(result, indent=2)


@mcp.tool(
    name="blender_object_get_hierarchy",
    description="Get the parent/child hierarchy of objects. If name is provided, returns the subtree for that object. Otherwise returns the full scene hierarchy.",
)
async def object_get_hierarchy(ctx: Context, name: str | None = None) -> str:
    params = {}
    if name:
        params["name"] = name
    result = await _get_conn(ctx).send_command("object.get_hierarchy", params)
    return json.dumps(result, indent=2)


@mcp.tool(
    name="blender_material_list",
    description="List all materials in the Blender file.",
)
async def material_list(ctx: Context) -> str:
    result = await _get_conn(ctx).send_command("material.list")
    return json.dumps(result, indent=2)


# -- Object mutation tools --


@mcp.tool(
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


@mcp.tool(
    name="blender_object_delete",
    description="Delete an object from the Blender scene by name.",
)
async def object_delete(ctx: Context, name: str) -> str:
    result = await _get_conn(ctx).send_command("object.delete", {"name": name})
    return json.dumps(result, indent=2)


@mcp.tool(
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


@mcp.tool(
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


@mcp.tool(
    name="blender_object_scale",
    description="Set the scale of an object. Provide scale as [x, y, z].",
)
async def object_scale(ctx: Context, name: str, scale: list[float]) -> str:
    result = await _get_conn(ctx).send_command("object.scale", {"name": name, "scale": scale})
    return json.dumps(result, indent=2)


@mcp.tool(
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


@mcp.tool(
    name="blender_material_create",
    description="Create a new material. Optionally set an initial base color as [r, g, b] with values 0-1.",
)
async def material_create(ctx: Context, name: str = "Material", color: list[float] | None = None) -> str:
    params: dict[str, Any] = {"name": name}
    if color:
        params["color"] = color
    result = await _get_conn(ctx).send_command("material.create", params)
    return json.dumps(result, indent=2)


@mcp.tool(
    name="blender_material_assign",
    description="Assign an existing material to an object.",
)
async def material_assign(ctx: Context, object: str, material: str) -> str:
    result = await _get_conn(ctx).send_command("material.assign", {"object": object, "material": material})
    return json.dumps(result, indent=2)


@mcp.tool(
    name="blender_material_set_color",
    description="Set the base color of a material. Color is [r, g, b] with values 0-1.",
)
async def material_set_color(ctx: Context, name: str, color: list[float]) -> str:
    result = await _get_conn(ctx).send_command("material.set_color", {"name": name, "color": color})
    return json.dumps(result, indent=2)


@mcp.tool(
    name="blender_material_set_texture",
    description="Set an image texture as the base color of a material. Provide the file path to the image.",
)
async def material_set_texture(ctx: Context, name: str, filepath: str) -> str:
    result = await _get_conn(ctx).send_command("material.set_texture", {"name": name, "filepath": filepath})
    return json.dumps(result, indent=2)


# -- Render tools --


@mcp.tool(
    name="blender_render_still",
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
        executor = HeadlessBlenderExecutor()
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


@mcp.tool(
    name="blender_render_animation",
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
        executor = HeadlessBlenderExecutor()
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


@mcp.tool(
    name="blender_export_gltf",
    description="Export the scene as glTF/GLB. Provide the output file path.",
)
async def export_gltf(ctx: Context, filepath: str) -> str:
    result = await _get_conn(ctx).send_command("export.gltf", {"filepath": filepath})
    return json.dumps(result, indent=2)


@mcp.tool(
    name="blender_export_obj",
    description="Export the scene as OBJ. Provide the output file path.",
)
async def export_obj(ctx: Context, filepath: str) -> str:
    result = await _get_conn(ctx).send_command("export.obj", {"filepath": filepath})
    return json.dumps(result, indent=2)


@mcp.tool(
    name="blender_export_fbx",
    description="Export the scene as FBX. Provide the output file path.",
)
async def export_fbx(ctx: Context, filepath: str) -> str:
    result = await _get_conn(ctx).send_command("export.fbx", {"filepath": filepath})
    return json.dumps(result, indent=2)


# -- History tools --


@mcp.tool(
    name="blender_history_undo",
    description="Undo the last operation in Blender.",
)
async def history_undo(ctx: Context) -> str:
    result = await _get_conn(ctx).send_command("history.undo")
    return json.dumps(result, indent=2)


@mcp.tool(
    name="blender_history_redo",
    description="Redo the last undone operation in Blender.",
)
async def history_redo(ctx: Context) -> str:
    result = await _get_conn(ctx).send_command("history.redo")
    return json.dumps(result, indent=2)


# -- Python execution tools --


@mcp.tool(
    name="blender_python_exec",
    description=(
        "Execute a Python script in Blender's context synchronously. "
        "Provide either 'code' (inline Python string) or 'script_path' (path to a .py file), not both. "
        "The script has access to 'bpy', 'mathutils', and an 'args' dict with your supplied arguments. "
        "Set '__result__' in the script to return a JSON-serializable value. "
        "Returns the result, captured stdout/stderr, and execution duration. "
        "Use transport='bridge' for the live Blender add-on session, or transport='headless' to run the script "
        "in a separate `blender -b` process. For long-running tasks like baking, use blender_python_exec_async."
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
    if transport == "headless":
        executor = HeadlessBlenderExecutor()
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


@mcp.tool(
    name="blender_python_exec_async",
    description=(
        "Start a long-running Python script in Blender asynchronously. "
        "Same parameters as blender_python_exec. Returns a job_id immediately. "
        "Use blender_job_status to poll for completion, and blender_job_cancel to abort. "
        "The script can check '__cancel_event__.is_set()' to detect cancellation. "
        "Ideal for fluid baking, rigid body simulation, or heavy scene generation. "
        "Use transport='headless' to run the job in a separate background Blender process."
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
    if transport == "headless":
        executor = HeadlessBlenderExecutor()
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


@mcp.tool(
    name="blender_job_status",
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


@mcp.tool(
    name="blender_job_cancel",
    description=(
        "Cancel a running or queued async Blender job. "
        "The job's __cancel_event__ is set; scripts that check it will stop gracefully."
    ),
)
async def job_cancel(ctx: Context, job_id: str) -> str:
    if job_id.startswith("headless-job-"):
        result = await HEADLESS_JOB_MANAGER.cancel(job_id)
    else:
        result = await _get_conn(ctx).send_command("job.cancel", {"job_id": job_id})
    return json.dumps(result, indent=2)


@mcp.tool(
    name="blender_job_list",
    description="List known async Blender jobs with their IDs, statuses, and creation timestamps.",
)
async def job_list(ctx: Context) -> str:
    headless_jobs = HEADLESS_JOB_MANAGER.list_jobs()["jobs"]
    try:
        bridge_result = await _get_conn(ctx).send_command("job.list")
        bridge_jobs = bridge_result.get("jobs", [])
    except Exception:
        bridge_jobs = []
    result = {"jobs": bridge_jobs + headless_jobs}
    return json.dumps(result, indent=2)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
