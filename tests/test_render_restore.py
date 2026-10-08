import types

import pytest

from blender_lab_mcp_client.server import _CODE_RENDER_ANIMATION, _CODE_RENDER_STILL, _build_code


def _make_scene(bpy, render_fn):
    scene = types.SimpleNamespace(
        render=types.SimpleNamespace(
            filepath="//orig.png", engine="BLENDER_EEVEE", resolution_x=1920, resolution_y=1080
        ),
        frame_start=1,
        frame_end=250,
    )
    bpy.context = types.SimpleNamespace(scene=scene)
    bpy.ops = types.SimpleNamespace(render=types.SimpleNamespace(render=render_fn))
    return scene


def _run(command, params):
    from blender_lab_mcp_client.server import _COMMAND_CODE

    ns = {"result": {}}
    exec(compile(_build_code(command, params), "<snippet>", "exec"), ns)
    return ns["result"]


@pytest.mark.parametrize("code", [_CODE_RENDER_STILL, _CODE_RENDER_ANIMATION])
def test_generated_code_has_restore_logic(code):
    assert "try:" in code and "finally:" in code
    tail = code.split("finally:")[1]
    assert "_orig[" in tail and "scene.render.filepath" in tail and "scene.render.engine" in tail


def test_still_restores_settings_and_reports_what_was_rendered(stub_bpy):
    seen = {}

    def fake_render(**kw):
        seen.update(kw, **vars(scene.render))

    scene = _make_scene(stub_bpy, fake_render)

    result = _run(
        "render.still",
        {"output_path": "C:/out/shot.png", "resolution_x": 640, "resolution_y": 360, "engine": "CYCLES"},
    )

    # The render itself saw the requested settings...
    assert seen["write_still"] is True
    assert (seen["filepath"], seen["engine"], seen["resolution_x"], seen["resolution_y"]) == (
        "C:/out/shot.png",
        "CYCLES",
        640,
        360,
    )
    assert result == {"output_path": "C:/out/shot.png", "engine": "CYCLES", "resolution_x": 640, "resolution_y": 360}
    # ...and the user's scene is back to how it was.
    r = scene.render
    assert (r.filepath, r.engine, r.resolution_x, r.resolution_y) == ("//orig.png", "BLENDER_EEVEE", 1920, 1080)


def test_still_restores_settings_when_render_fails(stub_bpy):
    def boom(**kw):
        raise RuntimeError("render failed")

    scene = _make_scene(stub_bpy, boom)

    with pytest.raises(RuntimeError, match="render failed"):
        _run("render.still", {"output_path": "C:/out/x.png", "resolution_x": 10, "engine": "CYCLES"})

    r = scene.render
    assert (r.filepath, r.engine, r.resolution_x) == ("//orig.png", "BLENDER_EEVEE", 1920)


def test_animation_restores_frame_range_and_settings(stub_bpy):
    seen = {}

    def fake_render(**kw):
        seen.update(kw, start=scene.frame_start, end=scene.frame_end, engine=scene.render.engine)

    scene = _make_scene(stub_bpy, fake_render)

    result = _run("render.animation", {"output_path": "C:/out/a_", "frame_start": 5, "frame_end": 9, "engine": "CYCLES"})

    assert seen["animation"] is True and (seen["start"], seen["end"], seen["engine"]) == (5, 9, "CYCLES")
    assert result["frame_start"] == 5 and result["frame_end"] == 9
    assert (scene.frame_start, scene.frame_end) == (1, 250)
    assert (scene.render.filepath, scene.render.engine) == ("//orig.png", "BLENDER_EEVEE")


def test_animation_restores_when_render_fails(stub_bpy):
    def boom(**kw):
        raise RuntimeError("nope")

    scene = _make_scene(stub_bpy, boom)
    with pytest.raises(RuntimeError):
        _run("render.animation", {"output_path": "C:/out/a_", "frame_start": 5, "frame_end": 9})
    assert (scene.frame_start, scene.frame_end) == (1, 250)
