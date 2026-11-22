"""Starting wb-host when the user logs in, on each platform (spec 12; platform-neutral).

wb-host runs game clients that open GPU windows, so it must live in the user's desktop session, never as a
system service. Each platform has its own way to start a program at login:

| platform | how                                                    |
|----------|--------------------------------------------------------|
| windows  | a Task Scheduler task, at logon, for the current user  |
| linux    | a systemd --user unit, wanted by graphical-session.target (DISPLAY / WAYLAND_DISPLAY come with it) |
| macos    | a launchd LaunchAgent in ~/Library/LaunchAgents, in the GUI session |

Every backend starts the checkout's own launcher (bin/wb or bin\\wb.cmd), so nothing is installed and no global
environment variable is set. ``plan`` returns what would be written and run; ``apply`` does it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

TASK = "Workbench host runner"
UNIT = "wb-host.service"
LABEL = "cc.orthrus.wb-host"


@dataclass
class Plan:
    platform: str
    files: dict[Path, str] = field(default_factory=dict)
    commands: list[list[str]] = field(default_factory=list)
    summary: str = ""

    def describe(self) -> str:
        lines = [self.summary]
        for path in self.files:
            lines.append(f"  write {path}")
        for command in self.commands:
            lines.append("  run   " + " ".join(command))
        return "\n".join(lines)


def _launcher(repo: Path, platform: str) -> Path:
    return repo / "bin" / ("wb.cmd" if platform == "windows" else "wb")


def windowless_python() -> str:
    """pythonw beside this interpreter (no console window), else the pyw launcher, else this interpreter."""
    beside = Path(sys.executable).with_name("pythonw.exe")
    if beside.exists():
        return str(beside)
    return shutil.which("pyw") or sys.executable


def powershell() -> str:
    """Windows PowerShell ships with every Windows; PowerShell 7 (pwsh) is optional."""
    return shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"


def install_plan(repo: Path, platform: str, home: Path | None = None) -> Plan:
    home = Path(home or Path.home())
    launcher = _launcher(repo, platform)
    if platform == "windows":
        python, script_file = windowless_python(), repo / "bin" / "wb-host.pyw"
        script = (f"$a = New-ScheduledTaskAction -Execute '{python}' -Argument '\"{script_file}\"' -WorkingDirectory '{repo}'; "
                  "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
                  "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                  "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew; "
                  f"Register-ScheduledTask -TaskName '{TASK}' -Action $a -Trigger $t -Settings $s -Force | Out-Null; "
                  f"Start-ScheduledTask -TaskName '{TASK}'")
        return Plan(platform, commands=[[powershell(), "-NoProfile", "-Command", script]],
                    summary=f"Register the Task Scheduler task '{TASK}' for the current user: at logon run "
                            f"{script_file} with {Path(python).name} (no console window)")
    if platform == "linux":
        unit = home / ".config" / "systemd" / "user" / UNIT
        text = "\n".join([
            "[Unit]",
            "Description=Workbench host runner (wb host serve)",
            "PartOf=graphical-session.target",
            "After=graphical-session.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={repo}",
            f"ExecStart={launcher} host serve",
            "Restart=on-failure",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=graphical-session.target",
            "",
        ])
        return Plan(platform, files={unit: text},
                    commands=[["systemctl", "--user", "daemon-reload"],
                              ["systemctl", "--user", "enable", "--now", UNIT]],
                    summary=f"Install the systemd user unit {UNIT}, started with the graphical session")
    if platform == "macos":
        plist = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        logs = home / "Library" / "Logs" / "wb-host.log"
        text = "\n".join([
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
            '<plist version="1.0">',
            "<dict>",
            f"  <key>Label</key><string>{LABEL}</string>",
            "  <key>ProgramArguments</key>",
            f"  <array><string>{escape(str(launcher))}</string><string>host</string><string>serve</string></array>",
            f"  <key>WorkingDirectory</key><string>{escape(str(repo))}</string>",
            "  <key>RunAtLoad</key><true/>",
            "  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>",
            "  <key>LimitLoadToSessionType</key><string>Aqua</string>",
            f"  <key>StandardOutPath</key><string>{escape(str(logs))}</string>",
            f"  <key>StandardErrorPath</key><string>{escape(str(logs))}</string>",
            "</dict>",
            "</plist>",
            "",
        ])
        uid = str(os.getuid()) if hasattr(os, "getuid") else "$UID"
        return Plan(platform, files={plist: text},
                    commands=[["launchctl", "bootstrap", f"gui/{uid}", str(plist)]],
                    summary=f"Install the LaunchAgent {LABEL}, started in the GUI session at login")
    raise ValueError(f"unknown platform {platform}")


def uninstall_plan(repo: Path, platform: str, home: Path | None = None) -> Plan:
    home = Path(home or Path.home())
    if platform == "windows":
        return Plan(platform, commands=[[powershell(), "-NoProfile", "-Command",
                                         f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false"]],
                    summary=f"Remove the Task Scheduler task '{TASK}'")
    if platform == "linux":
        return Plan(platform, commands=[["systemctl", "--user", "disable", "--now", UNIT]],
                    summary=f"Disable and remove the systemd user unit {UNIT}",
                    files={home / ".config" / "systemd" / "user" / UNIT: ""})
    if platform == "macos":
        plist = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        uid = str(os.getuid()) if hasattr(os, "getuid") else "$UID"
        return Plan(platform, commands=[["launchctl", "bootout", f"gui/{uid}", str(plist)]],
                    summary=f"Remove the LaunchAgent {LABEL}", files={plist: ""})
    raise ValueError(f"unknown platform {platform}")


def apply(plan: Plan, removing: bool = False) -> int:
    if not removing:
        for path, text in plan.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    code = 0
    for command in plan.commands:
        done = subprocess.run(command, capture_output=True, text=True)
        if done.stdout.strip() or done.stderr.strip():
            print((done.stdout + done.stderr).strip())
        code = code or done.returncode
    if removing:
        for path in plan.files:
            path.unlink(missing_ok=True)
    return code
