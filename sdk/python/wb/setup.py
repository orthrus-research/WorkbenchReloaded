"""``wb setup``: puts this machine's repositories on Workbench's hooks, after a report-only scrub.

    wb setup --report     scrub every repository's unpushed branches and remote ref names; change nothing
    wb setup              print the plan: wb.cli in the global git config, and the hook stubs per repository
    wb setup --yes        do it; a hook Workbench did not write is kept beside its stub as <name>.before-workbench
    wb setup --undo --yes restore those hooks and remove Workbench's

The stubs live in each repository's common git dir, so every linked worktree shares them, and they hold no path:
they run what ``git config wb.cli`` names."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import hooks
from .workspace import REPO, Workspace, WorkspaceError, common_dir, git

BACKUP = ".before-workbench"
OLD_MARKERS = ("Written by Workbench (ci.hook)", "# installed by orthrus-scrub")


def cli_command() -> str:
    """How a hook started by git's sh runs Workbench's CLI (a value for git config wb.cli): the release in service
    when wb deploy has made one, else this checkout's CLI."""
    from . import config as wbconfig
    from . import releases
    config = wbconfig.load()
    if releases.current(config).exists():
        return releases.cli_command(config)
    if os.name == "nt":
        cmd = REPO / "bin" / "wb.cmd"
        if " " in str(cmd):
            raise WorkspaceError(f"{cmd} has a space in its path, which a hook cannot pass to cmd.exe")
        return f"cmd.exe //c {cmd}"
    return str(REPO / "bin" / "wb")


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


def plan(workspace: Workspace, extra: list[str]) -> list[dict]:
    steps = [{"what": "git config --global wb.cli", "value": cli_command()}]
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
    git(Path.cwd(), "config", "--global", "wb.cli", cli_command())
    print(f"git config --global wb.cli = {cli_command()}")
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
    return 0


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
