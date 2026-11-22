r"""Declarative plans (spec M5): a turn or a campaign described once, checked before any GPU time is spent,
and run on a chosen machine.

    [plan]
    name = "phase2-r1"
    summary = "River in forest view 07, textures held still"
    protocol = "Settle, then the reference; stop at the first failure. Never score against an instance's first run."
    machine = "host"                       # host (the RTX 3060 through wb-host) or rental
    turn = true                            # consecutive host steps share one GPU lease

    [inputs.jar]                           # pinned: a drifted input refuses the plan
    path = 'D:\\work\\build\\quadrature-0.1.0-dev.jar'  (scrub: allow)
    sha256 = "..."

    [[step]]
    id = "prepare"
    command = "lab.prepare"
    params = { name = "quadrature-river-01", seed = 2485331, scale = 4, args = ["--client-mod", "{inputs.jar}"] }

    [[step]]
    id = "settle"
    command = "lab.play"
    params = { name = "quadrature-river-01", tour = 'D:\work\tours\forests-2485331.json', label = "r1-settle-game" }  (scrub: allow)

    [[step]]
    id = "score"
    command = "frames.compare"
    params = { reference = "{steps.settle.outputs.r1-settle-game-captures}", candidate = "...", profile = "near" }
    accept = ["ok", "finding"]             # outcomes that let the plan go on (default: ok)

    [rental]                               # for machine = "rental": how the game steps run on a rented GPU
    offer = "29811734"
    max_dph = 0.10
    cap = "90m"
    lab = 'D:\\work\\linux-lab'   # the four tars of linux-lab/pack.sh  (scrub: allow)
    window = "1280x720"
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REF = re.compile(r"\{([a-zA-Z_][\w.\-]*)\}")


def role(registry: dict, command: str) -> str | None:
    """What a command declares itself to a rental (module.toml ``role``): prepare, play, or nothing."""
    return (registry.get(command) or {}).get("role")


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
    machine: str
    turn: bool
    inputs: dict[str, dict]
    steps: list[Step]
    rental: dict
    source: Path | None = None


def load(path: Path) -> Plan:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PlanError(f"{path}: {error}") from None
    return parse(data, Path(path))


def parse(data: dict, source: Path | None = None) -> Plan:
    head = data.get("plan") or {}
    unknown = set(data) - {"plan", "inputs", "step", "rental"}
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
    machine = head.get("machine", "host")
    if machine not in ("host", "rental"):
        raise PlanError("plan.machine must be host or rental")
    return Plan(name=head.get("name") or (source.stem if source else "plan"), summary=head.get("summary", ""),
                protocol=head.get("protocol", ""), machine=machine, turn=bool(head.get("turn", True)),
                inputs=dict(data.get("inputs") or {}), steps=steps, rental=dict(data.get("rental") or {}),
                source=source)


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


def plan_env(plan: Plan, jar_names: dict[str, str], tour_names: dict[str, str],
             registry: dict) -> tuple[str, list[str]]:
    """The riven-tours plan.env for a rental: one prepare and its plays (calibration 01's shape)."""
    prepares = [s for s in plan.steps if role(registry, s.command) == "prepare"]
    plays = [s for s in plan.steps if role(registry, s.command) == "play"]
    if len(prepares) != 1 or not plays:
        raise PlanError("a rental plan needs exactly one prepare step and at least one play step (commands whose "
                        "module.toml declares role = \"prepare\" or \"play\")")
    prepare = prepares[0]
    names = {s.params.get("name") for s in prepares + plays}
    if len(names) != 1:
        raise PlanError("on a rental every game step uses the one instance it prepares")
    args = [jar_names.get(str(a), str(a)) for a in prepare.params.get("args", [])]
    lines = [f"# generated by Workbench from plan {plan.name}",
             f"NAME={prepare.params['name']}",
             f"SEED={prepare.params.get('seed', 0)}",
             f"SCALE={prepare.params.get('scale', 1)}",
             f'PREPARE_ARGS="{" ".join(args)}"',
             f'TOURS="{" ".join(tour_names[str(p.params["tour"])] for p in plays)}"',
             f'LABELS="{" ".join(p.params.get("label", p.id) for p in plays)}"']
    for index, play in enumerate(plays, start=1):
        lines.append(f'OPTIONS_{index}="{play.params.get("options", "")}"')
    lines += [f"WINDOW={plan.rental.get('window', plays[0].params.get('window', '1920x1080'))}", "GPU=1"]
    return "\n".join(lines) + "\n", [p.params.get("label", p.id) for p in plays]
