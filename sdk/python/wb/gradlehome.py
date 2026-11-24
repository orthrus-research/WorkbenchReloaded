"""The block of the user's Gradle home that lets a hand build or an IDE sync in a worktree resolve its set.

A plain ./gradlew in a worktree reads the folder's .set and needs to know where the set exports, the Store and its
plugins are, and which JDK builds the set's platform names. Workbench writes those into one marked block of the Gradle
home's gradle.properties:

    set.home=<the exports of the Set, one folder per revision>
    set.store=<the Store>
    set.plugins=<the Store's plugins folder, as a file URI>
    org.gradle.java.installations.paths=<the platform's JDKs in the Store, and this machine's own>

The block names no revision, so no landing rewrites it; it changes only when a JDK build enters the Store. Every line
outside Workbench's blocks is left as it was. JDK auto-detection and auto-download are left as the person has them:
a project not yet converted may rely on them. When Workbench's own JDK block (toolchain.gradle-home) is there, the
platform's JDKs join its list instead of a second, conflicting line."""

from __future__ import annotations

import os
from pathlib import Path

BEGIN = "# >>> Workbench: the set (wb work sync)"
END = "# <<< Workbench: the set"
JDK_BEGIN = "# >>> Workbench: this machine's JDKs (toolchain.gradle-home)"
JDK_END = "# <<< Workbench"
PATHS = "org.gradle.java.installations.paths"


def home_folder() -> Path:
    return Path(os.environ.get("GRADLE_USER_HOME") or Path.home() / ".gradle")


def _span(text: str, begin: str, end: str) -> tuple[int, int] | None:
    start = text.find(begin)
    if start == -1:
        return None
    stop = text.find(end, start + len(begin))
    if stop == -1:
        return None
    tail = text.find("\n", stop)
    return start, (tail + 1 if tail != -1 else len(text))


def _paths_in(block: str) -> list[str]:
    for line in block.splitlines():
        if line.startswith(PATHS + "="):
            return [p for p in line.split("=", 1)[1].split(",") if p]
    return []


def merged(existing: str, values: dict[str, str], jdks: list[str]) -> tuple[str, list[str]]:
    """The properties text with Workbench's set block written (and its JDK block's list grown), and what it could not
    do: a JDK list set by a line of the person's own is left alone and reported."""
    notes = []
    jdks = list(dict.fromkeys(Path(j).as_posix() for j in jdks))
    jdk_span = _span(existing, JDK_BEGIN, JDK_END)
    set_span = _span(existing, BEGIN, END)
    outside = existing
    for span in sorted([s for s in (jdk_span, set_span) if s], reverse=True):
        outside = outside[:span[0]] + outside[span[1]:]
    own_paths = any(line.strip().startswith(PATHS + "=") for line in outside.splitlines())
    lines = [BEGIN] + [f"{key}={value}" for key, value in values.items()]
    if jdk_span:
        block = existing[jdk_span[0]:jdk_span[1]]
        grown = list(dict.fromkeys(_paths_in(block) + jdks))
        new_block = "\n".join(f"{PATHS}={','.join(grown)}" if line.startswith(PATHS + "=") else line
                              for line in block.rstrip("\n").splitlines()) + "\n"
        existing = existing[:jdk_span[0]] + new_block + existing[jdk_span[1]:]
        set_span = _span(existing, BEGIN, END)
    elif own_paths:
        notes.append(f"{PATHS} is set by a line of your own, so the platform's JDKs were not added: add "
                     + ",".join(jdks))
    elif jdks:
        lines.append(f"{PATHS}={','.join(jdks)}")
    text = "\n".join(lines + [END]) + "\n"
    if set_span:
        return existing[:set_span[0]] + text + existing[set_span[1]:], notes
    separator = "" if not existing or existing.endswith("\n") else "\n"
    return existing + separator + text, notes


def values_for(workspace) -> tuple[dict[str, str], list[str]]:
    """The set block's values on this machine, and the JDK homes: the platform's builds in the Store for this
    operating system, then the machine's own ([runner] jdks)."""
    store = workspace.store_root()
    if store is None:
        return {}, []
    values = {"set.home": (store.parent / "sets").as_posix(), "set.store": store.as_posix(),
              "set.plugins": (store / "plugins").resolve().as_uri()}
    system = "windows" if os.name == "nt" else "linux"
    jdks = [p for p in sorted((store / "jdks").glob(f"*/{system}/*")) if p.is_dir()]
    runner = (workspace.values.get("runner") or {}).get("jdks") or {}
    jdks += [Path(str(runner[k])) for k in sorted((k for k in runner if str(k).isdigit()), key=int)]
    return values, [str(p) for p in jdks]


def ensure(workspace, say=print) -> bool:
    """Writes the set block into the Gradle home when it differs; True when the file changed."""
    values, jdks = values_for(workspace)
    if not values:
        return False
    target = home_folder() / "gradle.properties"
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    text, notes = merged(existing, values, jdks)
    for note in notes:
        say(note)
    if text == existing:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + f".{os.getpid()}.part")
    part.write_text(text, encoding="utf-8")
    part.replace(target)
    say(f"{target}: the set block written (set.home, set.store, set.plugins, {len(jdks)} JDK(s))")
    return True
