"""``wb status --doctor``: what still ties a project to the old routes, read from git and this machine's config.

- Pins on every set-named branch. For each registered project but the Set, at its landed commit and at its
  set-named branch's tip (the published branch where the checkout has one): a WB_PIN_CACHE read, a sourceCommit
  field, a pin file (cc.lock.json, anything in a pins/ folder), a refresh or sync task in its build or checks files,
  and a sibling commit, which is a 40-hex literal naming a commit the Set lists for another project or a commit in
  another registered project's repository. A commit permalink (``https://github.com/<org>/<repo>/blob/<sha>/...``,
  ``/tree/<sha>``, ``/commit/<sha>``) cites a commit and pins nothing, so its SHA is not one, nor is the SHA a
  Markdown link shows as its own text (``[`<sha>`](<its permalink>)``). A branch that carries no project.toml is not converted yet and is only
  named. Paths that set.json ``doctor.exempt`` lists for every project, or for that project, are never read.
- Workbench's own schedules: none may refresh or sync pins.
- The lease: the old lock path (``[runner] lease_file``, the configured alias) is the file the lease is taken on,
  and each registered checkout's old in-tree route to it (``.local/<its place under the lab home>``) resolves to
  that same file, as the old lanes folder resolves to the coordination folder.

Nothing is fetched, written or locked. Each finding names its project, branch, file and line.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0"}
HEX40 = re.compile(r"(?<![0-9A-Fa-f])[0-9a-f]{40}(?![0-9A-Fa-f])")
# A link to a commit, a file or a tree at a commit: a citation, which nothing builds or fetches from.
PERMALINK = re.compile(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:blob|tree|commit)/[0-9a-f]{40}"
                       r"(?![0-9A-Fa-f])")
# A Markdown link whose text is the SHA of its own permalink: [`<sha>`](https://github.com/.../commit/<sha>).
PERMALINK_SHOWN = re.compile(r"\[`?(?P<sha>[0-9a-f]{40})`?\]\(https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
                             r"(?:blob|tree|commit)/(?P=sha)(?![0-9A-Fa-f])[^)\s]*\)")
REFRESH = re.compile(r"(?<![A-Za-z0-9_])(?:refresh|sync)[A-Z][A-Za-z0-9]*")
REFRESH_FILES = ["*.gradle", "*.gradle.kts", "actions.toml", "project.toml"]
PIN_FILE = re.compile(r"(^|/)cc\.lock\.json$|(^|/)pins/")
SCHEDULE_PIN = re.compile(r"refresh|sync|pin", re.IGNORECASE)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, env={**os.environ, **GIT_ENV})


def _glob(pattern: str) -> re.Pattern:
    """A doctor.exempt pattern as a regular expression over a repository path: ``**`` spans folders, ``*`` does not."""
    out, index = "", 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out, index = out + "(?:.*/)?", index + 3
        elif pattern.startswith("**", index):
            out, index = out + ".*", index + 2
        elif pattern[index] == "*":
            out, index = out + "[^/]*", index + 1
        elif pattern[index] == "?":
            out, index = out + "[^/]", index + 1
        else:
            out, index = out + re.escape(pattern[index]), index + 1
    return re.compile(out + "$")


def _grep(repo: Path, commit: str, args: list[str], paths: list[str], exempt: list[str]) -> list[tuple[str, int, str]]:
    """``git grep`` at a commit: (file, line, text) for each matching line in a text file."""
    specs = [*(paths or ["."]), *[f":(exclude,glob){pattern}" for pattern in exempt]]
    done = _git(repo, "grep", "-n", "-I", "-z", "--no-color", *args, commit, "--", *specs)
    found = []
    for raw in done.stdout.decode("utf-8", "replace").splitlines():
        name, _, rest = raw.partition("\0")
        number, _, text = rest.partition("\0")
        found.append((name.removeprefix(f"{commit}:"), int(number or 0), text))
    return found


def _literals(text: str) -> list[str]:
    """The 40-hex literals of a line that may pin a commit: every one outside a commit permalink."""
    return HEX40.findall(PERMALINK.sub(" ", PERMALINK_SHOWN.sub(" ", text)))


def _commits(repo: Path, tokens: set[str]) -> set[str]:
    """The tokens that name a commit in ``repo``."""
    if not tokens or not repo.is_dir():
        return set()
    done = subprocess.run(["git", "cat-file", "--batch-check"], cwd=repo, capture_output=True, text=True,
                          input="\n".join(sorted(tokens)) + "\n", env={**os.environ, **GIT_ENV})
    return {line.split()[0] for line in done.stdout.splitlines() if line.split()[1:2] == ["commit"]}


def scan(project, commit: str, siblings: dict, exempt: list[str], listed: dict[str, str]) -> list[dict]:
    """Findings in ``project``'s tree at ``commit``. ``siblings`` maps each other registered project's name to its
    repository; ``listed`` maps a commit the Set names to the project it belongs to."""
    repo = project.repo
    found = []

    def add(kind, file, line, text):
        found.append({"kind": kind, "file": file, "line": line, "text": text.strip()[:160]})

    for file, line, text in _grep(repo, commit, ["-F", "-e", "WB_PIN_CACHE", "-e", "sourceCommit"], [], exempt):
        for kind in ("WB_PIN_CACHE", "sourceCommit"):
            if kind in text:
                add(kind, file, line, text)
    for file, line, text in _grep(repo, commit, ["-w", "-E", "-e", "(refresh|sync)[A-Z][A-Za-z0-9]*"],
                                  REFRESH_FILES, exempt):
        if REFRESH.search(text):
            add("refresh task", file, line, text)
    names = _git(repo, "ls-tree", "-r", "--name-only", "-z", commit).stdout.decode("utf-8", "replace").split("\0")
    patterns = [_glob(pattern) for pattern in exempt]
    for name in names:
        if name and PIN_FILE.search(name) and not any(p.match(name) for p in patterns):
            add("pin file", name, 1, "a pin file")
    hits = _grep(repo, commit, ["-w", "-E", "-e", "[0-9a-f]{40}"], [], exempt)
    tokens = {token for _, _, text in hits for token in _literals(text)}
    own = _commits(repo, tokens)
    owner = {token: name for token, name in listed.items() if name != project.name and token in tokens}
    for name, other in siblings.items():
        for token in _commits(other, tokens - own - set(owner)):
            owner.setdefault(token, name)
    for file, line, text in hits:
        for token in dict.fromkeys(_literals(text)):
            if token in owner:
                add("sibling commit", file, line, f"{owner[token]} {token[:12]}: {text}")
    return found


def branches(workspace, set_file: dict) -> tuple[list[dict], list[str]]:
    """What to read: ``[{"project", "branch", "commits": [(label, commit)]}]`` for each converted project, and a
    note for each registered project that is not read."""
    from .check import _carries
    manifest = workspace.engine().config_names()[0]
    lines = set_file.get("projects") or {}
    read, notes = [], []
    for name, project in sorted(workspace.projects().items()):
        if name == "set":
            continue
        if not project.repo.is_dir():
            notes.append(f"{name}: no checkout at {project.repo}")
            continue
        line = lines.get(name)
        branch, ref, tip = workspace.set_branch(project, line)
        commits = []
        if line and line.get("commit"):
            commits.append((f"landed {str(line['commit'])[:12]}", str(line["commit"])))
        if tip and tip not in [c for _, c in commits]:
            commits.append((f"{ref.removeprefix('refs/remotes/').removeprefix('refs/heads/')} {tip[:12]}", tip))
        elif tip:
            commits[0] = (commits[0][0] + " = tip", commits[0][1])
        if not commits:
            notes.append(f"{name}: no {branch} branch in {project.repo}")
            continue
        if not line and not _carries(project.repo, tip, manifest):
            notes.append(f"{name}: {branch} ({tip[:12]}) carries no {manifest}, so it is not converted; not read")
            continue
        read.append({"project": project, "branch": branch, "commits": commits})
    return read, notes


def schedules(config) -> list[dict]:
    """Workbench's own schedules that would refresh or sync pins."""
    found = []
    for name, schedule in ((config.values.get("schedules") or {}) if config else {}).items():
        if not isinstance(schedule, dict) or schedule.get("enabled") is False:
            continue
        what = f"{schedule.get('command', '')} {json.dumps(schedule.get('params') or {})}"
        if SCHEDULE_PIN.search(what):
            origin = (config.origin(f"schedules.{name}.command") or config.origin(f"schedules.{name}") or "") \
                if hasattr(config, "origin") else ""
            file, _, line = origin.rpartition(":") if origin[-1:].isdigit() else (origin, "", "")
            found.append({"project": "workbench", "branch": "config", "kind": "refresh schedule",
                          "file": file or f"schedules.{name}", "line": int(line or 0),
                          "text": f"schedule {name} runs {what.strip()}"})
    return found


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def lease(settings, projects: dict) -> tuple[list[str], list[dict]]:
    """Confirms the old lock path is the lease file: ``(lines that confirm, findings)``."""
    if str(Path(__file__).resolve().parents[3] / "host") not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "host"))
    from wb_host import lease as leases
    try:
        lock, coordination, _ = leases.paths(settings)
    except (KeyError, TypeError) as error:
        return [], [{"project": "workbench", "branch": "config", "kind": "lease", "file": "[runner]", "line": 0,
                     "text": f"the lease file cannot be named from this machine's config ({error})"}]
    runner = settings.runner or {}
    lab = getattr(settings, "lab", None) or {}
    home = Path(os.path.expanduser(lab.get("home") or runner.get("lab_home") or ""))
    ok, found = [], []

    def finding(project, file, text):
        found.append({"project": project, "branch": "checkout", "kind": "lease", "file": file, "line": 0,
                      "text": text})

    alias = runner.get("lease_file")
    if not alias:
        ok.append(f"lease: no old lock path is configured; the lease file is {lock}")
    elif not lock.parent.is_dir():
        finding("workbench", "[runner] lease_file", f"the old lock path {alias} names a folder that does not exist")
    else:
        ok.append(f"lease: the old lock path {alias} is the lease file"
                  + ("" if lock.exists() else " (not taken yet)"))
    routes = []
    for target, label in ((lock, "lock"), (coordination, "lanes folder")):
        try:
            routes.append((Path(".local") / Path(target).relative_to(home), target, label))
        except ValueError:
            continue  # not under the lab home: the old in-tree route never named it
    for name, project in sorted(projects.items()):
        for route, target, label in routes:
            old = project.repo / route
            if not old.exists():
                continue
            if _same(old, target):
                ok.append(f"lease: {name}'s old {label} route {route.as_posix()} resolves to {target}")
            else:
                finding(name, route.as_posix(), f"the old {label} route resolves to {os.path.realpath(old)}, "
                                                f"not {target}: a taker there follows another {label}")
    return ok, found


def run(workspace, settings=None) -> dict:
    """The whole report: ``{"read", "notes", "confirmed", "findings"}``."""
    set_file = workspace.set_file()
    from wb_core import setfile
    listed = {str(line.get("commit")): name for name, line in (set_file.get("projects") or {}).items()
              if line.get("commit")}
    for key, hold in (set_file.get("holds") or {}).items():
        if isinstance(hold, dict) and hold.get("commit"):
            listed.setdefault(str(hold["commit"]), key.split(":")[0].split("@")[0])
    read, notes = branches(workspace, set_file)
    projects = workspace.projects()
    findings, done = [], []
    for item in read:
        project = item["project"]
        siblings = {name: other.repo for name, other in projects.items()
                    if name not in (project.name, "set") and other.repo.is_dir()}
        for label, commit in item["commits"]:
            done.append(f"{project.name} {item['branch']} ({label})")
            exempt = setfile.exempt_paths(set_file, project.name)
            for finding in scan(project, commit, siblings, exempt, listed):
                findings.append({"project": project.name, "branch": f"{item['branch']} ({label})", **finding})
    findings += schedules(workspace.config)
    confirmed = []
    if settings is None:
        try:
            from wb_core.settings import resolve
            settings = resolve(workspace.config)
        except Exception as error:  # noqa: BLE001 - a config the daemon would refuse: say so and go on
            notes.append(f"the lease was not checked: the machine's config does not resolve ({error})")
    if settings is not None:
        confirmed, found = lease(settings, projects)
        findings += found
    return {"read": done, "notes": notes, "confirmed": confirmed, "findings": findings,
            "exempt": setfile.exempt_shown(set_file)}


def show(report: dict) -> str:
    lines = ["doctor: read " + ("; ".join(report["read"]) or "no set-named branch")]
    if report["exempt"]:
        lines.append("doctor: exempt (set.json doctor.exempt): " + ", ".join(report["exempt"]))
    lines += [f"doctor: {note}" for note in report["notes"]]
    lines += [f"doctor: {line}" for line in report["confirmed"]]
    for item in report["findings"]:
        where = f"{item['file']}:{item['line']}" if item["line"] else item["file"]
        lines.append(f"  {item['project']}  {item['branch']}  {where}  {item['kind']}: {item['text']}")
    lines.append(f"doctor: {len(report['findings'])} finding(s)" if report["findings"] else "doctor: nothing found")
    return "\n".join(lines)


def main(args, workspace=None) -> int:
    from .workspace import Workspace, WorkspaceError
    try:
        report = run(workspace or Workspace())
    except WorkspaceError as error:
        print(f"wb: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1, default=str) if getattr(args, "json", False) else show(report))
    return 1 if report["findings"] else 0
