from blender_lab_mcp_client.config import Settings, load_settings
from blender_lab_mcp_client.server import build_server

READ_TOOLS = {
    "blender_scene_get_info",
    "blender_scene_list_objects",
    "blender_object_get_transform",
    "blender_object_get_hierarchy",
    "blender_material_list",
    "blender_job_status",
    "blender_job_list",
}
PYTHON_TOOLS = {"blender_python_exec", "blender_python_exec_async"}


async def _names(settings):
    return {t.name for t in await build_server(settings).list_tools()}


async def test_default_registers_all_27_tools():
    names = await _names(Settings())
    assert len(names) == 27
    assert PYTHON_TOOLS <= names and READ_TOOLS <= names


async def test_disable_python_hides_only_python_tools():
    default = await _names(Settings())
    names = await _names(load_settings({"BLENDER_LAB_MCP_DISABLE_PYTHON": "1"}))
    assert default - names == PYTHON_TOOLS
    assert "blender_render_still" in names and "blender_job_cancel" in names


async def test_readonly_registers_only_read_tools():
    names = await _names(load_settings({"BLENDER_LAB_MCP_READONLY": "1"}))
    assert names == READ_TOOLS


async def test_readonly_wins_over_disable_python_flag_combinations():
    s = load_settings({"BLENDER_LAB_MCP_READONLY": "1", "BLENDER_LAB_MCP_DISABLE_PYTHON": "0"})
    assert not s.python_enabled
    assert await _names(s) == READ_TOOLS


async def test_disabled_tools_cannot_be_called(mcp_session, fake_addon):
    settings = Settings(host=fake_addon.host, port=fake_addon.port, disable_python=True)
    async with mcp_session(settings) as (_, call):
        is_err, text = await call("blender_python_exec", code="x = 1")
    assert is_err and "Unknown tool" in text
    assert fake_addon.requests == []


def test_mode_strings():
    assert "full" in Settings().mode
    assert "python disabled" in Settings(disable_python=True).mode
    assert "read-only" in Settings(readonly=True).mode
