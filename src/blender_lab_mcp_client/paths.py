"""Path restriction for the structured tools.

Configured with ``BLENDER_LAB_MCP_WRITE_DIRS`` (where render/export output may
go) and ``BLENDER_LAB_MCP_READ_DIRS`` (extra read-only locations for textures,
scripts and .blend files; write dirs are implicitly readable). When neither is
set nothing is restricted and paths are passed through untouched.

This only guards the structured tools. ``blender_python_exec`` runs arbitrary
code inside Blender and can bypass it; real containment needs
``BLENDER_LAB_MCP_DISABLE_PYTHON=1``.
"""

from __future__ import annotations

import os
from pathlib import Path

from blender_lab_mcp_client.config import PREFIX, Settings

READ = "read"
WRITE = "write"

_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"


class PathNotAllowedError(ValueError):
    """A path argument is outside the configured directories."""


def _plain(path: str) -> str:
    """Drop the Windows extended-length prefix from drive-letter paths."""
    if path.startswith(_EXTENDED) and not path.upper().startswith(_EXTENDED_UNC.upper()) and path[5:6] == ":":
        return path[len(_EXTENDED) :]
    return path


def _is_unc(path: str) -> bool:
    return path.startswith("\\\\") and not (path.startswith(_EXTENDED) and path[5:6] == ":")


def _inside(path: str, root: str) -> bool:
    # normcase makes Windows comparisons case-insensitive and slash-agnostic;
    # commonpath raises ValueError for paths on different drives.
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:
        return False


def _validate_path(value: str, settings: Settings, *, mode: str, param: str = "path") -> str:
    """Return the path to hand to Blender, or raise PathNotAllowedError.

    ``mode`` is ``"read"`` or ``"write"``. With no directories configured the
    value is returned unchanged. Otherwise the value is resolved (symlinks and
    ``..`` followed) and must lie inside an allowed directory; the *resolved*
    path is returned, so what was checked is what gets used.

    Blender-relative ``//name`` paths and plain relative paths are resolved
    against the first allowed directory for the mode (not the .blend's folder),
    so defaults like ``//render.png`` land in the first write directory.
    """
    if mode not in (READ, WRITE):
        raise ValueError(f"mode must be 'read' or 'write', got {mode!r}")
    if not settings.paths_restricted:
        return value

    where = f"{param}={value!r}"
    if not isinstance(value, str) or not value.strip():
        raise PathNotAllowedError(f"{param} must be a non-empty path")
    if "\0" in value:
        raise PathNotAllowedError(f"{where}: NUL bytes are not allowed in paths")
    if _is_unc(value):
        raise PathNotAllowedError(f"{where}: UNC / network paths are not allowed")

    roots = list(settings.write_dirs) if mode == WRITE else [*settings.read_dirs, *settings.write_dirs]
    if not roots:
        raise PathNotAllowedError(
            f"{where}: no directory is configured for {mode} access; set {PREFIX}WRITE_DIRS "
            f"(output, export) to allow writing."
        )

    if value.startswith("//"):
        # Blender-relative, always -- never a POSIX-style UNC path.
        candidate = Path(roots[0]) / value[2:].lstrip("/\\")
    else:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = Path(roots[0]) / candidate

    try:
        resolved = str(candidate.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathNotAllowedError(f"{where}: cannot resolve path ({exc})") from exc

    resolved = _plain(resolved)
    if _is_unc(resolved):
        raise PathNotAllowedError(f"{where}: resolves to a UNC / network path")

    if not any(_inside(resolved, _plain(root)) for root in roots):
        raise PathNotAllowedError(
            f"{where} resolves to {resolved!r}, which is outside the allowed {mode} directories: "
            + ", ".join(roots)
        )

    # A trailing separator means "directory / filename prefix" (e.g. render
    # animation output); resolve() would drop it.
    if value.endswith(("/", "\\")) and not resolved.endswith(os.sep):
        resolved += os.sep
    return resolved
