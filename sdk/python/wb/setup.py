"""``wb setup``: puts this machine on Workbench: a release, the ``wb`` shim, and every repository's hooks, after a
report-only scrub.

    wb setup --report     scrub every repository's unpushed branches and remote ref names; change nothing
    wb setup              print the plan: the first release if the machine has none, the wb shim, wb.cli in the
                          global git config, and the hook stubs per repository
    wb setup --yes        do it; a hook Workbench did not write is kept beside its stub as <name>.before-workbench
    wb setup --undo --yes restore those hooks and remove Workbench's

On a machine with no release, the first one is made from this clone's commit and marked bootstrap (wb deploy makes
every later one). The shim goes in a user-writable bin folder (WB_BIN, else ~/.local/bin) and runs
<releases>/current; wb setup never changes PATH itself: when the folder is not on it, it prints the command that
adds it. The stubs live in each repository's common git dir, so every linked worktree shares them, and they hold no
path: they run what ``git config wb.cli`` names."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import hooks
from .workspace import REPO, Workspace, WorkspaceError, common_dir, git

BACKUP = ".before-workbench"
OLD_MARKERS = ("Written by Workbench (ci.hook)", "# installed by orthrus-scrub")
SHIM_MARKER = "Written by wb setup"


def _config(config=None):
    from . import config as wbconfig
    return config or wbconfig.load()


def cli_command(config=None) -> str:
    """How a hook started by git's sh runs Workbench's CLI (a value for git config wb.cli): the release in service,
    <releases>/current, which ``bootstrap`` makes on a machine that has none."""
    from . import releases
    command = releases.cli_command(_config(config))
    if os.name == "nt" and " " in command.removeprefix("cmd.exe //c "):
        raise WorkspaceError(f"{command} has a space in its path, which a hook cannot pass to cmd.exe; set "
                             "[paths] releases to a folder without one")
    return command


def bootstrap(config=None) -> dict | None:
    """The first release, made from this clone's commit when the machine has none and marked bootstrap, with the
    container pass CI recorded for its tree (if any); None when a release is already in service."""
    from . import deploy, releases
    config = _config(config)
    if releases.serving(config) is not None:
        return None
    if not (REPO / ".git").exists():
        raise WorkspaceError(f"there is no release on this machine and {REPO} is not a checkout to make one from")
    sha = git(REPO, "rev-parse", "--verify", "HEAD^{commit}")
    tree = git(REPO, "rev-parse", f"{sha}^{{tree}}")
    ci = deploy.container_pass(config, REPO, sha)
    release = releases.make(config, REPO, sha, {"tree": tree, "bootstrap": True, "override": None, "tests": None,
                                                "ci": ci})
    releases.point(config, release)
    return {"release": release, "commit": sha, "ci": ci}


# -- the shim -------------------------------------------------------------------------------------------------------
def bin_folder() -> Path:
    """The user-writable folder the ``wb`` shim goes in: WB_BIN, else ~/.local/bin."""
    named = os.environ.get("WB_BIN")
    return Path(os.path.expanduser(named)) if named else Path.home() / ".local" / "bin"


def shims(config=None, folder: Path | None = None) -> dict[Path, str]:
    """The shim files and their text: each runs the release in service, so a deploy moves them with no rewrite."""
    from . import releases
    folder = folder or bin_folder()
    bin_ = releases.current(_config(config)) / "bin"
    if os.name == "nt":
        cmd = bin_ / "wb.cmd"
        return {folder / "wb.cmd": f'@echo off\r\nrem {SHIM_MARKER}: the release in service.\r\n'
                                   f'call "{cmd}" %*\r\nexit /b %ERRORLEVEL%\r\n',
                folder / "wb": f'#!/bin/sh\n# {SHIM_MARKER}: the release in service (for Git Bash).\n'
                               f'exec "{cmd.as_posix()}" "$@"\n'}
    return {folder / "wb": f'#!/bin/sh\n# {SHIM_MARKER}: the release in service.\nexec "{bin_ / "wb"}" "$@"\n'}


def on_path(folder: Path, path: str | None = None) -> bool:
    entries = [p for p in (os.environ.get("PATH", "") if path is None else path).split(os.pathsep) if p]
    wanted = os.path.normcase(os.path.normpath(str(folder)))
    return any(os.path.normcase(os.path.normpath(os.path.expanduser(entry))) == wanted for entry in entries)


def path_command(folder: Path) -> str:
    """The command a person runs to put ``folder`` on their own PATH; wb setup prints it and never runs it."""
    if os.name == "nt":
        return (f"[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path', "
                f"'User') + ';{folder}', 'User')   # in PowerShell; then open a new terminal")
    return f"echo 'export PATH=\"{folder}:$PATH\"' >> ~/.profile   # then open a new shell"


def shim_steps(config=None, folder: Path | None = None) -> list[dict]:
    folder = folder or bin_folder()
    steps = []
    for path, text in shims(config, folder).items():
        existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        if existing is None:
            action = "install"
        elif SHIM_MARKER not in existing:
            action = "refuse: another file is there; move it away, or set WB_BIN to another folder"
        else:
            action = "keep (already the shim)" if existing == text else "rewrite the shim"
        steps.append({"what": f"shim {path}", "path": str(path), "action": action})
    steps.append({"what": "PATH", "action": f"{folder} is on PATH" if on_path(folder) else
                  f"{folder} is not on PATH; wb setup does not change it: {path_command(folder)}"})
    return steps


def install_shims(config=None, folder: Path | None = None) -> int:
    folder = folder or bin_folder()
    refused = 0
    folder.mkdir(parents=True, exist_ok=True)
    for path, text in shims(config, folder).items():
        if path.is_file() and SHIM_MARKER not in path.read_text(encoding="utf-8", errors="replace"):
            print(f"{path} is another file, so the shim was not written; move it away, or set WB_BIN to another "
                  "folder")
            refused += 1
            continue
        path.write_bytes(text.encode("utf-8"))
        os.chmod(path, 0o755)
        print(f"shim: {path}")
    if not on_path(folder):
        print(f"{folder} is not on PATH, and wb setup does not change PATH. To add it, run:\n  {path_command(folder)}")
    return 1 if refused else 0


def repositories(workspace: Workspace, extra: list[str]) -> list[tuple[str, Path]]:
    """Every registered project's repository, Workbench's own, and any named with --repo: (label, common dir)."""
    found: dict[Path, str] = {}
    for project in workspace.projects().values():
        if project.common:
            found.setdefault(project.common, project.name)
    for label, path in [("workbench", REPO)] + [(Path(p).name, Path(p)) for p in extra]:
        common = common_dir(path)
        if common:
            found.setdefault(common, label)
    covered = []
    for common, label in found.items():
        top = common.parent if common.name == ".git" else common
        if not git(top, "remote", check=False):
            print(f"{label}: no remote, so nothing leaves this machine from it; not covered", file=sys.stderr)
            continue
        covered.append((label, common))
    return sorted(covered, key=lambda item: item[0])


def report(workspace: Workspace, extra: list[str]) -> int:
    """The scrub over what each repository has not pushed yet, and over its ref names. Changes nothing."""
    scrub = hooks._scrub()
    note = hooks.rules_note(scrub)
    if note:
        print(f"note: {note}")
    total = 0
    for label, common in repositories(workspace, extra):
        top = common.parent if common.name == ".git" else common
        try:
            rules = scrub.Rules.load(top)
            branches = git(top, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
            remote = git(top, "for-each-ref", "--format=%(refname:short)", "refs/remotes").splitlines()
            unpushed = git(top, "rev-list", "--branches", "--not", "--remotes")
            findings = scrub.check_range(top, ["--branches", "--not", "--remotes"], rules) if unpushed else []
            for ref in branches + remote:
                findings += list(scrub.check_line(ref, f"ref name {ref}", rules))
        except (WorkspaceError, OSError, scrub.ToolError) as error:
            print(f"{label}: could not scrub: {error}")
            continue
        commits = len(unpushed.splitlines()) if unpushed else 0
        print(f"{label}: {commits} unpushed commit(s), {len(branches) + len(remote)} ref(s), "
              f"{len(findings)} finding(s)")
        for finding in findings:
            print(f"  {finding}")
        total += len(findings)
    print(f"\n{total} finding(s) in all; nothing was changed. After `wb setup --yes`, the hooks refuse these on "
          "commit or push.")
    return 1 if total else 0


def release_step(config) -> dict:
    from . import releases
    serving = releases.serving(config)
    if serving is not None:
        return {"what": "release", "action": f"{serving.name} in service (wb deploy makes the next)"}
    head = git(REPO, "rev-parse", "--short=12", "HEAD") if (REPO / ".git").exists() else None
    return {"what": "release", "action": f"none on this machine: make the first from {head} (marked bootstrap)"
            if head else f"none on this machine, and {REPO} is not a checkout to make one from"}


def plan(workspace: Workspace, extra: list[str]) -> list[dict]:
    steps = [release_step(workspace.config)] + shim_steps(workspace.config)
    steps.append({"what": "git config --global wb.cli", "value": cli_command(workspace.config)})
    for label, common in repositories(workspace, extra):
        for name in hooks.NAMES:
            path = common / "hooks" / name
            existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
            if existing is not None and hooks.MARKER in existing:
                action = "keep (already Workbench's)" if existing == hooks.stub(name) else "rewrite Workbench's stub"
            elif existing is None:
                action = "install"
            elif any(marker in existing for marker in OLD_MARKERS):
                action = "replace an older Workbench or scrub hook"
            else:
                action = f"install, keeping the existing hook as {name}{BACKUP}"
            steps.append({"what": f"{label}: {name}", "path": str(path), "action": action})
    return steps


def lab_questions(values: dict | None) -> list[str]:
    """What wb setup asks about the game lab before any configure hook or driver runs: each [lab] key not yet set,
    with what it decides."""
    values = values or {}
    asks = {"home": "where instances, sessions and the lease live (empty: [runner] lab_home)",
            "trusted": "which projects' configure hooks and drivers may run here, e.g. ['riven', 'cubature']",
            "eula": "true if you accept the Minecraft EULA for the instances built here",
            "env": "more variable names a driver gets the turn token, lease or runtime cache in, e.g. "
                   "{ CUBIC_LAB_TURN_LEASE = 'token', WB_RUNTIME_CACHE = 'cache' }"}
    return [f"[lab] {key}: not set; {what}" for key, what in asks.items() if key not in values]


def apply(workspace: Workspace, extra: list[str]) -> int:
    made = bootstrap(workspace.config)
    if made is not None:
        ci = made["ci"]
        print(f"release: made the first, {made['release'].name}, from {made['commit'][:12]} (marked bootstrap)")
        print("  the container pass for its tree: " + (f"passed (run {ci['runs'][-1]})" if ci["passed"] and ci["runs"]
              else "passed" if ci["passed"] else f"none yet ({ci['reason']}); `wb check` at that commit runs it"))
    status = install_shims(workspace.config)
    command = cli_command(workspace.config)
    git(Path.cwd(), "config", "--global", "wb.cli", command)
    print(f"git config --global wb.cli = {command}")
    for label, common in repositories(workspace, extra):
        folder = common / "hooks"
        folder.mkdir(parents=True, exist_ok=True)
        for name in hooks.NAMES:
            path = folder / name
            if path.is_file():
                existing = path.read_text(encoding="utf-8", errors="replace")
                ours = hooks.MARKER in existing or any(marker in existing for marker in OLD_MARKERS)
                backup = path.with_name(name + BACKUP)
                if not ours and not backup.exists():
                    path.replace(backup)
                    print(f"{label}: kept the existing {name} as {backup.name}")
            path.write_text(hooks.stub(name), encoding="utf-8", newline="\n")
            os.chmod(path, 0o755)
        print(f"{label}: hooks installed in {folder}")
    return status


def undo(workspace: Workspace, extra: list[str]) -> int:
    for label, common in repositories(workspace, extra):
        for name in hooks.NAMES:
            path = common / "hooks" / name
            backup = path.with_name(name + BACKUP)
            if path.is_file() and hooks.MARKER in path.read_text(encoding="utf-8", errors="replace"):
                path.unlink()
            if backup.is_file():
                backup.replace(path)
                print(f"{label}: restored {name}")
        print(f"{label}: Workbench's hooks removed")
    return 0


def main(args) -> int:
    try:
        workspace = Workspace()
        if args.report:
            return report(workspace, args.repo)
        if args.undo:
            if not args.yes:
                print("wb setup --undo restores the hooks kept at install and removes Workbench's; add --yes")
                return 2
            return undo(workspace, args.repo)
        steps = plan(workspace, args.repo)
        from . import config as wbconfig
        for line in lab_questions(wbconfig.load().values.get("lab")):
            print(line)  # answered in the machine's workbench.toml; nothing here writes it
        if not args.yes:
            for step in steps:
                print(f"{step['what']}: {step.get('action') or step.get('value')}")
            print("\nnothing changed; run `wb setup --report` first, then `wb setup --yes`")
            return 0
        return apply(workspace, args.repo)
    except WorkspaceError as error:
        print(f"wb: {error}", file=sys.stderr)
        return 3
