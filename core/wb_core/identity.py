"""Which Workbench code is running: the commit of the checkout it was started from, ``-dirty`` when tracked files
differ from it. The daemon in its image reads WB_COMMIT, which ``wb build`` bakes in; the host runner and the CLI
read their checkout. Every part reports it, so a part on other code is named instead of failing obscurely."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def source_commit(root: Path | None = None) -> str | None:
    if root is None and os.environ.get("WB_COMMIT"):
        return os.environ["WB_COMMIT"]
    root = root or ROOT
    try:
        head = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD"],
                              capture_output=True, text=True, timeout=30)
        if head.returncode != 0 or not head.stdout.strip():
            return None
        dirty = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(root), "status", "--porcelain",
                                "--untracked-files=no"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return head.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")


def short(commit: str | None) -> str:
    if not commit:
        return "unknown"
    sha, _, dirty = commit.partition("-")
    return sha[:12] + (f"-{dirty}" if dirty else "")
