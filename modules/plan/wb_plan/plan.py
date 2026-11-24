r"""Declarative plans (spec M5): a turn or a campaign described once, checked before any GPU time is spent,
and run on the host.

    [plan]
    name = "river-r1"
    summary = "A river view, textures held still"
    protocol = "Noise first, then the score; stop at the first failure. Never score against an instance's first run."
    turn = true                            # consecutive turn routines share one GPU lease

    [inputs.reference]                     # pinned: a drifted input refuses the plan
    path = '/srv/captures/river-reference.png'
    sha256 = "..."

    [inputs.candidate]
    path = '/srv/captures/river-candidate.png'
    sha256 = "..."

    [[step]]
    id = "noise"
    command = "frames.check"
    params = { run = "{inputs.candidate}", other = "{inputs.reference}" }

    [[step]]
    id = "score"
    command = "frames.compare"
    params = { reference = "{inputs.reference}", candidate = "{inputs.candidate}", profile = "near" }
    accept = ["ok", "finding"]             # outcomes that let the plan go on (default: ok)

A step's parameters may name an earlier step's results: ``{steps.<id>.outputs.<name>}``, ``{steps.<id>.data.<key>}``
or ``{steps.<id>.job}``.

A plan runs on the host. The rental route (``machine = "rental"``, a ``[rental]`` table, game steps written into a
bundle from a template) is retired: a rental bundle is made from a launch file alone (``vast.bundle-launch``) and run
with ``vast.rent``.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REF = re.compile(r"\{([a-zA-Z_][\w.\-]*)\}")


def turnable(registry: dict, command: str) -> bool:
    """Whether a command declares it may run inside a turn (module.toml ``turn = true``)."""
    return bool((registry.get(command) or {}).get("turn"))


class PlanError(Exception):
    pass


@dataclass
class Step:
    id: str
    command: str
    params: dict
    accept: list[str] = field(default_factory=lambda: ["ok"])


@dataclass
class Plan:
    name: str
    summary: str
    protocol: str
    turn: bool
    inputs: dict[str, dict]
    steps: list[Step]
    source: Path | None = None


def load(path: Path) -> Plan:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PlanError(f"{path}: {error}") from None
    return parse(data, Path(path))


# A plan that would rent: what replaces the route it used.
RENTAL_RETIRED = ("rental plans are retired: make a rental bundle from a launch file with vast.bundle-launch and run "
                  "it with vast.rent")


def parse(data: dict, source: Path | None = None) -> Plan:
    head = data.get("plan") or {}
    if "rental" in data or head.get("machine", "host") == "rental":
        raise PlanError(RENTAL_RETIRED)
    unknown = set(data) - {"plan", "inputs", "step"}
    if unknown:
        raise PlanError(f"unknown tables {sorted(unknown)}")
    steps = []
    for index, raw in enumerate(data.get("step", []), start=1):
        extra = set(raw) - {"id", "command", "params", "accept"}
        if extra:
            raise PlanError(f"step {index}: unknown keys {sorted(extra)}")
        if not raw.get("command"):
            raise PlanError(f"step {index}: needs a command")
        accept = raw.get("accept", ["ok"])
        if set(accept) - {"ok", "finding"}:
            raise PlanError(f"step {index}: accept may hold only ok and finding")
        steps.append(Step(id=str(raw.get("id") or f"step-{index}"), command=raw["command"],
                          params=dict(raw.get("params") or {}), accept=list(accept)))
    if head.get("machine", "host") != "host":
        raise PlanError("plan.machine must be host")
    return Plan(name=head.get("name") or (source.stem if source else "plan"), summary=head.get("summary", ""),
                protocol=head.get("protocol", ""), turn=bool(head.get("turn", True)),
                inputs=dict(data.get("inputs") or {}), steps=steps, source=source)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def references(value) -> list[str]:
    if isinstance(value, str):
        return REF.findall(value)
    if isinstance(value, list):
        return [r for v in value for r in references(v)]
    if isinstance(value, dict):
        return [r for v in value.values() for r in references(v)]
    return []


def substitute(value, lookup):
    if isinstance(value, str):
        whole = REF.fullmatch(value)
        if whole:
            return lookup(whole.group(1))
        return REF.sub(lambda m: str(lookup(m.group(1))), value)
    if isinstance(value, list):
        return [substitute(v, lookup) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, lookup) for k, v in value.items()}
    return value


def groups(plan: Plan, registry: dict) -> list[list[Step]]:
    """Steps in jobs: with ``turn``, consecutive turnable routines run as one turn under one lease."""
    out: list[list[Step]] = []
    for step in plan.steps:
        if plan.turn and turnable(registry, step.command) and out and turnable(registry, out[-1][0].command):
            out[-1].append(step)
        else:
            out.append([step])
    return out
