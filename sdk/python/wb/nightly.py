"""The two runs that keep a machine in order each night: retention (``wb gc --nightly``) and the backup
(``wb backup``), started by the operating system's scheduler from the release in service.

On Windows ``wb setup --yes`` installs them as scheduled tasks and ``wb setup --undo --yes`` removes them; elsewhere
setup prints the crontab lines, and never edits a crontab itself. ``wb gc --schedule`` prints the same lines."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Task:
    name: str          # the scheduled task's name on Windows
    at: str            # HH:MM, local time: off the hour, after the day's work, apart from each other
    args: tuple        # the wb verb and its arguments


TASKS = (Task("Workbench gc", "03:31", ("gc", "--nightly")),
         Task("Workbench backup", "04:07", ("backup",)))


def _wb(config) -> Path:
    from . import releases
    return releases.current(config) / "bin" / ("wb.cmd" if os.name == "nt" else "wb")


def line(config, task: Task) -> str:
    """The command that installs ``task``: a schtasks line on Windows, a crontab line elsewhere."""
    wb = _wb(config)
    if os.name == "nt":
        return (f'schtasks /Create /F /SC DAILY /ST {task.at} /TN "{task.name}" '
                f'/TR "\\"{wb}\\" {" ".join(task.args)}"')
    hour, minute = task.at.split(":")
    return f"{int(minute)} {int(hour)} * * * {wb} {' '.join(task.args)}"


def installed(task: Task) -> bool | None:
    """Whether the scheduler holds ``task``; None where Workbench does not ask the scheduler (outside Windows)."""
    if os.name != "nt":
        return None
    done = subprocess.run(["schtasks", "/Query", "/TN", task.name], capture_output=True, text=True,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    return done.returncode == 0


def steps(config) -> list[dict]:
    """The plan's lines for the nightly runs."""
    found = []
    for task in TASKS:
        state = installed(task)
        action = ("keep (installed)" if state else f"install at {task.at}: {line(config, task)}") if state is not None \
            else f"add to your crontab: {line(config, task)}"
        found.append({"what": f"nightly: {task.name}", "action": action})
    return found


def install(config, say=print) -> int:
    """Installs each nightly run the scheduler does not hold (Windows); elsewhere prints the crontab lines."""
    status = 0
    for task in TASKS:
        if os.name != "nt":
            say(f"nightly: add to your crontab (wb setup never edits it): {line(config, task)}")
            continue
        wb = _wb(config)
        done = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", task.at, "/TN", task.name,
                               "/TR", f'"{wb}" {" ".join(task.args)}'], capture_output=True, text=True,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        if done.returncode == 0:
            say(f"nightly: {task.name} runs `wb {' '.join(task.args)}` daily at {task.at}")
        else:
            status = 1
            say(f"nightly: {task.name} could not be installed: {(done.stderr or done.stdout).strip()}")
    return status


def remove(say=print):
    """Removes the nightly runs setup installed (Windows)."""
    if os.name != "nt":
        return
    for task in TASKS:
        if installed(task):
            subprocess.run(["schtasks", "/Delete", "/F", "/TN", task.name], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            say(f"nightly: removed {task.name}")
