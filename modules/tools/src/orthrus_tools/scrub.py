"""Refuse commits that carry what a project keeps out of its repository.

A commit carries no attribution trailers, no private files, no local paths (drive letters, home
folders, ``.local``) and no personal data: not in its diff, not in its message, and not in the
author and committer it names. This tool checks that mechanically, as Git hooks or in CI.

    orthrus-scrub install                 # commit-msg, pre-commit and pre-push hooks in this clone
    orthrus-scrub staged                  # what a pre-commit hook checks: the staged diff
    orthrus-scrub message .git/COMMIT_EDITMSG
    orthrus-scrub range origin/main..HEAD # every commit in a range, merges included (CI)
    orthrus-scrub tree                    # every tracked file, for a first audit of a repository

Findings print as ``where: category: detail`` and the exit status is 1. A line that must keep a
match (a documented example path) can carry the marker ``scrub: allow``.

A range checks every commit's author, committer and message, merges included, and the diff of
every commit that is not a merge (what a merge brings in is checked in the commits it merges). A
merge made on a forge's website passes no local hook: after a fetch, ``range main..origin/main``
checks it.

The built-in checks name no one: ``*-by:`` and ``*session:`` trailers, local paths, email
addresses and ``.local/``. What else is private is the machine's to say, in a rules file kept out
of every repository: ``~/.config/wb/scrub-rules.toml``, or the file ``ORTHRUS_SCRUB_RULES_FILE``
names. It takes the same keys as the settings below:

    attribution-names = ["examplebot"]       # regular expressions: names that no co-author
                                             # trailer, "made with ..." line, author or committer
                                             # may credit
    deny-patterns = ['example\\.chat/share']  # regular expressions no line may match
    deny-paths = [".tooling/**"]             # files that must stay untracked

Personal terms (names, handles) belong in an untracked file, by default ``.local/scrub-terms.txt``
or the file ``ORTHRUS_SCRUB_TERMS_FILE`` names, one per line; they are matched case-insensitively
as whole words and never printed.

Settings (``orthrus.toml`` table ``[scrub]`` or ``[tool.orthrus.scrub]``): ``allow-paths`` and
``deny-paths`` (glob lists, ``**`` crosses directories; an allowed path may exist, and its
content is still checked), ``allow-emails``, ``attribution-names`` and ``deny-patterns`` (regular
expressions) and ``terms-file``. The rules file adds to them.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import cli
from .cli import ENVIRONMENT, FINDING, OK, USAGE, Output, ToolError
from .config import project_root, tool_settings

ALLOW_MARKER = "scrub: allow"
HOOK_MARKER = "# installed by orthrus-scrub"
HOOKS = ("commit-msg", "pre-commit", "pre-push")
ZERO_SHA = re.compile(r"^0+$")

# Trailers a message or a file must never carry, whoever they name.
TRAILERS = [
    re.compile(r"(?im)^\s*(assisted|generated|written|created)-by:"),
    re.compile(r"(?im)^\s*[\w-]*session:\s*https?://"),
]
DEFAULT_DENY_PATHS = [".local/**", ".worktrees/**"]
# The machine's own rules, outside every repository (in the container, name it with the variable).
RULES_FILE = Path.home() / ".config" / "wb" / "scrub-rules.toml"


def attribution_patterns(names: list[str]) -> list[re.Pattern[str]]:
    """The built-in trailers, and the lines that credit one of ``names`` (regular expressions)."""
    patterns = list(TRAILERS)
    if names:
        credited = "|".join(f"(?:{name})" for name in names)
        patterns += [
            re.compile(rf"(?im)^\s*co-authored-by:.*\b({credited})\b"),
            re.compile(rf"(?i)\b(generated|created|written|made)\s+(with|by|using)\s+\[?({credited})\b"),
        ]
    return patterns


def credited_pattern(names: list[str]) -> re.Pattern[str] | None:
    """One of ``names`` (regular expressions) as a whole word, for a commit's author or committer."""
    if not names:
        return None
    either = "|".join(f"(?:{name})" for name in names)
    return re.compile(rf"(?i)\b({either})\b")


def private_rules() -> dict[str, Any]:
    """The machine's rules file: the one ``ORTHRUS_SCRUB_RULES_FILE`` names, else ``RULES_FILE``."""
    named = os.environ.get("ORTHRUS_SCRUB_RULES_FILE")
    path = Path(named) if named else RULES_FILE
    if not path.is_file():
        if named:
            raise ToolError(ENVIRONMENT, f"ORTHRUS_SCRUB_RULES_FILE names {path}, which is not a file")
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ToolError(ENVIRONMENT, f"cannot read the scrub rules in {path}: {error}") from None
LOCAL_PATHS = [
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(\\\\?|/)(?=[\w .$-])"),  # drive letters; scrub: allow
    re.compile(r"(?<![\w.])/(home|Users)/(?!\$|<|\{|USER\b|user\b|runner\b)[\w.-]+"),
    re.compile(r"(?i)\\\\wsl(\$|\.localhost)\\"),
]
# The top-level domain must start with a letter, so a pinned version (pkg@v1.2.0) is no address.
# The top-level domain has at least two letters, so a version such as name.yml@vX.Y.Z is not one.
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)*\.[A-Za-z][A-Za-z0-9-]+\b")
DEFAULT_ALLOW_EMAILS = [
    r"noreply",
    r"@example\.(com|org|net)$",
    r"\.invalid$",
    r"\.(test|example|localhost)$",
]


@dataclass(frozen=True)
class Finding:
    where: str
    category: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: {self.category}: {self.detail}"

    def to_dict(self) -> dict[str, str]:
        return {"where": self.where, "category": self.category, "detail": self.detail}


@dataclass
class Rules:
    allow_paths: list[str]
    deny_paths: list[str]
    allow_emails: list[re.Pattern[str]]
    terms: list[re.Pattern[str]]
    attribution: list[re.Pattern[str]] = field(default_factory=lambda: list(TRAILERS))
    deny_patterns: list[re.Pattern[str]] = field(default_factory=list)
    credited: re.Pattern[str] | None = None  # names no author or committer may carry

    @staticmethod
    def load(root: Path) -> Rules:
        settings = tool_settings(root, "scrub")
        private = private_rules()

        def both(key: str) -> list[str]:
            """A list setting from the repository, then the machine's rules file."""
            return list(settings.get(key, [])) + list(private.get(key, []))

        terms_file = root / settings.get("terms-file", ".local/scrub-terms.txt")
        extra = os.environ.get("ORTHRUS_SCRUB_TERMS_FILE")
        terms: list[str] = []
        for path in (terms_file, Path(extra) if extra else None):
            if path is not None and path.is_file():
                lines = path.read_text(encoding="utf-8").splitlines()
                terms += [t.strip() for t in lines if t.strip() and not t.startswith("#")]
        names = both("attribution-names")
        return Rules(
            allow_paths=both("allow-paths"),
            deny_paths=DEFAULT_DENY_PATHS + both("deny-paths"),
            allow_emails=[re.compile(p, re.I) for p in DEFAULT_ALLOW_EMAILS + both("allow-emails")],
            terms=[re.compile(rf"(?i)(?<!\w){re.escape(t)}(?!\w)") for t in terms],
            attribution=attribution_patterns(names),
            deny_patterns=[re.compile(p) for p in both("deny-patterns")],
            credited=credited_pattern(names),
        )


def glob_match(path: str, pattern: str) -> bool:
    """Gitignore-like matching: ``**`` spans directories; ``**/x`` also matches ``x`` at the root."""
    regex = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            regex += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            regex += ".*"
            i += 2
        elif pattern[i] == "*":
            regex += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            regex += "[^/]"
            i += 1
        else:
            regex += re.escape(pattern[i])
            i += 1
    return re.fullmatch(regex, path) is not None


def check_path(path: str, rules: Rules) -> Iterator[Finding]:
    posix = PurePosixPath(path).as_posix()
    if any(glob_match(posix, pattern) for pattern in rules.allow_paths):
        return
    for pattern in rules.deny_paths:
        if glob_match(posix, pattern):
            yield Finding(posix, "private-file", f"matches {pattern}; keep it untracked")
            return


def check_text(text: str, where: str, rules: Rules, first_line: int = 1) -> Iterator[Finding]:
    for offset, line in enumerate(text.splitlines()):
        if ALLOW_MARKER in line:
            continue
        place = f"{where}:{first_line + offset}"
        yield from check_line(line, place, rules)


def check_line(line: str, place: str, rules: Rules) -> Iterator[Finding]:
    for pattern in rules.attribution:
        match = pattern.search(line)
        if match:
            yield Finding(place, "attribution", repr(match.group(0).strip()))
            break
    for pattern in rules.deny_patterns:
        match = pattern.search(line)
        if match:
            yield Finding(place, "denied-text", repr(match.group(0).strip()))
            break
    for pattern in LOCAL_PATHS:
        match = pattern.search(line)
        if match:
            yield Finding(place, "local-path", repr(line.strip()[:80]))
            break
    for match in EMAIL.finditer(line):
        address = match.group(0)
        if not any(allowed.search(address) for allowed in rules.allow_emails):
            yield Finding(place, "personal-data", f"email address {address!r}")
    for index, term in enumerate(rules.terms, start=1):
        if term.search(line):
            yield Finding(place, "personal-data", f"term #{index} from the terms file")


def git(root: Path, *args: str, stdin: str | None = None) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=stdin,
        )
    except FileNotFoundError:
        raise ToolError(ENVIRONMENT, "git is not installed or not on PATH") from None
    if completed.returncode != 0:
        raise ToolError(USAGE, f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def added_lines(diff: str) -> Iterator[tuple[str, int, str]]:
    """(path, new line number, text) for every added line of a ``-U0`` unified diff."""
    path, number = "", 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = target[2:] if target.startswith("b/") else target
            if target == "/dev/null":
                path = ""
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            number = int(match.group(1)) if match else 0
        elif line.startswith("+") and path:
            yield path, number, line[1:]
            number += 1


def check_diff(diff: str, names: Iterable[str], rules: Rules, label: str = "") -> list[Finding]:
    findings: list[Finding] = []
    for name in names:
        findings += check_path(name, rules)
    for path, number, text in added_lines(diff):
        if ALLOW_MARKER in text:
            continue
        findings += check_line(text, f"{label}{path}:{number}", rules)
    return findings


DIFF_ARGS = ("--no-color", "--no-ext-diff", "-U0", "--diff-filter=ACMR", "--no-renames")


def check_staged(root: Path, rules: Rules) -> list[Finding]:
    names = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0")
    diff = git(root, "diff", "--cached", *DIFF_ARGS)
    return check_diff(diff, [n for n in names if n], rules)


def check_identity(identity: str, place: str, rules: Rules) -> Iterator[Finding]:
    """A commit's author or committer, ``name <address>``: no personal data, no credited name."""
    credited = rules.credited.search(identity) if rules.credited else None
    if credited:
        yield Finding(place, "attribution", repr(credited.group(0)))
    yield from check_line(identity, place, rules)


def check_commit(root: Path, sha: str, rules: Rules, merge: bool = False) -> list[Finding]:
    """A commit's author, committer and message, and its diff unless it is a merge."""
    short = sha[:10]
    shown = git(root, "log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%B", sha)
    author, committer, message = shown.split("\n", 2)
    findings = list(check_identity(author, f"{short} author", rules))
    findings += check_identity(committer, f"{short} committer", rules)
    findings += check_text(message, f"{short} message", rules)
    if merge:
        return findings  # what a merge brings in is checked in the commits it merges
    names = git(root, "show", "--format=", "--name-only", "--diff-filter=ACMR", "-z", sha)
    diff = git(root, "show", "--format=", *DIFF_ARGS, sha)
    findings += check_diff(diff, [n for n in names.split("\0") if n.strip()], rules, f"{short} ")
    return findings


def check_range(root: Path, revisions: list[str], rules: Rules) -> list[Finding]:
    findings: list[Finding] = []
    for line in git(root, "rev-list", "--reverse", "--parents", *revisions).splitlines():
        sha, *parents = line.split()
        findings += check_commit(root, sha, rules, merge=len(parents) > 1)
    return findings


def check_tree(root: Path, rules: Rules) -> list[Finding]:
    findings: list[Finding] = []
    for name in git(root, "ls-files", "-z").split("\0"):
        if not name:
            continue
        findings += check_path(name, rules)
        path = root / name
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue  # binary
        findings += check_text(data.decode("utf-8", errors="replace"), name, rules)
    return findings


def pre_push(root: Path, stdin: str, rules: Rules) -> list[Finding]:
    """Check what a push would publish, from the ``<local ref> <sha> <remote ref> <sha>`` lines."""
    findings: list[Finding] = []
    for line in stdin.splitlines():
        parts = line.split()
        if len(parts) != 4 or ZERO_SHA.match(parts[1]):
            continue  # malformed, or a branch deletion
        local, remote = parts[1], parts[3]
        if ZERO_SHA.match(remote):
            revisions = [local, "--not", "--remotes"]
        else:
            revisions = [f"{remote}..{local}"]
        findings += check_range(root, revisions, rules)
    return findings


def install(root: Path, force: bool, out: Output) -> int:
    hooks = Path(git(root, "rev-parse", "--git-path", "hooks").strip())
    if not hooks.is_absolute():
        hooks = root / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable).as_posix()
    installed: list[str] = []
    out.result = {"hooks": str(hooks), "installed": installed}
    for name in HOOKS:
        path = hooks / name
        if path.exists() and HOOK_MARKER not in path.read_text(encoding="utf-8", errors="replace"):
            if not force:
                raise ToolError(
                    USAGE, f"{path} exists and was not written by orthrus-scrub; use --force"
                )
        script = (
            f'#!/bin/sh\n{HOOK_MARKER}\nexec "{python}" -m orthrus_tools.scrub hook {name} "$@"\n'
        )
        path.write_text(script, encoding="utf-8", newline="\n")
        path.chmod(0o755)
        installed.append(name)
        out.line(f"installed {name}")
    return OK


def hooks_installed(root: Path) -> bool:
    """Whether every orthrus-scrub hook is installed in the clone at ``root``."""
    hooks = Path(git(root, "rev-parse", "--git-path", "hooks").strip())
    if not hooks.is_absolute():
        hooks = root / hooks
    return all(
        (hooks / name).is_file()
        and HOOK_MARKER in (hooks / name).read_text(encoding="utf-8", errors="replace")
        for name in HOOKS
    )


def report(findings: list[Finding], context: str, out: Output) -> int:
    out.result = {"context": context, "findings": [finding.to_dict() for finding in findings]}
    for finding in findings:
        out.error(str(finding))
    if findings:
        out.error(
            f"orthrus-scrub: {len(findings)} finding(s) in {context}. Remove them, or mark a line "
            f"that must keep its text with '{ALLOW_MARKER}'."
        )
        return FINDING
    return OK


def build_parser() -> cli.Parser:
    parser = cli.parser(
        "orthrus-scrub", __doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root", type=Path, metavar="DIR", help="repository root (default: the Git work tree)"
    )
    commands = cli.subcommands(parser)
    commands.add_parser("staged", help="check the staged diff")
    message = commands.add_parser("message", help="check a commit message file")
    message.add_argument("file", type=Path)
    revisions = commands.add_parser("range", help="check every commit in a revision range")
    revisions.add_argument("revisions", nargs="+")
    commands.add_parser("tree", help="check every tracked file")
    installer = commands.add_parser("install", help="install the Git hooks in this clone")
    installer.add_argument("--force", action="store_true", help="replace hooks written by others")
    hook = commands.add_parser("hook", help="entry point the installed hooks call")
    hook.add_argument("name", choices=HOOKS)
    hook.add_argument("hook_args", nargs="*")
    return parser


def _message(path: Path, rules: Rules, out: Output) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ToolError(USAGE, f"cannot read {path}: {error}") from None
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    return report(list(check_text(body, "message", rules)), "the commit message", out)


def run(args: argparse.Namespace, out: Output) -> int:
    root = (args.root or project_root()).resolve()
    if args.command == "install":
        return install(root, args.force, out)
    rules = Rules.load(root)
    if args.command == "message":
        return _message(args.file, rules, out)
    if args.command == "staged":
        return report(check_staged(root, rules), "the staged changes", out)
    if args.command == "range":
        return report(check_range(root, args.revisions, rules), " ".join(args.revisions), out)
    if args.command == "tree":
        return report(check_tree(root, rules), "the tracked files", out)
    if args.name == "commit-msg":
        if not args.hook_args:
            raise ToolError(USAGE, "commit-msg needs the message file")
        return _message(Path(args.hook_args[0]), rules, out)
    if args.name == "pre-commit":
        return report(check_staged(root, rules), "the staged changes", out)
    return report(pre_push(root, sys.stdin.read(), rules), "the commits being pushed", out)


def main(argv: list[str] | None = None) -> int:
    return cli.run("scrub", build_parser, run, argv)


if __name__ == "__main__":
    sys.exit(main())
