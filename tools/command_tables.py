"""The command tables of README.md and docs/commands.md, generated from the module manifests
(modules/*/wb_*/module.toml): each module's summary and command names, and each command's summary and where it runs.

    python tools/command_tables.py            rewrite the generated sections
    python tools/command_tables.py --check    exit 1, naming each file, when a section differs from the manifests

A generated section lies between a begin and an end marker line; everything outside them is hand-written and kept.
A hand edit inside a section, or a command added to a manifest without running this, fails the check (and the test
that runs it)."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BEGIN = "<!-- begin generated {name}: python tools/command_tables.py writes this table from the module manifests -->"
END = "<!-- end generated {name} -->"
RUNS_ON = {"container": "container", "host": "host"}


def manifests(repo: Path = REPO) -> list[dict]:
    """Every module's manifest, by module name."""
    found = []
    for path in sorted(repo.glob("modules/*/wb_*/module.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        found.append({"name": data["module"]["name"], "summary": data["module"].get("summary", ""),
                      "commands": data.get("command") or [], "path": path.relative_to(repo).as_posix()})
    return sorted(found, key=lambda m: m["name"])


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def modules_table(found: list[dict]) -> str:
    lines = ["| Module | What it does | Commands |", "|---|---|---|"]
    for module in found:
        names = ", ".join(f"`{c['name']}`" for c in module["commands"]) or "none (aliases only)"
        lines.append(f"| `{module['name']}` | {_cell(module['summary'])} | {names} |")
    return "\n".join(lines)


def commands_table(found: list[dict]) -> str:
    parts = []
    for module in found:
        parts.append(f"### `{module['name']}`\n\n{_cell(module['summary'])} ({module['path']})\n")
        if not module["commands"]:
            parts.append("No commands of its own.\n")
            continue
        rows = ["| Command | Runs on | Summary |", "|---|---|---|"]
        for command in module["commands"]:
            where = ", ".join(RUNS_ON.get(m, m) for m in command.get("runs_on") or ["container"])
            aliases = command.get("aliases") or []
            also = f" Also as {', '.join(f'`{a}`' for a in aliases)}." if aliases else ""
            rows.append(f"| `{module['name']}.{command['name']}` | {where} | {_cell(command.get('summary', ''))}"
                        f"{also} |")
        parts.append("\n".join(rows) + "\n")
    return "\n".join(parts).rstrip("\n")


SECTIONS = {
    "README.md": {"modules": modules_table},
    "docs/commands.md": {"commands": commands_table},
}


def render(text: str, name: str, body: str, file: str) -> str:
    begin, end = BEGIN.format(name=name), END.format(name=name)
    if begin not in text or end not in text:
        raise SystemExit(f"{file}: no generated section {name} (its marker lines are missing)")
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{begin}\n{body}\n{end}{tail}"


def expected(repo: Path = REPO) -> dict[str, str]:
    """Each file's text with its sections as the manifests make them."""
    found = manifests(repo)
    out = {}
    for file, sections in SECTIONS.items():
        text = (repo / file).read_text(encoding="utf-8")
        for name, make in sections.items():
            text = render(text, name, make(found), file)
        out[file] = text
    return out


def stale(repo: Path = REPO) -> list[str]:
    return [file for file, text in expected(repo).items() if (repo / file).read_text(encoding="utf-8") != text]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or check the command tables generated from the manifests.")
    parser.add_argument("--check", action="store_true", help="change nothing; exit 1 when a table is stale")
    args = parser.parse_args(argv)
    if args.check:
        files = stale()
        for file in files:
            print(f"{file}: its command table differs from the module manifests; run python tools/command_tables.py")
        if not files:
            print("command tables match the manifests")
        return 1 if files else 0
    for file, text in expected().items():
        (REPO / file).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {', '.join(SECTIONS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
