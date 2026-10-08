"""Environment-driven configuration.

Every variable is prefixed ``BLENDER_LAB_MCP_`` (``BLENDER_BIN`` is the one
historical exception and is read by ``headless.py``). ``load_settings`` takes
an explicit mapping so tests never have to touch ``os.environ``.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

PREFIX = "BLENDER_LAB_MCP_"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876
DEFAULT_MCP_PORT = 8765
# The streamable-http listener is always bound to this address; there is
# deliberately no setting to change it.
MCP_BIND_HOST = "127.0.0.1"
TRANSPORTS = ("stdio", "streamable-http")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}


class ConfigError(ValueError):
    """Raised for invalid or unsafe configuration; the message is user-facing."""


@dataclass(frozen=True)
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allow_remote: bool = False
    disable_python: bool = False
    readonly: bool = False  # implies disable_python
    transport: str = "stdio"  # how MCP clients reach *this* server
    mcp_port: int = DEFAULT_MCP_PORT  # streamable-http only; bound to loopback
    # Resolved absolute directories. WRITE_DIRS: where output_path / filepath
    # (render, export) may point. READ_DIRS: extra read-only dirs (textures,
    # scripts, .blend files); write dirs are implicitly readable too.
    write_dirs: tuple[str, ...] = ()
    read_dirs: tuple[str, ...] = ()

    @property
    def paths_restricted(self) -> bool:
        return bool(self.write_dirs or self.read_dirs)

    @property
    def python_enabled(self) -> bool:
        return not (self.disable_python or self.readonly)

    @property
    def mode(self) -> str:
        if self.readonly:
            return "read-only (python and all mutating tools disabled)"
        if self.disable_python:
            return "python disabled"
        return "full (python execution enabled)"


def _env_bool(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(PREFIX + name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{PREFIX}{name} must be 1 or 0 (got {env[PREFIX + name]!r})")


def _env_port(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(PREFIX + name)
    if raw is None or raw.strip() == "":
        return default
    try:
        port = int(raw.strip())
    except ValueError:
        raise ConfigError(f"{PREFIX}{name} must be an integer between 1 and 65535 (got {raw!r})") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"{PREFIX}{name} must be between 1 and 65535 (got {port})")
    return port


def _env_dirs(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    """Parse an os.pathsep-separated list of absolute directories."""
    out: list[str] = []
    for entry in (e.strip() for e in env.get(PREFIX + name, "").split(os.pathsep)):
        if not entry:
            continue
        if entry.startswith(("\\\\", "//")):
            raise ConfigError(f"{PREFIX}{name}: UNC and Blender-relative ('//') entries are not allowed (got {entry!r})")
        if not Path(entry).is_absolute():
            raise ConfigError(f"{PREFIX}{name}: entries must be absolute paths (got {entry!r})")
        resolved = str(Path(entry).resolve())
        if resolved not in out:
            out.append(resolved)
    return tuple(out)


def is_loopback_host(host: str) -> bool:
    """True for ``localhost`` and loopback IP literals. No DNS lookups are done."""
    candidate = host.strip().strip("[]")
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    host = (env.get(PREFIX + "HOST") or DEFAULT_HOST).strip()
    port = _env_port(env, "PORT", DEFAULT_PORT)
    allow_remote = _env_bool(env, "ALLOW_REMOTE")

    if not is_loopback_host(host) and not allow_remote:
        raise ConfigError(
            f"Refusing to talk to non-loopback host {host!r}: the Blender Lab add-on has no "
            f"authentication and executes arbitrary Python. Set {PREFIX}ALLOW_REMOTE=1 only if "
            "you understand that anyone who can reach that port controls Blender (and the machine it runs on)."
        )

    transport = (env.get(PREFIX + "TRANSPORT") or "stdio").strip().lower()
    if transport not in TRANSPORTS:
        raise ConfigError(f"{PREFIX}TRANSPORT must be one of {', '.join(TRANSPORTS)} (got {transport!r})")
    mcp_port = _env_port(env, "MCP_PORT", DEFAULT_MCP_PORT)
    if transport == "streamable-http" and mcp_port == port and is_loopback_host(host):
        raise ConfigError(f"{PREFIX}MCP_PORT ({mcp_port}) must differ from the Blender add-on port")

    return Settings(
        host=host,
        port=port,
        allow_remote=allow_remote,
        disable_python=_env_bool(env, "DISABLE_PYTHON"),
        readonly=_env_bool(env, "READONLY"),
        transport=transport,
        mcp_port=mcp_port,
        write_dirs=_env_dirs(env, "WRITE_DIRS"),
        read_dirs=_env_dirs(env, "READ_DIRS"),
    )
