"""set.json, schema 1: which commit of each project builds together, with the files its build made there.

    projects.<name>  = {url?, branch, commit, artifacts: {<artifact>: {sha256, file, glob, check}}}
    third.<name>     = a third-party file by coordinate or URL with its SHA-256 and licence
    platform         = {java, jdk: {<major>: {build, linux: {url, sha256}, windows: {url, sha256}}}, gradle, ...}
    holds.<name>     = one artifact, or one consumer's view of a project, on another commit, with a reason
    doctor.exempt    = paths the copied-pin check leaves alone

Only a landing writes it. Exports of a revision live beside the Store, ``<out>/sets/<revision>/set.json``, never
pruned, so a job reads exactly the revision its caller resolved."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TOP = {"schema", "projects", "third", "platform", "holds", "doctor"}


class SetError(Exception):
    pass


def parse(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError as error:
        raise SetError(f"set.json does not parse: {error}") from None
    problems = check(data)
    if problems:
        raise SetError("set.json: " + "; ".join(problems[:5]))
    return data


def check(data) -> list[str]:
    if not isinstance(data, dict):
        return ["not an object"]
    found = []
    if data.get("schema") != 1:
        found.append("schema must be 1")
    found += [f"unknown key {k}" for k in sorted(set(data) - TOP)]
    for name, line in (data.get("projects") or {}).items():
        if not HEX40.match(str((line or {}).get("commit", ""))):
            found.append(f"projects.{name}.commit is not 40 hex")
        for artifact, item in ((line or {}).get("artifacts") or {}).items():
            if not HEX64.match(str((item or {}).get("sha256", ""))):
                found.append(f"projects.{name}.artifacts.{artifact}.sha256 is not 64 hex")
    return found


def dump(data: dict) -> str:
    """The canonical text: one way to write a set, so equal sets have equal blobs."""
    ordered = dict(data)
    for key in ("projects", "third", "holds"):
        ordered[key] = {name: (data.get(key) or {})[name] for name in sorted(data.get(key) or {})}
    return json.dumps(ordered, indent=1, ensure_ascii=False) + "\n"


def line(data: dict, project: str) -> dict | None:
    return (data.get("projects") or {}).get(project)


def with_lines(data: dict, lines: dict[str, dict]) -> dict:
    """``data`` with each project's line replaced by the one given."""
    changed = json.loads(json.dumps(data))
    changed.setdefault("projects", {}).update(lines)
    return changed


def export(folder: Path, revision: str, text: str) -> Path:
    """Writes the export of ``revision`` once; an existing export must hold the same text."""
    path = folder / revision / "set.json"
    if path.is_file():
        if path.read_text(encoding="utf-8") != text:
            raise SetError(f"{path} holds another set than revision {revision[:12]}")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"set.json.{os.getpid()}.part")
    part.write_text(text, encoding="utf-8")
    part.replace(path)
    return path


def write_head(folder: Path, revision: str):
    """Records which exported revision is the Set's head, so a landing that waited on the lock composes from it."""
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / f"HEAD.{os.getpid()}.part"
    part.write_text(revision + "\n", encoding="ascii")
    part.replace(folder / "HEAD")


def read_head(folder: Path) -> str | None:
    try:
        value = (folder / "HEAD").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value if HEX40.match(value) else None


def read_export(folder: Path, revision: str) -> dict:
    path = folder / revision / "set.json"
    try:
        return parse(path.read_text(encoding="utf-8"))
    except OSError:
        raise SetError(f"no export of set revision {revision[:12]} at {path}") from None
