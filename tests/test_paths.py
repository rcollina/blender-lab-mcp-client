import ast
import json
import logging
import os
import subprocess
import sys

import pytest

from blender_lab_mcp_client.config import ConfigError, Settings, load_settings
from blender_lab_mcp_client.paths import PathNotAllowedError, _validate_path
from blender_lab_mcp_client.server import _log_startup

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


@pytest.fixture
def dirs(tmp_path):
    """tmp/write, tmp/read (read-only extra dir) and tmp/outside, all real dirs."""
    out = {}
    for name in ("write", "read", "outside"):
        d = tmp_path / name
        d.mkdir()
        out[name] = d.resolve()
    return out


@pytest.fixture
def settings(dirs):
    return Settings(write_dirs=(str(dirs["write"]),), read_dirs=(str(dirs["read"]),))


def make_dir_link(link, target):
    """Symlink, or a junction on Windows when symlinks need privileges we lack."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if done.returncode == 0:
            return
    pytest.skip("cannot create symlinks or junctions here")


def check(settings, value, mode="write"):
    return _validate_path(value, settings, mode=mode, param="p")


# -- unrestricted ----------------------------------------------------------


@pytest.mark.parametrize("value", ["//render.png", "../../anything", "C:\\x", "\\\\server\\share\\x", ""])
@pytest.mark.parametrize("mode", ["read", "write"])
def test_unrestricted_passes_through_unchanged(value, mode):
    assert _validate_path(value, Settings(), mode=mode) == value


def test_bad_mode_is_a_programming_error():
    with pytest.raises(ValueError, match="mode"):
        _validate_path("x", Settings(), mode="delete")


# -- accepted --------------------------------------------------------------


def test_absolute_path_inside_write_dir(settings, dirs):
    target = dirs["write"] / "sub" / "out.png"
    assert check(settings, str(target)) == str(target)


def test_relative_path_resolves_against_first_dir(settings, dirs):
    assert check(settings, "sub/out.png") == str(dirs["write"] / "sub" / "out.png")


def test_blender_relative_default_maps_into_first_write_dir(settings, dirs):
    assert check(settings, "//render.png") == str(dirs["write"] / "render.png")


def test_trailing_separator_is_preserved(settings, dirs):
    assert check(settings, "//frames/").endswith(os.sep)
    assert check(settings, "//render_") == str(dirs["write"] / "render_")


def test_dotdot_that_stays_inside_is_fine(settings, dirs):
    assert check(settings, str(dirs["write"] / "a" / ".." / "b.png")) == str(dirs["write"] / "b.png")


# -- rejected --------------------------------------------------------------


@pytest.mark.parametrize("value", ["../outside/x.png", "a/../../outside/x.png", "//../outside/x.png"])
def test_dotdot_traversal_rejected(settings, value):
    with pytest.raises(PathNotAllowedError, match="outside the allowed write"):
        check(settings, value)


def test_absolute_path_outside_rejected(settings, dirs):
    with pytest.raises(PathNotAllowedError):
        check(settings, str(dirs["outside"] / "x.png"))


def test_sibling_with_common_prefix_rejected(settings, dirs):
    sibling = dirs["write"].parent / (dirs["write"].name + "-evil")
    with pytest.raises(PathNotAllowedError):
        check(settings, str(sibling / "x.png"))


def test_symlink_escape_rejected(settings, dirs):
    link = dirs["write"] / "escape"
    make_dir_link(link, dirs["outside"])
    with pytest.raises(PathNotAllowedError):
        check(settings, str(link / "x.png"))
    with pytest.raises(PathNotAllowedError):
        check(settings, "//escape/x.png")


def test_symlink_inside_is_resolved_and_returned_resolved(settings, dirs):
    (dirs["write"] / "real").mkdir()
    link = dirs["write"] / "alias"
    make_dir_link(link, dirs["write"] / "real")
    assert check(settings, str(link / "x.png")) == str(dirs["write"] / "real" / "x.png")


@pytest.mark.parametrize("value", ["", "   ", "a\0b.png"])
def test_empty_and_nul_rejected(settings, value):
    with pytest.raises(PathNotAllowedError):
        check(settings, value)


def test_non_string_rejected(settings):
    with pytest.raises(PathNotAllowedError):
        check(settings, None)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["\\\\server\\share\\x.png", "\\\\?\\UNC\\server\\share\\x.png", "\\\\.\\pipe\\x"])
def test_unc_rejected_on_every_platform(settings, value):
    with pytest.raises(PathNotAllowedError, match="UNC"):
        check(settings, value)


# -- Windows semantics ---------------------------------------------------


@windows_only
def test_windows_case_insensitive_and_mixed_separators(settings, dirs):
    p = str(dirs["write"] / "Out.PNG")
    assert check(settings, p.upper()).lower() == p.lower()
    assert check(settings, p.lower().replace("\\", "/")).lower() == p.lower()


@windows_only
def test_windows_other_drive_rejected_without_crashing(settings, dirs):
    own = os.path.splitdrive(str(dirs["write"]))[0][0].upper()
    other = "Y" if own != "Y" else "Z"
    with pytest.raises(PathNotAllowedError):
        check(settings, f"{other}:\\render\\x.png")
    with pytest.raises(PathNotAllowedError):
        check(settings, f"{other}:x.png")


@windows_only
def test_windows_extended_length_prefix_inside_is_accepted(settings, dirs):
    got = check(settings, "\\\\?\\" + str(dirs["write"] / "x.png"))
    assert got == str(dirs["write"] / "x.png")


@windows_only
def test_windows_rooted_path_without_drive_is_checked(settings, dirs):
    with pytest.raises(PathNotAllowedError):
        check(settings, "\\Windows\\System32\\x.png")


# -- read vs write dirs ----------------------------------------------------


def test_read_dir_only_path_is_readable_but_not_writable(settings, dirs):
    p = str(dirs["read"] / "tex.png")
    assert check(settings, p, "read") == p
    with pytest.raises(PathNotAllowedError, match="outside the allowed write"):
        check(settings, p, "write")


def test_write_dirs_are_implicitly_readable(settings, dirs):
    p = str(dirs["write"] / "tex.png")
    assert check(settings, p, "read") == p


def test_outside_both_lists_rejected_for_read(settings, dirs):
    with pytest.raises(PathNotAllowedError, match="outside the allowed read"):
        check(settings, str(dirs["outside"] / "x.png"), "read")


def test_relative_read_path_uses_first_read_dir(settings, dirs):
    assert check(settings, "tex.png", "read") == str(dirs["read"] / "tex.png")
    assert check(settings, "//tex.png", "read") == str(dirs["read"] / "tex.png")


def test_only_read_dirs_configured_means_nothing_is_writable(dirs):
    s = Settings(read_dirs=(str(dirs["read"]),))
    assert check(s, str(dirs["read"] / "a.png"), "read")
    with pytest.raises(PathNotAllowedError, match="WRITE_DIRS"):
        check(s, "//render.png", "write")


# -- config parsing ----------------------------------------------------------


def test_dirs_parsed_from_env(dirs):
    sep = os.pathsep
    s = load_settings(
        {
            "BLENDER_LAB_MCP_WRITE_DIRS": f"{dirs['write']}{sep}{dirs['write']}{sep}",
            "BLENDER_LAB_MCP_READ_DIRS": f"{dirs['read']}{sep}{dirs['outside']}",
        }
    )
    assert s.write_dirs == (str(dirs["write"]),)
    assert s.read_dirs == (str(dirs["read"]), str(dirs["outside"]))
    assert s.paths_restricted


@pytest.mark.parametrize("name", ["WRITE_DIRS", "READ_DIRS"])
@pytest.mark.parametrize("value", ["relative/dir", "//blender/relative", "\\\\server\\share"])
def test_bad_dir_entries_rejected(name, value):
    with pytest.raises(ConfigError, match=name):
        load_settings({f"BLENDER_LAB_MCP_{name}": value})


def test_unset_means_unrestricted():
    s = load_settings({})
    assert not s.paths_restricted and s.write_dirs == () and s.read_dirs == ()


# -- tools: every path argument goes through the validator -----------------


def _params(code: str) -> dict:
    line = next(l for l in code.splitlines() if l.startswith("params = json.loads("))
    return json.loads(ast.literal_eval(line[len("params = json.loads(") : -1]))


@pytest.fixture
def restricted(fake_addon, dirs):
    return Settings(
        host=fake_addon.host,
        port=fake_addon.port,
        write_dirs=(str(dirs["write"]),),
        read_dirs=(str(dirs["read"]),),
    )


async def test_default_render_paths_work_in_restricted_mode(mcp_session, fake_addon, restricted, dirs):
    async with mcp_session(restricted) as (_, call):
        assert not (await call("blender_render_still"))[0]
        assert not (await call("blender_render_animation"))[0]
    still, anim = (_params(c) for c in fake_addon.codes)
    assert still["output_path"] == str(dirs["write"] / "render.png")
    assert anim["output_path"] == str(dirs["write"] / "render_")


async def test_render_outside_write_dir_is_rejected_before_sending(mcp_session, fake_addon, restricted, dirs):
    async with mcp_session(restricted) as (_, call):
        is_err, text = await call("blender_render_still", output_path=str(dirs["read"] / "x.png"))
        assert is_err and "outside the allowed write" in text
        is_err, text = await call("blender_render_animation", output_path="//../x_")
        assert is_err
    assert fake_addon.requests == []


@pytest.mark.parametrize("tool", ["blender_export_gltf", "blender_export_obj", "blender_export_fbx"])
async def test_exports_need_write_dir(mcp_session, fake_addon, restricted, dirs, tool):
    async with mcp_session(restricted) as (_, call):
        is_err, _ = await call(tool, filepath=str(dirs["read"] / "m.glb"))
        assert is_err and fake_addon.requests == []
        is_err, _ = await call(tool, filepath=str(dirs["write"] / "m.glb"))
        assert not is_err
    assert _params(fake_addon.last_code)["filepath"] == str(dirs["write"] / "m.glb")


async def test_set_texture_may_read_from_read_dirs(mcp_session, fake_addon, restricted, dirs):
    async with mcp_session(restricted) as (_, call):
        is_err, _ = await call("blender_material_set_texture", name="M", filepath=str(dirs["read"] / "t.png"))
        assert not is_err
        is_err, _ = await call("blender_material_set_texture", name="M", filepath=str(dirs["outside"] / "t.png"))
        assert is_err
    assert len(fake_addon.requests) == 1


@pytest.mark.parametrize("tool", ["blender_python_exec", "blender_python_exec_async"])
@pytest.mark.parametrize("transport", ["bridge", "headless"])
async def test_script_path_and_blend_file_must_be_readable(mcp_session, fake_addon, restricted, dirs, tool, transport, monkeypatch):
    import asyncio

    async def no_spawn(*a, **k):
        raise AssertionError("must not start Blender for a rejected path")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    async with mcp_session(restricted) as (_, call):
        is_err, text = await call(tool, script_path=str(dirs["outside"] / "s.py"), transport=transport)
        assert is_err and "outside the allowed read" in text
        is_err, text = await call(tool, code="x = 1", blend_file=str(dirs["outside"] / "s.blend"), transport=transport)
        assert is_err and "blend_file" in text
    assert fake_addon.requests == []


async def test_headless_executor_validates_on_its_own(dirs):
    from blender_lab_mcp_client.headless import HeadlessBlenderExecutor

    ex = HeadlessBlenderExecutor(settings=Settings(write_dirs=(str(dirs["write"]),)))
    with pytest.raises(PathNotAllowedError):
        await ex.execute(script_path=str(dirs["outside"] / "s.py"))
    with pytest.raises(PathNotAllowedError):
        await ex.execute(code="x = 1", blend_file=str(dirs["outside"] / "s.blend"))


async def test_unrestricted_tools_pass_paths_untouched(mcp_session, fake_addon):
    settings = Settings(host=fake_addon.host, port=fake_addon.port)
    async with mcp_session(settings) as (_, call):
        await call("blender_render_still")
        await call("blender_export_obj", filepath="C:\\anywhere\\m.obj")
    still, export = (_params(c) for c in fake_addon.codes)
    assert still["output_path"] == "//render.png"
    assert export["filepath"] == "C:\\anywhere\\m.obj"


# -- startup logging ---------------------------------------------------------


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_single_warning_when_unrestricted(caplog):
    with caplog.at_level(logging.INFO, logger="blender_lab_mcp_client.server"):
        _log_startup(Settings())
    msgs = _warnings(caplog)
    assert len(msgs) == 1 and "NOT restricted" in msgs[0]


def test_restricted_with_python_warns_about_bypass(caplog, dirs):
    with caplog.at_level(logging.INFO, logger="blender_lab_mcp_client.server"):
        _log_startup(Settings(write_dirs=(str(dirs["write"]),)))
    msgs = _warnings(caplog)
    assert len(msgs) == 1 and "bypass" in msgs[0]


def test_restricted_without_python_has_no_warning(caplog, dirs):
    with caplog.at_level(logging.INFO, logger="blender_lab_mcp_client.server"):
        _log_startup(Settings(write_dirs=(str(dirs["write"]),), disable_python=True))
    assert _warnings(caplog) == []


def test_python_tool_descriptions_state_the_bypass():
    import asyncio

    from blender_lab_mcp_client.server import build_server

    tools = {t.name: t for t in asyncio.run(build_server(Settings()).list_tools())}
    for name in ("blender_python_exec", "blender_python_exec_async"):
        d = tools[name].description
        assert "bypass" in d and "DISABLE_PYTHON" in d
