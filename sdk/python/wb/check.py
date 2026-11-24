"""What ``wb check`` runs where: the container checks it was asked for go to ci.run, the host checks to ci.host-run.

A host check (``on = "host"``, or ``gpu = true``) never runs in the container and is never in the gate, so ``wb check``
with no names runs the gate as it always has. Named checks and workflows (``all`` names every check) are expanded
by the commit's own project.toml, with what they need, and split: the host checks among them run on the host, the
rest in the container. When none of them is a host check, the names go to ci.run as given.
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def split(workspace, project, commit: str, names: list[str]) -> dict[str, list[str]]:
    """``{"container": [...], "host": [...]}`` for ``names`` at ``commit``. Without a jobs file to read, or with a
    name it does not know, everything goes to ci.run, which says what is wrong."""
    names = [str(name) for name in names]
    if not names:
        return {"container": [], "host": []}
    actions = workspace.engine()
    with tempfile.TemporaryDirectory(prefix="wb-check-") as scratch:
        try:
            config = workspace.config_at(project, commit, Path(scratch))
        except actions.ToolError:
            config = None
        if config is None:
            return {"container": names, "host": []}
        try:
            ordered = actions.resolve(config, names)
        except actions.ToolError:
            return {"container": names, "host": []}
        jobs = config.jobs
    host = [name for name in ordered if actions.on_host(jobs[name])]
    if not host:
        return {"container": names, "host": []}
    return {"container": [name for name in ordered if name not in host], "host": host}
