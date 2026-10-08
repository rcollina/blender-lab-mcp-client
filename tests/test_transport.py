import pytest

from blender_lab_mcp_client.config import ConfigError, Settings, load_settings
from blender_lab_mcp_client.server import build_server


def test_default_transport_is_stdio():
    s = load_settings({})
    assert (s.transport, s.mcp_port) == ("stdio", 8765)


def test_streamable_http_settings():
    s = load_settings({"BLENDER_LAB_MCP_TRANSPORT": "Streamable-HTTP", "BLENDER_LAB_MCP_MCP_PORT": "9000"})
    assert (s.transport, s.mcp_port) == ("streamable-http", 9000)


@pytest.mark.parametrize("value", ["sse", "http", "tcp"])
def test_unknown_transport_rejected(value):
    with pytest.raises(ConfigError, match="TRANSPORT"):
        load_settings({"BLENDER_LAB_MCP_TRANSPORT": value})


def test_mcp_port_validated():
    with pytest.raises(ConfigError, match="MCP_PORT"):
        load_settings({"BLENDER_LAB_MCP_MCP_PORT": "70000"})


def test_mcp_port_must_not_collide_with_blender_port():
    with pytest.raises(ConfigError, match="must differ"):
        load_settings({"BLENDER_LAB_MCP_TRANSPORT": "streamable-http", "BLENDER_LAB_MCP_MCP_PORT": "9876"})


def test_http_listener_is_always_bound_to_loopback():
    # Even with a remote Blender host allowed, our own listener stays on loopback,
    # and no environment variable exists to change its address.
    env = {
        "BLENDER_LAB_MCP_TRANSPORT": "streamable-http",
        "BLENDER_LAB_MCP_MCP_PORT": "9001",
        "BLENDER_LAB_MCP_HOST": "10.1.2.3",
        "BLENDER_LAB_MCP_ALLOW_REMOTE": "1",
        "BLENDER_LAB_MCP_MCP_HOST": "0.0.0.0",
    }
    server = build_server(load_settings(env))
    assert server.settings.host == "127.0.0.1"
    assert server.settings.port == 9001
    assert server.settings.streamable_http_path == "/mcp"


def test_dns_rebinding_protection_is_on_for_loopback():
    server = build_server(Settings(transport="streamable-http"))
    sec = server.settings.transport_security
    assert sec is not None and sec.enable_dns_rebinding_protection
