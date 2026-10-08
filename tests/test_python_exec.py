import asyncio

import pytest

from blender_lab_mcp_client.server import BlenderConnection, build_server

SHAPE = {"result", "stdout", "stderr", "error", "duration_seconds", "timed_out", "cancelled"}


@pytest.fixture
def conn(fake_addon, stub_bpy):
    fake_addon.run_locally = True
    return BlenderConnection(fake_addon.host, fake_addon.port)


async def test_result_stdout_stderr_duration(conn):
    out = await conn.send_command(
        "python.execute",
        {
            "code": (
                "import sys\n"
                "print('hello')\n"
                "print('oops', file=sys.stderr)\n"
                "__result__ = {'n': args['n'] + 1}\n"
            ),
            "args": {"n": 41},
        },
    )
    assert set(out) == SHAPE
    assert out["result"] == {"n": 42}
    assert out["stdout"] == "hello\n"
    assert out["stderr"] == "oops\n"
    assert out["error"] is None
    assert out["timed_out"] is False and out["cancelled"] is False
    assert isinstance(out["duration_seconds"], float) and out["duration_seconds"] >= 0


async def test_user_exception_is_reported_with_captured_output(conn):
    out = await conn.send_command("python.execute", {"code": "print('before')\nraise ValueError('bad')"})
    assert out["result"] is None
    assert out["stdout"] == "before\n"
    assert "ValueError: bad" in out["error"]


async def test_system_exit_does_not_escape(conn):
    out = await conn.send_command("python.execute", {"code": "raise SystemExit(3)"})
    assert "SystemExit" in out["error"]


async def test_non_json_result_falls_back_to_repr(conn):
    out = await conn.send_command("python.execute", {"code": "__result__ = {1, 2}"})
    assert out["error"] is None
    assert out["result"] == repr({1, 2})


async def test_output_is_capped(conn):
    out = await conn.send_command("python.execute", {"code": "print('x' * 60000)"})
    assert len(out["stdout"]) < 51000
    assert "truncated" in out["stdout"]


async def test_script_path(conn, tmp_path):
    script = tmp_path / "s.py"
    script.write_text("__result__ = 'from file ✓'", encoding="utf-8")
    out = await conn.send_command("python.execute", {"script_path": str(script)})
    assert out["result"] == "from file ✓"


@pytest.mark.parametrize(
    "params",
    [{}, {"code": "x = 1", "script_path": "whatever.py"}],
)
async def test_code_xor_script_path(conn, params):
    out = await conn.send_command("python.execute", params)
    assert out["error"] and "ValueError" in out["error"]


async def test_timeout_returns_clear_error(fake_addon):
    fake_addon.delay = 0.5
    conn = BlenderConnection(fake_addon.host, fake_addon.port)

    out = await conn.send_command("python.execute", {"code": "x = 1", "timeout_seconds": 0.1})

    assert set(out) == SHAPE
    assert out["timed_out"] is True
    assert out["result"] is None
    assert "NOT stopped" in out["error"]
    assert out["duration_seconds"] < 0.5


async def test_no_timeout_when_unset(fake_addon):
    fake_addon.delay = 0.05
    fake_addon.reply = {"status": "ok", "result": {"result": 1}}
    conn = BlenderConnection(fake_addon.host, fake_addon.port)
    out = await conn.send_command("python.execute", {"code": "x = 1"})
    assert out == {"result": 1}


async def test_lock_released_after_timeout(fake_addon):
    fake_addon.delay = 0.3
    conn = BlenderConnection(fake_addon.host, fake_addon.port)
    await conn.send_command("python.execute", {"code": "x = 1", "timeout_seconds": 0.05})
    fake_addon.delay = 0
    fake_addon.reply = {"status": "ok", "result": {"scene": "s"}}
    assert await asyncio.wait_for(conn._send_execute("result = 1"), 2) == {"scene": "s"}


async def test_descriptions_are_honest():
    tools = {t.name: t for t in await build_server().list_tools()}
    for name in ("blender_python_exec", "blender_python_exec_async", "blender_job_cancel"):
        assert "__cancel_event__" not in tools[name].description
    assert "NOT stopped" in tools["blender_python_exec"].description
