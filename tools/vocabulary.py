"""The vocabulary (docs/vocabulary.md) checked against what Workbench says about itself: the help of every `wb` verb
and option (sdk/python/wb/cli.py's parser) and every module's and command's summary (modules/*/wb_*/module.toml).

    python tools/vocabulary.py            list every help text this checks, by where it is
    python tools/vocabulary.py --check    exit 1, naming each place, when help uses a word the vocabulary retired
                                          or a word in a sense it does not have here

docs/vocabulary.md holds two tables this reads. "Retired words" (| Retired | Say instead | Kept in |) lists, in
backticks, phrases that name what the vocabulary replaced; "One meaning each" (| Word | Not in the sense of | Means
here | Kept in |) lists words the vocabulary defines, with phrases that use them in another sense. A phrase matches
case-insensitively, as whole words. "Kept in" names, in backticks outside its parentheses, the places that may still
use them: a verb such as `wb check` (its options too), a command such as `ci.run`, or a module such as `lab` (its
summary). A use anywhere else fails the check, and so does a "Kept in" place that no longer uses the phrase, so the
list only shrinks."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VOCABULARY = "docs/vocabulary.md"
TABLES = {"Retired words": "retired", "One meaning each": "sense"}


@dataclass
class Rule:
    kind: str                     # retired or sense
    word: str                     # the row's first cell
    phrases: list[str]
    instead: str                  # what to say: the replacement, or the word's meaning here
    kept: list[str] = field(default_factory=list)

    def pattern(self) -> re.Pattern[str]:
        def one(phrase: str) -> str:
            body = r"\s+".join(re.escape(part) for part in phrase.split())
            head = r"\b" if phrase[:1].isalnum() else ""
            tail = r"\b" if phrase[-1:].isalnum() else ""
            return head + body + tail
        return re.compile("|".join(one(p) for p in self.phrases), re.IGNORECASE)


def _ticks(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def _places(cell: str) -> list[str]:
    """The places a "Kept in" cell names: its backticked words outside the parentheses that say why."""
    return _ticks(re.sub(r"\([^)]*\)", "", cell))


def rules(repo: Path = REPO) -> list[Rule]:
    """Every rule of the vocabulary's two tables."""
    found, kind = [], None
    for line in (repo / VOCABULARY).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            kind = TABLES.get(line[3:].strip())
            continue
        if kind is None or not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] in ("Retired", "Word"):
            continue  # a header row
        if kind == "retired" and len(cells) == 3:  # | Retired | Say instead | Kept in |
            phrases = _ticks(cells[0])
            found.append(Rule(kind, phrases[0], phrases, cells[1], _places(cells[2])))
        elif kind == "sense" and len(cells) == 4:  # | Word | Not in the sense of | Means here | Kept in |
            found.append(Rule(kind, cells[0].strip("`"), _ticks(cells[1]), cells[2], _places(cells[3])))
        else:
            raise SystemExit(f"{VOCABULARY}: a row of the {kind} table has {len(cells)} cells: {line}")
    return found


def _parser():
    """This checkout's ``wb`` parser (``repo`` only moves the files this reads: the vocabulary and the manifests)."""
    sdk = str(REPO / "sdk" / "python")
    if sdk not in sys.path:
        sys.path.insert(0, sdk)
    from wb import cli
    return cli.build_parser()


def help_texts(repo: Path = REPO) -> list[tuple[str, str]]:
    """(where, text) for every verb's help and its options' and arguments', then every module and command summary."""
    found: list[tuple[str, str]] = []
    parser = _parser()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        helps = {choice.dest: choice.help for choice in action._choices_actions}
        for verb, sub in action.choices.items():
            if not helps.get(verb) or helps[verb] == argparse.SUPPRESS:
                continue  # a hidden verb: an old name kept for a release, which says nothing of its own
            found.append((f"wb {verb}", str(helps[verb])))
            for option in sub._actions:
                if option.help and option.help != argparse.SUPPRESS and option.dest != "help":
                    found.append((f"wb {verb} {option.option_strings[-1] if option.option_strings else option.dest}",
                                  str(option.help)))
    for path in sorted(repo.glob("modules/*/wb_*/module.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        module = data["module"]["name"]
        found.append((module, str(data["module"].get("summary", ""))))
        for command in data.get("command") or []:
            found.append((f"{module}.{command['name']}", str(command.get("summary", ""))))
    return found


def _kept(where: str, kept: list[str]) -> bool:
    return any(where == place or where.startswith(place + " ") for place in kept)


def findings(repo: Path = REPO) -> list[str]:
    """Each use of a retired word or another sense outside its kept places, and each kept place that no longer
    uses it."""
    out = []
    texts = help_texts(repo)
    for rule in rules(repo):
        pattern, used = rule.pattern(), set()
        for where, text in texts:
            match = pattern.search(text)
            if not match:
                continue
            for place in rule.kept:
                if where == place or where.startswith(place + " "):
                    used.add(place)
            if _kept(where, rule.kept):
                continue
            if rule.kind == "retired":
                out.append(f"{where}: '{match.group(0)}' is retired ({rule.word}); say {rule.instead}")
            else:
                out.append(f"{where}: '{match.group(0)}' uses {rule.word} in another sense; here it means "
                           f"{rule.instead}")
        for place in rule.kept:
            if place not in used:
                out.append(f"{VOCABULARY}: {rule.word} is kept in {place}, which no longer uses it; take it out of "
                           "Kept in")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check help text against the vocabulary.")
    parser.add_argument("--check", action="store_true", help="exit 1 when help uses a word as the vocabulary forbids")
    args = parser.parse_args(argv)
    if not args.check:
        for where, text in help_texts():
            print(f"{where}: {text}")
        return 0
    found = findings()
    for line in found:
        print(line)
    print(f"{len(found)} finding(s) against {VOCABULARY}" if found else f"help text agrees with {VOCABULARY}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
