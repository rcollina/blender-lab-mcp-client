import json
import socket

import pytest

from blender_lab_mcp_client.server import BlenderConnection


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_send_execute_happy_path(fake_addon):
    fake_addon.reply = {"status": "ok", "result": {"answer": 42}}
    conn = BlenderConnection(fake_addon.host, fake_addon.port)

    result = await conn._send_execute("result = {'answer': 42}")

    assert result == {"answer": 42}
    assert fake_addon.requests == [
        {"type": "execute", "code": "result = {'answer': 42}", "strict_json": True}
    ]
    raw = fake_addon.raw_requests[0]
    assert raw.endswith(b"\0") and raw.count(b"\0") == 1
    assert json.loads(raw[:-1])["type"] == "execute"


async def test_send_execute_opens_a_fresh_connection_per_request(fake_addon):
    conn = BlenderConnection(fake_addon.host, fake_addon.port)
    await conn._send_execute("a = 1")
    await conn._send_execute("b = 2")
    assert fake_addon.connections == 2


async def test_send_execute_error_status_raises_runtime_error(fake_addon):
    fake_addon.reply = {"status": "error", "message": "Traceback: boom"}
    conn = BlenderConnection(fake_addon.host, fake_addon.port)

    with pytest.raises(RuntimeError, match="boom"):
        await conn._send_execute("raise Exception('boom')")


async def test_send_execute_connection_refused():
    conn = BlenderConnection("127.0.0.1", _free_port())

    with pytest.raises(ConnectionError, match="Lost connection to Blender"):
        await conn._send_execute("result = 1")


async def test_send_execute_dropped_connection(fake_addon):
    fake_addon.drop = True
    conn = BlenderConnection(fake_addon.host, fake_addon.port)

    with pytest.raises(ConnectionError):
        await conn._send_execute("result = 1")
