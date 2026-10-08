from blender_lab_mcp_client.config import Settings
from blender_lab_mcp_client.server import build_server

READ_ONLY = {
    "blender_scene_get_info",
    "blender_scene_list_objects",
    "blender_object_get_transform",
    "blender_object_get_hierarchy",
    "blender_material_list",
    "blender_job_status",
    "blender_job_list",
}
DESTRUCTIVE = {
    "blender_object_delete",
    "blender_history_undo",
    "blender_history_redo",
    "blender_render_still",
    "blender_render_animation",
    "blender_export_gltf",
    "blender_export_obj",
    "blender_export_fbx",
    "blender_python_exec",
    "blender_python_exec_async",
    "blender_job_cancel",
}


async def test_annotations_match_expected_sets():
    tools = await build_server(Settings()).list_tools()
    assert len(tools) == 27
    by_name = {t.name: t for t in tools}

    for name, tool in by_name.items():
        a = tool.annotations
        assert a is not None, name
        assert a.openWorldHint is False, name
        assert a.readOnlyHint is (name in READ_ONLY), name
        assert a.destructiveHint is (name in DESTRUCTIVE), name

    non_destructive_writes = set(by_name) - READ_ONLY - DESTRUCTIVE
    assert {"blender_object_create", "blender_material_set_texture", "blender_object_scale"} <= non_destructive_writes
    assert READ_ONLY | DESTRUCTIVE | non_destructive_writes == set(by_name)
