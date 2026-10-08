import asyncio
import json
import logging
import os
import sys

import pytest

from blender_lab_mcp_client import headless
from blender_lab_mcp_client.headless import RESULT_PREFIX, HeadlessBlenderExecutor, resolve_blender_binary


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(headless, "_logged_binary", None)
    monkeypatch.delenv("BLENDER_BIN", raising=False)


class FakeProc:
    returncode = 0

    def __init__(self, stdout: bytes = b"", stderr: bytes = b""):
        self._out, self._err = stdout, stderr

    async def communicate(self):
        return self._out, self._err

    def kill(self):
        pass

    def terminate(self):
        pass


def _payload(result):
    body = {"result": result, "stdout": "", "stderr": "", "error": None, "timed_out": False, "cancelled": False}
    return (RESULT_PREFIX + json.dumps(body) + "\n").encode()


async def test_missing_binary_is_a_clear_error_not_filenotfound(monkeypatch):
    monkeypatch.setattr(headless.shutil, "which", lambda *_a, **_k: None)

    out = await HeadlessBlenderExecutor().execute(code="x = 1")

    assert out["result"] is None
    assert "BLENDER_BIN" in out["error"] and "PATH" in out["error"]
    assert {"stdout", "stderr", "duration_seconds", "timed_out", "cancelled"} <= set(out)


async def test_bad_blender_bin_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_BIN", str(tmp_path / "no-such-blender.exe"))

    out = await HeadlessBlenderExecutor().execute(code="x = 1")

    assert "no-such-blender" in out["error"] and "not an existing executable" in out["error"]


async def test_blender_bin_wins_over_path(monkeypatch):
    exe = sys.executable
    monkeypatch.setenv("BLENDER_BIN", exe)
    monkeypatch.setattr(headless.shutil, "which", lambda name, *a, **k: name if name == exe else "/elsewhere/blender")
    assert resolve_blender_binary() == os.path.abspath(exe)


async def test_falls_back_to_path_lookup(monkeypatch):
    monkeypatch.setattr(headless.shutil, "which", lambda name, *a, **k: "/opt/blender/blender" if name == "blender" else None)
    assert resolve_blender_binary() == os.path.abspath("/opt/blender/blender")


async def test_resolved_path_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(headless.shutil, "which", lambda *_a, **_k: "/opt/blender/blender")
    with caplog.at_level(logging.INFO, logger="blender_lab_mcp_client.headless"):
        resolve_blender_binary()
        resolve_blender_binary()
    msgs = [r.getMessage() for r in caplog.records if "Headless Blender binary" in r.getMessage()]
    assert len(msgs) == 1 and os.path.abspath("/opt/blender/blender") in msgs[0]


async def test_spawns_with_argument_list_not_a_shell(monkeypatch, tmp_path):
    exe = os.path.abspath(sys.executable)
    monkeypatch.setenv("BLENDER_BIN", exe)
    calls = []

    async def fake_exec(*argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProc(_payload({"ok": True}))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    scene = str(tmp_path / "my scene; echo pwned.blend")

    out = await HeadlessBlenderExecutor().execute(code="x = 1", blend_file=scene)

    assert out["error"] is None and out["result"] == {"ok": True}
    argv, kwargs = calls[0]
    assert argv[0] == exe and argv[1:4] == ("-b", "--factory-startup", scene)
    assert argv[4] == "--python"
    assert "shell" not in kwargs


async def test_spawn_failure_becomes_error_result(monkeypatch):
    monkeypatch.setenv("BLENDER_BIN", os.path.abspath(sys.executable))

    async def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    out = await HeadlessBlenderExecutor().execute(code="x = 1")
    assert "Could not start Blender" in out["error"] and "denied" in out["error"]


async def test_real_subprocess_without_payload_reports_exit(monkeypatch):
    # Python is not Blender: it rejects the flags and never prints a payload,
    # which exercises the real spawn + "no result payload" path end to end.
    monkeypatch.setenv("BLENDER_BIN", sys.executable)
    out = await HeadlessBlenderExecutor().execute(code="x = 1", timeout_seconds=30)
    assert "without a result payload" in out["error"]
