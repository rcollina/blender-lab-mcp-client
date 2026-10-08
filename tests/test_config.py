import pytest

from blender_lab_mcp_client.config import ConfigError, load_settings


def test_defaults():
    s = load_settings({})
    assert (s.host, s.port, s.allow_remote) == ("127.0.0.1", 9876, False)


def test_host_and_port_from_env():
    s = load_settings({"BLENDER_LAB_MCP_HOST": "localhost", "BLENDER_LAB_MCP_PORT": "1234"})
    assert (s.host, s.port) == ("localhost", 1234)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost", "LOCALHOST"])
def test_loopback_hosts_accepted(host):
    assert load_settings({"BLENDER_LAB_MCP_HOST": host}).host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "10.0.0.1", "::"])
def test_non_loopback_refused_by_default(host):
    with pytest.raises(ConfigError, match="ALLOW_REMOTE"):
        load_settings({"BLENDER_LAB_MCP_HOST": host})


def test_non_loopback_allowed_with_opt_in():
    s = load_settings({"BLENDER_LAB_MCP_HOST": "192.168.1.5", "BLENDER_LAB_MCP_ALLOW_REMOTE": "1"})
    assert s.host == "192.168.1.5" and s.allow_remote


@pytest.mark.parametrize("port", ["0", "65536", "-1", "abc", "9876.5", "99999999999"])
def test_invalid_ports_rejected(port):
    with pytest.raises(ConfigError, match="BLENDER_LAB_MCP_PORT"):
        load_settings({"BLENDER_LAB_MCP_PORT": port})


@pytest.mark.parametrize("port,expected", [("1", 1), ("65535", 65535), (" 80 ", 80)])
def test_valid_port_bounds(port, expected):
    assert load_settings({"BLENDER_LAB_MCP_PORT": port}).port == expected


def test_bad_bool_rejected():
    with pytest.raises(ConfigError, match="ALLOW_REMOTE"):
        load_settings({"BLENDER_LAB_MCP_ALLOW_REMOTE": "maybe"})
