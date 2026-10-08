"""Shared fixtures: a fake Blender Lab add-on that speaks the real wire protocol.

The fake listens on an ephemeral loopback port, reads one null-byte-terminated
JSON request, replies with one null-byte-terminated JSON response, and closes
the socket -- exactly like the real add-on. No Blender and no external network
are ever involved.
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
import types
from typing import Any, Callable

import pytest


class FakeAddon:
    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = 0
        self.raw_requests: list[bytes] = []
        self.requests: list[dict[str, Any]] = []
        self.connections = 0
        # What to answer: a dict, or a callable(request) -> dict.
        self.reply: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]] = {
            "status": "ok",
            "result": {},
        }
        self.delay = 0.0  # seconds to wait before replying
        self.drop = False  # close without replying
        self.run_locally = False  # exec received code against a stub bpy
        self._server: asyncio.AbstractServer | None = None

    @property
    def codes(self) -> list[str]:
        return [r.get("code", "") for r in self.requests]

    @property
    def last_code(self) -> str:
        return self.codes[-1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def _answer(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.run_locally:
            return self._exec_like_addon(request["code"])
        if callable(self.reply):
            return self.reply(request)
        return self.reply

    @staticmethod
    def _exec_like_addon(code: str) -> dict[str, Any]:
        # Mirrors the add-on: namespace starts as {"result": {}}; an exception
        # becomes {"status": "error", "message": <traceback>}.
        namespace: dict[str, Any] = {"result": {}}
        try:
            exec(compile(code, "<addon>", "exec"), namespace)
            json.dumps(namespace["result"])  # strict_json
        except Exception:
            return {"status": "error", "message": traceback.format_exc()}
        return {"status": "ok", "result": namespace["result"]}

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            data = await reader.readuntil(b"\0")
            self.raw_requests.append(data)
            request = json.loads(data[:-1])
            self.requests.append(request)
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.drop:
                return
            writer.write(json.dumps(self._answer(request)).encode() + b"\0")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()  # the real add-on closes after every request


@pytest.fixture
async def fake_addon():
    addon = FakeAddon()
    await addon.start()
    try:
        yield addon
    finally:
        await addon.stop()


@pytest.fixture
def mcp_session():
    """Factory: `async with mcp_session(settings) as (session, call)`.

    `call(tool_name, **args)` invokes a tool through a real in-memory MCP session
    and returns (is_error, parsed_json_or_text).
    """
    from contextlib import asynccontextmanager

    from mcp.shared.memory import create_connected_server_and_client_session

    from blender_lab_mcp_client.server import build_server

    @asynccontextmanager
    async def factory(settings):
        server = build_server(settings)
        # Older mcp releases only accept the low-level server here.
        async with create_connected_server_and_client_session(getattr(server, "_mcp_server", server)) as session:

            async def call(_tool, **args):
                res = await session.call_tool(_tool, args)
                text = res.content[0].text if res.content else ""
                if res.isError:
                    return True, text
                try:
                    return False, json.loads(text)
                except ValueError:
                    return False, text

            yield session, call

    return factory


@pytest.fixture
def stub_bpy(monkeypatch):
    """Install a throwaway `bpy` / `mathutils` so generated snippets can run."""
    bpy = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "mathutils", types.SimpleNamespace())
    return bpy
