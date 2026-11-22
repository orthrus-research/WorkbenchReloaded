"""Run a build and record what it was built from and what it produced.

    orthrus-tools receipt run --run-id build-001 --artifact "build/libs/*.jar" \\
        --junit "build/test-results/**/TEST-*.xml" -- ./gradlew build --rerun-tasks
    orthrus-tools receipt verify .local/receipts/build-001/receipt.json

``run`` hashes every source file Git would track (tracked and untracked, not ignored), runs the
command with its output in ``build.log``, then records the artifacts and JUnit reports the
command freshly wrote, the identity files named (a JDK's ``release`` file, a lock file) and
whether any source changed while it ran. Each run gets its own folder, which is never
overwritten: a receipt describes one build. ``verify`` checks that the artifacts and sources on
disk are still the ones a receipt names. Settings come from ``[receipt]`` in orthrus.toml.
Exit status: 0 passed, 1 the build or a check failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import cli
from .cli import FINDING, OK, USAGE, Output, ToolError
from .config import project_root, tool_settings

SCHEMA = "orthrus.receipt.v1"
DEFAULT_OUT = ".local/receipts"
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
COUNTS = ("tests", "failures", "errors", "skipped")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def describe(root: Path, path: Path) -> dict[str, Any]:
    """Path, size, SHA-256 and modification time, refused if the file changes while read."""
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"{relative(root, path)} changed while it was read")
    return {
        "path": relative(root, path),
        "bytes": len(data),
        "sha256": sha256(data),
        "modifiedAtNs": after.st_mtime_ns,
    }


def source_inputs(root: Path, exclude: list[str]) -> dict[str, str]:
    """SHA-256 of every file Git would track: tracked plus untracked, not ignored."""
    try:
        listed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
            timeout=120,
        ).stdout
    except FileNotFoundError:
        raise ToolError(cli.ENVIRONMENT, "git is not installed") from None
    except subprocess.CalledProcessError:
        raise ToolError(USAGE, f"{root} is not a Git work tree") from None
    prefixes = tuple(p.strip("/") + "/" for p in exclude if p.strip("/"))
    inputs = {}
    for name in sorted(set(listed.decode("utf-8", "replace").split("\0")) - {""}):
        if name.startswith(prefixes):
            continue
        path = root / name
        if path.is_file():  # a deleted tracked file is listed but absent
            inputs[name] = sha256(path.read_bytes())
    return inputs


def source_digest(inputs: dict[str, str]) -> str:
    return sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode())


def glob(root: Path, patterns: list[str]) -> dict[str, list[Path]]:
    return {pattern: sorted(p for p in root.glob(pattern) if p.is_file()) for pattern in patterns}


def junit_counts(path: Path) -> dict[str, int]:
    """Totals of a JUnit XML report: one <testsuite>, or <testsuites> around several."""
    element = ET.fromstring(path.read_bytes())
    suites = [element] if element.tag == "testsuite" else list(element.iter("testsuite"))
    counts = dict.fromkeys(COUNTS, 0)
    for suite in suites:
        for key in COUNTS:
            value = int(suite.get(key, "0") or 0)
            if value < 0:
                raise ValueError(f"negative {key} count in {path.name}")
            counts[key] += value
    return counts


def git_state(root: Path) -> dict[str, Any]:
    def ask(*arguments: str) -> str | None:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments], capture_output=True, text=True, timeout=60
        )
        return completed.stdout.strip() if completed.returncode == 0 else None

    return {"commit": ask("rev-parse", "HEAD"), "dirty": bool(ask("status", "--porcelain"))}


def settings_list(settings: dict[str, Any], key: str, given: list[str]) -> list[str]:
    configured = settings.get(key, [])
    return list(given) or ([configured] if isinstance(configured, str) else list(configured))


def record(args: argparse.Namespace, root: Path, out: Output) -> int:
    settings = tool_settings(root, "receipt")
    command = list(args.build)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise ToolError(USAGE, "name the build command after --")
    if not RUN_ID.match(args.run_id):
        raise ToolError(USAGE, "a run id is letters, digits, '.', '_' and '-', like build-001")
    base = args.out or Path(str(settings.get("out", DEFAULT_OUT)))
    base = base if base.is_absolute() else root / base
    directory = base / args.run_id
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ToolError(USAGE, f"{directory} exists; a receipt is never overwritten") from None
    artifacts = settings_list(settings, "artifacts", args.artifact)
    reports = settings_list(settings, "junit", args.junit)
    identities = settings_list(settings, "identity", args.identity)
    exclude = settings_list(settings, "exclude", args.exclude) + [relative(root, base)]
    allow_skipped = args.allow_skipped or bool(settings.get("allow_skipped", False))

    errors: list[str] = []
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "runId": args.run_id,
        "status": "failed",
        "startedAtUtc": utc_now(),
        "command": command,
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "git": git_state(root),
        "exitCode": None,
        "errors": errors,
    }
    log_path = directory / "build.log"
    inputs = source_inputs(root, exclude)
    receipt["source"] = {"files": inputs, "digest": source_digest(inputs), "excluded": exclude}
    receipt["identity"] = []
    for name in identities:
        path = Path(name) if Path(name).is_absolute() else root / name
        if path.is_file():
            entry = describe(root, path)
            entry.pop("modifiedAtNs")
            receipt["identity"].append(entry)
        else:
            errors.append(f"identity file is missing: {name}")
    out.line(f"building in {root}; output goes to {relative(root, log_path)}")
    started_ns = time.time_ns()
    with log_path.open("xb") as log:
        try:
            completed = subprocess.run(
                command, cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT
            )
            receipt["exitCode"] = completed.returncode
        except OSError as error:
            log.write(f"cannot start {command[0]}: {error}\n".encode())
            receipt["exitCode"] = 127
    receipt["finishedAtUtc"] = utc_now()
    if receipt["exitCode"] != 0:
        errors.append(f"the build exited with code {receipt['exitCode']}")

    receipt["artifacts"] = []
    for pattern, paths in glob(root, artifacts).items():
        if not paths:
            errors.append(f"no artifact matches {pattern}")
        for path in paths:
            entry = describe(root, path)
            entry["fresh"] = entry["modifiedAtNs"] >= started_ns
            receipt["artifacts"].append(entry)
            if not entry["fresh"]:
                errors.append(f"{entry['path']} was not written by this build")
    totals = dict.fromkeys(COUNTS, 0)
    tests: list[dict[str, Any]] = []
    for pattern, paths in glob(root, reports).items():
        if not paths:
            errors.append(f"no JUnit report matches {pattern}")
        for path in paths:
            entry = describe(root, path)
            entry["fresh"] = entry["modifiedAtNs"] >= started_ns
            try:
                entry.update(junit_counts(path))
            except (ET.ParseError, ValueError) as error:
                errors.append(f"{entry['path']}: {error}")
                continue
            for key in COUNTS:
                totals[key] += entry[key]
            tests.append(entry)
            if not entry["fresh"]:
                errors.append(f"{entry['path']} is a report from an earlier build")
    if reports:
        if totals["tests"] <= 0:
            errors.append("the JUnit reports hold no tests")
        if totals["failures"] or totals["errors"]:
            errors.append(f"{totals['failures']} failures and {totals['errors']} errors in tests")
        if totals["skipped"] and not allow_skipped:
            errors.append(f"{totals['skipped']} tests were skipped (--allow-skipped accepts that)")
    receipt["tests"] = {"totals": totals, "reports": tests}

    after = source_inputs(root, exclude)
    receipt["sourceUnchanged"] = after == inputs
    if after != inputs:
        changed = sorted(set(after.items()) ^ set(inputs.items()))
        names = sorted({name for name, _ in changed})
        errors.append("sources changed during the build: " + ", ".join(names[:10]))
    for entry in receipt["artifacts"] + tests:
        path = root / entry["path"]
        if not path.is_file() or sha256(path.read_bytes()) != entry["sha256"]:
            errors.append(f"{entry['path']} changed while the receipt was written")
    receipt["log"] = describe(root, log_path)
    receipt["log"].pop("modifiedAtNs")
    if not errors:
        receipt["status"] = "passed"
    path = directory / "receipt.json"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(receipt, indent=2) + "\n")
    out.result = {
        "receipt": relative(root, path),
        "status": receipt["status"],
        "exitCode": receipt["exitCode"],
        "artifacts": len(receipt["artifacts"]),
        "tests": totals,
        "errors": errors,
    }
    for message in errors:
        out.error(message)
    out.line(f"{receipt['status']}: {relative(root, path)}")
    return OK if receipt["status"] == "passed" else FINDING


def verify(args: argparse.Namespace, root: Path, out: Output) -> int:
    path = args.receipt if args.receipt.is_absolute() else Path.cwd() / args.receipt
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ToolError(USAGE, f"cannot read {args.receipt}: {error}") from None
    if receipt.get("schema") != SCHEMA:
        raise ToolError(USAGE, f"{args.receipt} is not an {SCHEMA} receipt")
    problems: list[str] = []
    if receipt.get("status") != "passed":
        problems.append("the receipt records a failed build")
    states = []
    for entry in receipt.get("artifacts", []):
        target = root / entry["path"]
        if not target.is_file():
            state = "missing"
        elif sha256(target.read_bytes()) != entry["sha256"]:
            state = "changed"
        else:
            state = "same"
        states.append({"path": entry["path"], "state": state})
        out.line(f"{state:8} {entry['path']}")
        if state != "same":
            problems.append(f"{entry['path']} is {state}")
    source = receipt.get("source", {})
    current = source_inputs(root, list(source.get("excluded", [])))
    same_source = source_digest(current) == source.get("digest")
    if not args.artifacts_only:
        out.line(f"{'same' if same_source else 'changed':8} sources")
        if not same_source:
            recorded = source.get("files", {})
            names = sorted(
                {n for n in set(recorded) | set(current) if recorded.get(n) != current.get(n)}
            )
            problems.append("sources differ from the receipt's: " + ", ".join(names[:10]))
    out.result = {
        "receipt": str(args.receipt),
        "status": receipt.get("status"),
        "artifacts": states,
        "sourceSame": same_source,
        "problems": problems,
    }
    for problem in problems:
        out.error(problem)
    return FINDING if problems else OK


def build_parser() -> cli.Parser:
    parser = cli.parser(
        "orthrus-receipt",
        (__doc__ or "").splitlines()[0],
        epilog="Settings and the receipt's fields: docs/receipt.md.",
    )
    commands = cli.subcommands(parser)
    common = cli.Parser(add_help=False)
    common.add_argument(
        "--root", type=Path, metavar="DIR", help="project root (default: the Git work tree)"
    )
    run = commands.add_parser("run", help="run a build and write its receipt", parents=[common])
    run.add_argument(
        "--run-id", required=True, metavar="ID", help="a fresh name for this build: build-001"
    )
    run.add_argument("--out", type=Path, metavar="DIR", help="receipts folder (.local/receipts)")
    run.add_argument(
        "--artifact", action="append", default=[], metavar="GLOB", help="a file the build makes"
    )
    run.add_argument(
        "--junit", action="append", default=[], metavar="GLOB", help="JUnit XML it writes"
    )
    run.add_argument(
        "--identity",
        action="append",
        default=[],
        metavar="FILE",
        help="a file the build depends on outside the sources, such as a JDK release file",
    )
    run.add_argument(
        "--exclude", action="append", default=[], metavar="DIR", help="leave out of the sources"
    )
    run.add_argument("--allow-skipped", action="store_true", help="accept skipped tests")
    run.add_argument(
        "build", nargs=argparse.REMAINDER, metavar="-- COMMAND", help="the build, after the options"
    )
    check = commands.add_parser(
        "verify", help="check files on disk against a receipt", parents=[common]
    )
    check.add_argument("receipt", type=Path, metavar="RECEIPT", help="a receipt.json")
    check.add_argument("--artifacts-only", action="store_true", help="do not compare the sources")
    return parser


def run(args: argparse.Namespace, out: Output) -> int:
    root = (args.root or project_root()).resolve()
    if not root.is_dir():
        raise ToolError(USAGE, f"not a directory: {root}")
    if args.command == "run":
        return record(args, root, out)
    return verify(args, root, out)


def main(argv: list[str] | None = None) -> int:
    return cli.run("receipt", build_parser, run, argv)


if __name__ == "__main__":
    sys.exit(main())
