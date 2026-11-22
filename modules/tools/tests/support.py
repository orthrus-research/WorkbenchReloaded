"""Helpers shared by the tests: run a command and capture what it prints."""

import contextlib
import io
import json
import subprocess
from pathlib import Path


def capture(main, *argv):
    """(exit code, stdout, stderr) of ``main(argv)``."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def capture_json(main, *argv):
    """(exit code, the one JSON object printed on stdout)."""
    code, out, _ = capture(main, *argv, "--json")
    return code, json.loads(out)


def git_repo(root: Path) -> Path:
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.com"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root
