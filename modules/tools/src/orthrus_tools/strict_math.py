"""Keep deterministic Java code on platform-independent math: inexact ``Math`` calls must be ``StrictMath``.

``java.lang.Math`` may use CPU- and JDK-specific intrinsics for transcendental functions (sin,
cos, exp, pow, atan2, tanh, cbrt, log...), which the specification lets differ from
``StrictMath`` by an ulp. One JVM stays consistent with itself, so a determinism check on one
machine cannot see the difference, but another CPU or JDK build can produce different output.
``StrictMath`` is bit-identical everywhere.

Only ``Math`` methods whose result the specification fixes exactly (min, floor, sqrt, ...) are
allowed. Existing calls can be recorded per file in a baseline, a ratchet: a file may not add
calls beyond its baseline. Converting a recorded call to ``StrictMath`` can change output, so it
belongs in a commit that also updates whatever golden data the project keeps.

    orthrus-strict-math                      # check against the baseline
    orthrus-strict-math --init               # record the current calls as the first baseline
    orthrus-strict-math --update             # shrink the baseline after removing calls
    orthrus-strict-math --sources a/src --sources b/src --baseline strict-math.json

Settings (``orthrus.toml`` table ``[strict-math]`` or ``[tool.orthrus.strict-math]``):
``sources`` (default ``["src/main/java"]``) and ``baseline`` (default
``strict-math-baseline.json``), both relative to the project root.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from . import cli
from .cli import FINDING, OK, USAGE, Output, ToolError
from .config import project_root, tool_settings

DEFAULT_SOURCES = ["src/main/java"]
DEFAULT_BASELINE = "strict-math-baseline.json"

# Math methods whose results the Java specification defines exactly, so every platform agrees.
EXACT = frozenset(
    """abs absExact addExact ceil ceilDiv ceilDivExact ceilMod clamp copySign decrementExact
    floor floorDiv floorDivExact floorMod fma getExponent incrementExact max min multiplyExact
    multiplyFull multiplyHigh negateExact nextAfter nextDown nextUp rint round scalb signum sqrt
    subtractExact toDegrees toIntExact toRadians ulp unsignedMultiplyHigh""".split()
)
# Fields such as Math.PI and Math.E are constants and always allowed.
CALL = re.compile(r"(?<![\w.])(?:java\s*\.\s*lang\s*\.\s*)?Math\s*(?:\.\s*(\w+)\s*\(|::\s*(\w+))")
STATIC_IMPORT = re.compile(r"^[ \t]*import\s+static\s+java\.lang\.Math\.", re.M)
COMMENT_OR_STRING = re.compile(
    r'//[^\n]*|/\*.*?\*/|"""(?:\\.|[^\\])*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S
)

Calls = dict[str, dict[str, int]]


def strip(text: str) -> str:
    """Code with comments and literals blanked, so mentions in them are not calls."""
    return COMMENT_OR_STRING.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


def inexact_calls(text: str) -> tuple[Counter[str], list[int]]:
    """Inexact Math methods called in Java source text, and lines that import Math statically."""
    code = strip(text)
    calls: Counter[str] = Counter()
    for match in CALL.finditer(code):
        name = match.group(1) or match.group(2)
        if name not in EXACT:
            calls[name] += 1
    imports = [code.count("\n", 0, m.start()) + 1 for m in STATIC_IMPORT.finditer(code)]
    return calls, imports


def scan(root: Path, sources: list[str]) -> tuple[Calls, list[str], list[str]]:
    """Inexact calls by root-relative file, static imports to report, and missing source roots."""
    found: Calls = {}
    imports: list[str] = []
    missing: list[str] = []
    for source in sources:
        directory = root / source
        if not directory.is_dir():
            missing.append(source)
            continue
        for path in sorted(directory.rglob("*.java")):
            relative = path.relative_to(root).as_posix()
            calls, lines = inexact_calls(path.read_text(encoding="utf-8", errors="replace"))
            if calls:
                found[relative] = dict(sorted(calls.items()))
            imports += [f"{relative}:{line}" for line in lines]
    return found, imports, missing


def compare(found: Calls, baseline: Calls) -> tuple[list[str], list[str]]:
    """(calls beyond the baseline, baseline entries no longer used)."""
    added, spare = [], []
    for file in sorted(set(found) | set(baseline)):
        now, allowed = found.get(file, {}), baseline.get(file, {})
        for name in sorted(set(now) | set(allowed)):
            n, a = now.get(name, 0), allowed.get(name, 0)
            if n > a:
                added.append(f"{file}: Math.{name} x{n - a} (use StrictMath.{name})")
            elif n < a:
                spare.append(f"{file}: Math.{name} {a} -> {n}")
    return added, spare


def load_baseline(path: Path) -> Calls | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["files"]


def write_baseline(path: Path, found: Calls) -> None:
    document = {
        "comment": "Inexact java.lang.Math calls recorded by orthrus-strict-math, by file. "
        "Converting one to StrictMath can change output; shrink with --update.",
        "files": found,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


def build_parser() -> cli.Parser:
    parser = cli.parser(
        "orthrus-strict-math", __doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root", type=Path, metavar="DIR", help="project root (default: the Git work tree)"
    )
    parser.add_argument(
        "--sources", action="append", metavar="DIR", help="Java source root, repeatable"
    )
    parser.add_argument(
        "--baseline", metavar="FILE", help="baseline JSON path relative to the root"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="record the current calls")
    mode.add_argument("--update", action="store_true", help="shrink the baseline to the tree")
    return parser


def run(args: argparse.Namespace, out: Output) -> int:
    root = (args.root or project_root()).resolve()
    settings = tool_settings(root, "strict-math")
    sources = args.sources or settings.get("sources") or DEFAULT_SOURCES
    baseline_path = root / (args.baseline or settings.get("baseline") or DEFAULT_BASELINE)
    out.command = "init" if args.init else "update" if args.update else "check"

    found, imports, missing = scan(root, sources)
    total = sum(sum(c.values()) for c in found.values())
    result: dict[str, object] = {
        "baseline": str(baseline_path),
        "missingSources": missing,
        "calls": total,
        "files": len(found),
    }
    out.result = result
    if missing and len(missing) == len(sources):
        raise ToolError(USAGE, "strict math: no source root exists: " + ", ".join(missing))
    if imports:
        result["staticImports"] = imports
        raise ToolError(
            FINDING,
            "Static imports of java.lang.Math hide which methods are called; use Math. or "
            "StrictMath. explicitly:\n  " + "\n  ".join(imports),
        )
    baseline = load_baseline(baseline_path)
    if args.init:
        if baseline is not None:
            raise ToolError(USAGE, f"strict math: {baseline_path} exists; use --update")
        write_baseline(baseline_path, found)
        out.line(f"strict math: baseline records {total} call(s) in {len(found)} file(s)")
        return OK
    added, spare = compare(found, baseline or {})
    result["added"] = added
    result["spare"] = spare
    if args.update:
        if baseline is None:
            raise ToolError(USAGE, "strict math: no baseline yet; use --init")
        if added:
            raise ToolError(
                FINDING,
                "--update only shrinks the baseline; new inexact calls must use StrictMath:\n  "
                + "\n  ".join(added),
            )
        write_baseline(baseline_path, found)
        out.line(f"strict math: baseline holds {total} call(s) in {len(found)} file(s)")
        return OK
    if added:
        raise ToolError(
            FINDING,
            "Inexact java.lang.Math calls can differ by CPU and JDK and so change output on "
            "another machine; use StrictMath:\n  " + "\n  ".join(added),
        )
    if spare:
        out.note(
            "strict math: fewer recorded calls remain; run orthrus-strict-math --update:\n  "
            + "\n  ".join(spare)
        )
    for source in missing:
        out.warn(f"strict math: source root {source} does not exist")
    out.line("strict math: no new inexact Math calls")
    return OK


def main(argv: list[str] | None = None) -> int:
    return cli.run("strict-math", build_parser, run, argv)


if __name__ == "__main__":
    sys.exit(main())
