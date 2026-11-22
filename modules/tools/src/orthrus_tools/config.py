"""Find a project's root and read a tool's settings from it.

A project configures a tool in ``orthrus.toml`` at its root (a table per tool, for Gradle and
other non-Python projects) or in ``pyproject.toml`` under ``[tool.orthrus.<tool>]``. Paths in
settings are relative to the project root and always written with ``/``.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any


def project_root(start: Path | None = None) -> Path:
    """The Git work tree containing ``start``, else the nearest directory holding a config file."""
    start = (start or Path.cwd()).resolve()
    try:
        completed = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return Path(completed.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    for directory in (start, *start.parents):
        if (directory / "orthrus.toml").is_file() or (directory / "pyproject.toml").is_file():
            return directory
    return start


def tool_settings(root: Path, tool: str) -> dict[str, Any]:
    """Settings for ``tool``: ``orthrus.toml`` wins over ``pyproject.toml`` when both define it."""
    orthrus = root / "orthrus.toml"
    if orthrus.is_file():
        table = tomllib.loads(orthrus.read_text(encoding="utf-8")).get(tool)
        if isinstance(table, dict):
            return table
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        table = data.get("tool", {}).get("orthrus", {}).get(tool)
        if isinstance(table, dict):
            return table
    return {}
