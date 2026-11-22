"""lab: host routines (run natively by wb-host). ``play`` holds the GPU lease for its whole run and passes the
turn token down, so the launcher joins the held lease instead of taking it again.

Everything about the launcher (its commands, its instance layout, what the lab move carries) is the instance
profile (wb_lab.profile), read from ``[modules.lab] profile``; the code names no project."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

from . import profile as profiles


def _profile_name(ctx: Context) -> str:
    """The instance profile a job names (its `profile` param); empty: the default profile."""
    return str((getattr(ctx, "params", None) or {}).get("profile") or "")


def _profile(ctx: Context) -> dict:
    name = _profile_name(ctx)
    try:
        return profiles.merged(ctx.config, name)
    except ValueError as error:
        where = f"modules.lab.profiles.{name}" if name else "modules.lab.profile"
        raise EnvironmentProblem(f"{where}: {error}") from None


def _lab_items(ctx: Context) -> list[tuple[str, str]]:
    """What the launcher's lab root holds for the lab, less what must stay (``stays``: e.g. toolchains whose
    resolved paths instances record, so a link there would make every existing instance fail to load)."""
    p = _profile(ctx)
    stays = set(p["stays"])
    return [(name, what) for name, what in p["lab_items"] if name not in stays]


def _python(ctx: Context) -> str:
    import sys
    return ctx.config.get("python") or sys.executable


def _launcher(ctx: Context) -> Path:
    """The checkout whose launcher prepares and plays instances (``modules.lab.launcher``)."""
    from wb_core.service import REPO_ROOT
    value = _profile(ctx).get("checkout") or ctx.config.get("launcher")
    if not value:
        raise EnvironmentProblem("modules.lab.launcher is not set (the checkout whose launcher builds instances), "
                                 "and the instance profile names no checkout")
    path = Path(value)
    path = path if path.is_absolute() else (REPO_ROOT / path).resolve()
    script = _profile(ctx)["launcher"]
    if not (path / script).is_file():
        raise EnvironmentProblem(f"no {script} at {path} (set modules.lab.launcher, or the profile's launcher)")
    return path


def _prepare_command(ctx: Context, name: str, seed, scale, args: list) -> list[str]:
    p = _profile(ctx)
    command = profiles.command(p["prepare"], python=_python(ctx), name=name)
    for value, flag, what in ((seed, "seed_flag", "seed"), (scale, "scale_flag", "scale")):
        if value is None:
            continue
        if not p[flag]:
            raise ParamError(f"the instance profile has no {flag}, so a {what} cannot be given")
        command += [p[flag], str(value)]
    return command + [str(a) for a in args]


def _env(ctx: Context, extra: dict | None = None) -> dict:
    """The profile's environment for launcher commands ({checkout} values become paths), plus ``extra``."""
    env = {}
    for name, value in _profile(ctx)["env"].items():
        value = str(value)
        env[name] = str(_profile_path(ctx, value)) if profiles.checkout_of(value)[0] else value
    return {**env, **(extra or {})}


def gpu(ctx: Context):
    if not shutil.which("nvidia-smi"):
        raise EnvironmentProblem("nvidia-smi is not on PATH on the host")
    done = ctx.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,pcie.link.gen.current,"
                    "pcie.link.width.current", "--format=csv,noheader"], check=True, log_output=False)
    name, driver, memory, gen, width = [p.strip() for p in done.stdout.splitlines()[0].split(",")]
    ctx.machine_info(gpu=name, driver=driver)
    ctx.summary(f"{name}, driver {driver}, {memory}, PCIe gen {gen} x{width}")
    return {"gpu": name, "driver": driver, "memory": memory, "pcie": f"gen{gen} x{width}"}


def queue(ctx: Context):
    from wb_host import lease
    lab = Path(os.environ.get("WB_LAB_HOME", "."))
    settings_lease = os.environ.get("WB_LEASE_FILE")
    lock = Path(settings_lease) if settings_lease else lab / "lease" / "gpu.lock"
    coordination = Path(os.environ.get("WB_COORDINATION_DIR") or lab / "coordination")
    held = lease.occupied(lock)
    lanes = {name: {"state": e.get("state"), "waiting_since": e.get("waiting_since"), "note": e.get("note"),
                    "live": lease.live(e, __import__("time").time())}
             for name, e in lease.entries(coordination).items()}
    owner = lease.owner(lock) if held else None
    ctx.summary(f"lease {'held by ' + str((owner or {}).get('lane') or (owner or {}).get('name')) if held else 'free'}; "
                f"{sum(1 for v in lanes.values() if v['live'])} live lane(s)")
    return {"lease": {"file": str(lock), "held": held, "owner": owner}, "lanes": lanes}


def prepare(ctx: Context, name: str, args: list, seed=None, scale=None, profile: str = ""):
    launcher = _launcher(ctx)
    command = _prepare_command(ctx, name, seed, scale, args)
    ctx.progress("prepare", msg=name)
    done = ctx.run(command, cwd=launcher, env=_env(ctx), timeout=40 * 60)
    log = ctx.out / f"prepare-{name}.log"
    log.write_text(done.stdout, encoding="utf-8")
    ctx.output("prepare-log", log)
    if done.returncode != 0:
        raise EnvironmentProblem(f"prepare exited {done.returncode}: {done.stdout.splitlines()[-1:] or ''}")
    ctx.summary(f"instance {name} prepared")
    return {"instance": name, "exit": done.returncode}


def play(ctx: Context, name: str, tour: str, window: str, options: str, label: str, args: list, profile: str = ""):
    launcher, p = _launcher(ctx), _profile(ctx)
    if not os.environ.get("WB_TURN_TOKEN"):
        raise EnvironmentProblem("play must run under the GPU lease (wb-host takes it for this routine)")
    instance = _instances(ctx) / name
    if not instance.is_dir():
        raise ParamError(f"instance {name} does not exist at {instance} (run lab.prepare first)")
    command = profiles.command(p["play"], python=_python(ctx), name=name, tour=tour, window=window) + list(args)
    if options and not p.get("options_env"):
        raise ParamError("the instance profile has no options_env, so options cannot be given")
    env = _env(ctx, {p["options_env"]: options} if options else None)
    ctx.progress("play", msg=f"{Path(tour).name} as {label}")
    done = ctx.run(command, cwd=launcher, env=env, timeout=90 * 60)
    out = ctx.out
    (out / f"{label}-play.log").write_text(done.stdout, encoding="utf-8")
    ctx.output(f"{label}-play-log", out / f"{label}-play.log")
    for relative, kept_as in p["collect"]:
        source = instance / relative
        if source.is_file():
            shutil.copy2(source, out / f"{label}-{kept_as}")
            ctx.output(f"{label}-{kept_as}", out / f"{label}-{kept_as}")
    captures = re.findall(p["captures_pattern"], done.stdout) if p.get("captures_pattern") else []
    if captures and Path(captures[-1].strip()).is_dir():
        target = out / f"{label}-captures"
        shutil.copytree(captures[-1].strip(), target, dirs_exist_ok=True)
        ctx.output(f"{label}-captures", target)
        ctx.measure("captures", sum(1 for _ in target.glob("*.png")))
    if done.returncode != 0:
        collected = [out / f"{label}-{kept_as}" for _relative, kept_as in p["collect"]]
        texts = [path.read_text(encoding="utf-8", errors="replace") for path in collected if path.is_file()]
        why = explain(p["failures"], texts + [done.stdout])
        ctx.finding(f"tour {Path(tour).name} exited {done.returncode}" + (f": {why}" if why else ""))
    else:
        ctx.summary(f"tour {Path(tour).name} played on {name}")
    return {"instance": name, "exit": done.returncode, "captures": bool(captures)}


def explain(patterns: list[str], texts: list[str]) -> str | None:
    """Why a play failed, by the profile's failure patterns: tried in order, most telling first, each over every text
    (the files play collected, then its own output); the first line one finds, shortened."""
    for pattern in patterns:
        regex = re.compile(pattern)
        for text in texts:
            for line in text.splitlines():
                if regex.search(line):
                    line = line.strip()
                    return line if len(line) <= 240 else line[:237] + "..."
    return None


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def migrate_plan(ctx: Context, profile: str = ""):
    """A dry run only: moving the lab touches every project's tools and needs the user's go-ahead and a quiet
    moment (lab-and-ci-split.md, step A)."""
    launcher = _launcher(ctx)
    lab = Path(os.environ.get("WB_LAB_HOME", ""))
    local = launcher / _lab_root(ctx)
    items = []
    for name, what in _lab_items(ctx):
        source = local / name
        if source.exists():
            items.append({"item": name, "what": what, "from": str(source), "to": str(lab / name),
                          "bytes": _size(source), "link": "junction" if os.name == "nt" else "symlink"})
    plan = ctx.out / "migrate-plan.json"
    plan.write_text(json.dumps({"launcher": str(launcher), "lab_home": str(lab), "items": items}, indent=1),
                    encoding="utf-8")
    ctx.output("plan", plan)
    total = sum(i["bytes"] for i in items)
    ctx.summary(f"{len(items)} item(s), {total / 1e9:.1f} GB would move from {local} to {lab}; nothing moved")
    return {"items": items, "bytes": total, "moved": False}


def _allowed(ctx: Context, script: str) -> Path:
    path = Path(script).resolve()
    roots = [Path(r).resolve() for r in ctx.config.get("scripts") or []]
    if not any(path.is_relative_to(root) for root in roots):
        raise ParamError(f"{script} is under no folder in modules.lab.scripts "
                         f"({', '.join(map(str, roots)) or 'none allow-listed'})")
    if not path.is_file() or path.suffix.lower() not in (".py", ".ps1"):
        raise ParamError(f"{script} is not a .py or .ps1 file")
    return path


def _script_python(ctx: Context, path: Path) -> str:
    """The interpreter for a script: the environment made from the requirements files its allow-listed folder lists
    in ``modules.lab.environments`` (relative to that folder, or absolute), else the lab's own interpreter."""
    from wb_core.environments import default_cache, python_from
    for folder, requirements in (ctx.config.get("environments") or {}).items():
        root = Path(folder).resolve()
        if path.is_relative_to(root):
            files = [Path(r) if Path(r).is_absolute() else root / r for r in requirements]
            cache = Path(ctx.config.get("python_cache") or default_cache())
            return python_from(ctx, files, cache, f"modules.lab.environments for {folder}")
    return _python(ctx)


def script(ctx: Context, script: str, args: list, timeout: float = 4 * 3600):
    """``timeout`` holds even for a script that prints nothing: the run is stopped there (with whatever it started)
    and that is a finding."""
    from . import process
    path = _allowed(ctx, script)
    if path.suffix.lower() == ".py":
        command = [_script_python(ctx, path), "-X", "utf8", str(path), *args]
    else:
        shell = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
        if not shell:
            raise EnvironmentProblem("no PowerShell (pwsh or powershell.exe) to run " + path.name)
        command = [shell, "-NoProfile", "-File", str(path), *args]
    ctx.progress("script", msg=path.name)
    done = process.run(ctx, command, cwd=path.parent, timeout=timeout)
    log = ctx.out / f"{path.stem}.log"
    log.write_text(done.output, encoding="utf-8")
    ctx.output(path.stem, log)
    if done.code is None:
        ctx.finding(f"{path.name} did not finish within {process.span(timeout)}; stopped")
    elif done.code != 0:
        ctx.finding(f"{path.name} exited {done.code}")
    else:
        ctx.summary(f"{path.name} finished")
    return {"script": str(path), "exit": done.code, "seconds": done.seconds}


def turn(ctx: Context, steps: list, continue_on_finding: bool):
    """Runs several lab routines in this one process, so the lease wb-host took for this job is held from the
    first step to the last and nothing else can start between them."""
    from wb_core import types
    from wb_core.manifest import parse
    manifest = parse(Path(__file__).with_name("module.toml"), "wb_lab")
    routines = {c.full: c for c in manifest.commands if c.turn}  # module.toml marks them: turn = true
    done = []
    for index, step in enumerate(steps, start=1):
        ctx.check_cancel()
        name = step.get("command")
        if name not in routines:
            raise ParamError(f"turn step {index}: {name} is not a routine a turn can run ({', '.join(routines)})")
        command = routines[name]
        try:
            params = types.validate(command.params, step.get("params") or {})
        except types.ValueError_ as error:
            raise ParamError(f"turn step {index} ({name}): {error}") from None
        label = step.get("id") or f"step-{index}"
        ctx.progress(label, (index - 1) / len(steps), name)
        before = len(ctx.findings)
        result = globals()[command.handler.rpartition(":")[2]](ctx, **params)
        failed = len(ctx.findings) > before
        done.append({"id": label, "command": name, "result": result, "finding": failed})
        if failed and not continue_on_finding:
            ctx.log.warning(f"stopping at {label}: {ctx.findings[-1]}")
            break
    if ctx.outcome == "ok":
        ctx.summary(f"turn of {len(done)} step(s) finished under one lease")
    return {"steps": done}


def _lab_home() -> Path:
    value = os.environ.get("WB_LAB_HOME", "")
    if not value:
        raise EnvironmentProblem("wb-host did not say where the lab home is")
    return Path(value)


def migrate(ctx: Context, execute: bool, items: list, undo: bool, profile: str = ""):
    """lab-and-ci-split step A, reversible: each item is renamed into the lab home (same volume only, never a
    copy) and a link (a junction on Windows, a symlink elsewhere) is left at its old path, so every reader keeps
    working until it is pointed at the lab home. The GPU lease is held throughout, so no turn starts mid-move."""
    import time as _time

    from wb import platform
    launcher, lab = _launcher(ctx), _lab_home()
    local = launcher / _lab_root(ctx)
    manifests = lab / "migrations"
    if undo:
        done = sorted(manifests.glob("*.json"))
        if not done:
            raise ParamError("no migration to undo")
        record = json.loads(done[-1].read_text(encoding="utf-8"))
        undone = []
        for item in reversed(record["moved"]):
            source, target = Path(item["from"]), Path(item["to"])
            if execute:
                if item.get("link") == "hardlink":
                    source.unlink()
                elif platform.is_link(source):
                    platform.remove_link(source)
                os.rename(target, source)
            undone.append(item["item"])
        if execute:
            done[-1].rename(done[-1].with_suffix(".undone"))
            ctx.summary(f"moved {len(undone)} item(s) back to {local}")
        else:
            ctx.summary(f"would move {len(undone)} item(s) back (run with execute=true)")
        return {"undone": undone, "executed": execute}
    moves, refused = [], []
    for name, what in _lab_items(ctx):
        if items and name not in items:
            continue
        source, target = local / name, lab / name
        if not source.exists() or platform.is_link(source):
            continue
        held = [Path(p) for p in (os.environ.get("WB_LEASE_FILE"), os.environ.get("WB_COORDINATION_DIR")) if p]
        if any(_inside(h, source) for h in held):
            refused.append({"item": name, "why": "it holds the GPU lease or the lanes this runner holds; move it "
                                                 "with wb-host stopped and no lane running or waiting"})
        elif target.exists():
            refused.append({"item": name, "why": f"{target} already exists"})
        elif not platform.same_volume(source, lab):
            refused.append({"item": name, "why": "the lab home is on another volume, so this would be a copy, not a "
                                                 "rename; move it by hand or put the lab home on the same volume"})
        else:
            moves.append({"item": name, "what": what, "from": str(source), "to": str(target)})
    if not execute:
        ctx.summary(f"would move {len(moves)} item(s) by rename and link each back; {len(refused)} refused; "
                    "nothing moved (run with execute=true)")
        return {"moves": moves, "refused": refused, "executed": False}
    lab.mkdir(parents=True, exist_ok=True)
    moved = []
    try:
        for move in moves:
            os.rename(move["from"], move["to"])
            if Path(move["to"]).is_file():
                os.link(move["to"], move["from"])  # a junction points only at folders; a file keeps one inode
                kind = "hardlink"
            else:
                kind = platform.make_link(Path(move["from"]), Path(move["to"]))
            moved.append(dict(move, link=kind))
            ctx.log.info(f"moved {move['item']} and left a {kind} at {move['from']}")
    finally:
        manifests.mkdir(parents=True, exist_ok=True)
        record = manifests / f"{_time.strftime('%Y%m%d-%H%M%S', _time.gmtime())}.json"
        record.write_text(json.dumps({"launcher": str(launcher), "lab_home": str(lab), "moved": moved}, indent=1),
                          encoding="utf-8")
        ctx.output("manifest", record)
    ctx.summary(f"moved {len(moved)} item(s) into {lab}; links left at the old paths; undo with undo=true")
    return {"moved": moved, "refused": refused, "executed": True}


def _lab_root(ctx: Context) -> str:
    root = _profile(ctx)["lab_root"]
    if not root:
        raise EnvironmentProblem("the instance profile names no lab_root, so there is nothing to move")
    return root


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve() == folder.resolve() or path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False


def _instances(ctx: Context) -> Path:
    relative = Path(_profile(ctx)["instances"])
    folder = _launcher(ctx) / relative
    return folder.resolve() if folder.exists() else _lab_home() / relative.name


def _last_used(folder: Path) -> float:
    newest = folder.stat().st_mtime
    for child in folder.iterdir():
        try:
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            pass
    return newest


def prune_instances(ctx: Context, days: int, execute: bool, keep: list, names: list, expect=None, profile: str = ""):
    """``names`` limits a deletion to exactly the instances someone approved; ``expect`` refuses one whose count
    has drifted since the dry run that was approved (instances cross the age line as time passes)."""
    import fnmatch
    import time as _time
    folder = _instances(ctx)
    if not folder.is_dir():
        raise EnvironmentProblem(f"no instances folder at {folder}")
    patterns = list(keep) + list(ctx.config.get("keep_instances") or [])
    cutoff = _time.time() - days * 86400
    candidates, kept = [], 0
    for instance in sorted(p for p in folder.iterdir() if p.is_dir()):
        if any(fnmatch.fnmatch(instance.name, p) for p in patterns):
            kept += 1
            continue
        used = _last_used(instance)
        if used < cutoff:
            candidates.append({"instance": instance.name,
                               "last_used": _time.strftime("%Y-%m-%d", _time.gmtime(used)),
                               "bytes": _size(instance) if execute or len(candidates) < 2000 else None})
    if names:
        wanted = set(names)
        candidates = [c for c in candidates if c["instance"] in wanted]
        missing = sorted(wanted - {c["instance"] for c in candidates})
        if missing and execute:
            raise ParamError(f"{len(missing)} named instance(s) are no longer candidates (used again, or gone): "
                             f"{', '.join(missing[:5])}; nothing removed")
    if expect is not None and execute and len(candidates) != expect:
        raise ParamError(f"{len(candidates)} instance(s) match now, not the {expect} approved; nothing removed "
                         "(run the dry run again, or pass names)")
    total = sum(c["bytes"] or 0 for c in candidates)
    if not execute:
        ctx.summary(f"{len(candidates)} instance(s) unused for {days} days, {total / 1e9:.1f} GB; {kept} kept by "
                    "pattern; nothing removed (run with execute=true, and expect=<count> or names=[...])")
        return {"candidates": candidates, "bytes": total, "removed": False}
    from .store import MARKER, remove_tree
    store = _store(ctx)
    for item in candidates:
        ctx.check_cancel()
        path = folder / item["instance"]
        store.remove(path) if (path / MARKER).is_file() else remove_tree(path)
        ctx.log.info(f"removed instance {item['instance']} (last used {item['last_used']})")
    ctx.summary(f"removed {len(candidates)} instance(s), {total / 1e9:.1f} GB")
    return {"candidates": candidates, "bytes": total, "removed": True}


# ----------------------------------------------------------------------------------------------
# the instance store (wb_lab.store): recipes, templates, linked instances, world snapshots


def _store(ctx: Context):
    from .store import Store
    store = Store(_lab_home() / "store", profiles.layout(_profile(ctx)))
    folder = _instances(ctx)
    if folder.exists() and not store.same_volume(folder):
        raise EnvironmentProblem(f"the store ({store.root}) and the instances ({folder}) are on different volumes; "
                                 "hard links need one volume")
    return store


def _profile_path(ctx: Context, relative: str) -> Path:
    """A profile path: relative to the launcher checkout, or under a named checkout (``{name}/...``)."""
    name, rest = profiles.checkout_of(relative)
    launcher = _launcher(ctx)
    if name is None:
        return launcher / rest
    checkouts = _profile(ctx)["checkouts"]
    if name not in checkouts:
        raise EnvironmentProblem(f"the instance profile uses {{{name}}} but its checkouts name no {name}")
    base = Path(checkouts[name])
    base = base if base.is_absolute() else (launcher / base).resolve()
    return base / rest if rest else base


def _digest(path: Path) -> str | None:
    from .store import sha256
    return sha256(path) if path.is_file() else None


def recipe_inputs(ctx: Context, seed, scale, args: list) -> dict:
    """Everything the launcher's prepare reads, each file by its SHA-256."""
    p = _profile(ctx)
    file_flags = set(p["file_flags"])
    files, normal = {}, []
    previous = None
    for arg in args:
        arg = str(arg)
        if previous in file_flags or (Path(arg).suffix.lower() in (".jar", ".json") and Path(arg).is_file()):
            digest = _digest(Path(arg))
            if digest is None:
                raise ParamError(f"{arg} does not exist")
            files[arg] = digest
            normal.append("sha256:" + digest)
        else:
            normal.append(arg)
        previous = arg
    inputs = {"schema": 2, "seed": seed, "scale": scale, "args": normal,
              "files": {name: _digest(_profile_path(ctx, name)) for name in p["recipe_files"]}}
    if _profile_name(ctx):  # another harness's recipe never shares a template; the default profile's keys stand
        inputs["profile"] = _profile_name(ctx)
    default = p.get("default_artifact") or {}
    if default.get("path") and default.get("flag") not in [str(a) for a in args]:
        inputs["files"][default["path"]] = _digest(_profile_path(ctx, default["path"]))
    return inputs


def world_key(ctx: Context, instance: Path) -> tuple[dict, str | None]:
    """What a world depends on and the generator build that wrote it, both from the instance manifest."""
    p = _profile(ctx)
    path = instance / p["manifest"]
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return profiles.world_key(manifest, p)


def _verify(ctx: Context, name: str):
    p = _profile(ctx)
    if not p.get("load"):
        return
    done = ctx.run(profiles.command(p["load"], python=_python(ctx), name=name), cwd=_launcher(ctx), env=_env(ctx),
                   timeout=300, log_output=False)
    if done.returncode != 0 or (p.get("load_ok") and p["load_ok"] not in done.stdout):
        raise EnvironmentProblem(f"the launcher refuses to load {name}: {done.stdout.strip().splitlines()[-1:] or ''}")


def _check_world(ctx: Context, store, instance: Path, label: str, allow_writer_change: bool):
    record = store.world(label)
    key, writer = world_key(ctx, instance)
    if record["key"]["key"] != key:
        raise ParamError(f"world {label} was generated for {record['key']['key']}, not this instance's {key}")
    kept = record["key"].get("writer", record["key"].get("cubature"))  # snapshots before 2026-11-21 said cubature
    if kept != writer and not allow_writer_change:
        raise ParamError(f"world {label} was written by another build of its writer (the profile's world_writer); "
                         "pass allow_writer_change=true if that change is the treatment")


def ensure(ctx: Context, name: str, args: list, world: str, replace: bool, allow_writer_change: bool, verify: bool,
           seed=None, scale=None, profile: str = ""):
    from .store import MARKER, recipe_of, remove_tree
    store, folder = _store(ctx), _instances(ctx)
    inputs = recipe_inputs(ctx, seed, scale, args)
    recipe = recipe_of(inputs)
    destination = folder / name
    reused, prepared = False, False
    sizes = {"linked_bytes": 0, "copied_bytes": 0}
    if destination.exists():
        marker = destination / MARKER
        current = json.loads(marker.read_text(encoding="utf-8")).get("recipe") if marker.is_file() else None
        if current == recipe:
            reused = True
        elif not replace:
            raise ParamError(f"instance {name} exists " + (f"from recipe {current}" if current else "as a full copy")
                             + f", not recipe {recipe}; pass replace=true to rebuild it")
        else:
            store.remove(destination) if marker.is_file() else remove_tree(destination)
    if not reused:
        if store.template(recipe) is None:
            temporary = f"wb-template-{recipe[:12]}"
            if (folder / temporary).exists():
                remove_tree(folder / temporary)
            command = _prepare_command(ctx, temporary, seed, scale, args)
            ctx.progress("prepare", msg=f"preparing the template for recipe {recipe}")
            done = ctx.run(command, cwd=_launcher(ctx), env=_env(ctx), timeout=40 * 60)
            (ctx.out / "prepare.log").write_text(done.stdout, encoding="utf-8")
            ctx.output("prepare-log", ctx.out / "prepare.log")
            if done.returncode != 0:
                remove_tree(folder / temporary)
                raise EnvironmentProblem(f"prepare exited {done.returncode}")
            store.adopt_template(folder / temporary, recipe, inputs)
            prepared = True
        ctx.progress("materialize", msg=f"{name} from recipe {recipe}")
        sizes = store.materialize(recipe, destination)
    if world:
        try:
            _check_world(ctx, store, destination, world, allow_writer_change)
        except ParamError:
            if not reused:
                store.remove(destination)  # never leave a half-made instance; the template stays for reuse
            raise
        ctx.progress("world", msg=f"restoring {world}")
        store.restore(destination, world)
        marker = destination / MARKER
        record = json.loads(marker.read_text(encoding="utf-8"))
        record["world"] = world
        marker.write_text(json.dumps(record, indent=1), encoding="utf-8")
    if verify:
        _verify(ctx, name)
    ctx.measure("linked_mb", round(sizes["linked_bytes"] / 1e6, 1))
    ctx.measure("copied_mb", round(sizes["copied_bytes"] / 1e6, 1))
    how = "reused" if reused else ("built from a new template" if prepared else "built from the template")
    ctx.summary(f"instance {name} {how} (recipe {recipe}" + (f", world {world}" if world else "") + ")"
                + (f"; {sizes['linked_bytes'] / 1e6:.0f} MB shared, {sizes['copied_bytes'] / 1e6:.0f} MB own"
                   if not reused else ""))
    return {"instance": name, "recipe": recipe, "reused": reused, "prepared_template": prepared,
            "world": world or None, **sizes}


def snapshot(ctx: Context, name: str, label: str, profile: str = ""):
    store, instance = _store(ctx), _instances(ctx) / name
    if not instance.is_dir():
        raise ParamError(f"no instance {name}")
    key, writer = world_key(ctx, instance)
    record = store.snapshot(instance, label, {"key": key, "writer": writer})
    ctx.measure("world_mb", round(record["bytes"] / 1e6, 1))
    ctx.summary(f"world of {name} kept as {label}: {len(record['files'])} files, {record['bytes'] / 1e6:.0f} MB")
    return {"label": label, "files": len(record["files"]), "bytes": record["bytes"], "key": key}


def reset(ctx: Context, name: str, world: str, allow_writer_change: bool, profile: str = ""):
    from .store import MARKER
    store, instance = _store(ctx), _instances(ctx) / name
    if not instance.is_dir():
        raise ParamError(f"no instance {name}")
    _check_world(ctx, store, instance, world, allow_writer_change)
    size = store.restore(instance, world)
    marker = instance / MARKER
    if marker.is_file():
        record = json.loads(marker.read_text(encoding="utf-8"))
        record["world"] = world
        marker.write_text(json.dumps(record, indent=1), encoding="utf-8")
    ctx.summary(f"{name} reset to world {world} ({size / 1e6:.0f} MB)")
    return {"instance": name, "world": world, "bytes": size}


def instances(ctx: Context, profile: str = ""):
    from .store import MARKER
    store, folder = _store(ctx), _instances(ctx)
    built, legacy = [], 0
    for path in sorted(p for p in folder.iterdir() if p.is_dir()) if folder.exists() else []:
        marker = path / MARKER
        if marker.is_file():
            record = json.loads(marker.read_text(encoding="utf-8"))
            built.append({"instance": path.name, "recipe": record.get("recipe"), "world": record.get("world")})
        else:
            legacy += 1
    templates = sorted(p.name for p in store.templates.glob("*") if (p / "template.json").is_file()) \
        if store.templates.exists() else []
    worlds = sorted(p.name for p in store.worlds.glob("*") if (p / "world.json").is_file()) if store.worlds.exists() else []
    ctx.summary(f"{len(built)} store-built instance(s), {legacy} full copies; {len(templates)} template(s), "
                f"{len(worlds)} world snapshot(s)")
    return {"built": built, "legacy": legacy, "templates": templates, "worlds": worlds}


def remove(ctx: Context, name: str, profile: str = ""):
    from .store import MARKER, remove_tree
    store, instance = _store(ctx), _instances(ctx) / name
    if not instance.is_dir():
        raise ParamError(f"no instance {name}")
    store.remove(instance) if (instance / MARKER).is_file() else remove_tree(instance)
    ctx.summary(f"removed {name}")
    return {"removed": name}


def dedupe(ctx: Context, execute: bool, limit: int, profile: str = ""):
    """Existing full-copy instances keep working exactly as they are; only their identical read-only files
    (libraries, mod jars, natives) become hard links to one stored copy."""
    from .store import MARKER, sha256
    store, folder = _store(ctx), _instances(ctx)
    linkable = store.layout.linkable
    legacy = [p for p in sorted(folder.iterdir()) if p.is_dir() and not (p / MARKER).exists()]
    if limit:
        legacy = legacy[:limit]
    groups: dict[tuple[str, int], int] = {}
    candidates = []
    for instance in legacy:
        for directory, _dirs, files in os.walk(instance):
            for file in files:
                path = Path(directory) / file
                rel = path.relative_to(instance).as_posix()
                if not linkable(rel):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                groups[(file, size)] = groups.get((file, size), 0) + 1
                candidates.append((instance, path, rel, size))
    estimate = sum(size * (count - 1) for (_name, size), count in groups.items() if count > 1)
    if not execute:
        ctx.summary(f"{len(legacy)} full-copy instance(s), {len(candidates)} read-only files; about "
                    f"{estimate / 1e9:.1f} GB would be freed (estimated by name and size); nothing changed "
                    "(run with execute=true)")
        return {"instances": len(legacy), "files": len(candidates), "estimated_bytes": estimate, "executed": False}
    freed = 0
    by_instance: dict[Path, dict] = {}
    for index, (instance, path, rel, size) in enumerate(candidates):
        if index % 500 == 0:
            ctx.check_cancel()
            ctx.progress("dedupe", index / max(1, len(candidates)), instance.name)
        digest = sha256(path)
        existed = store.blob(digest).exists()
        store.intern(path, digest)
        if existed:
            freed += size
        by_instance.setdefault(instance, {})[rel] = digest
    for instance, links in by_instance.items():
        (instance / MARKER).write_text(json.dumps({"schema": 1, "recipe": None, "legacy": True, "links": links,
                                                    "world": None}, indent=1), encoding="utf-8")
    ctx.summary(f"{len(by_instance)} instance(s) linked to the store; {freed / 1e9:.1f} GB freed")
    return {"instances": len(by_instance), "freed_bytes": freed, "executed": True}


def store_gc(ctx: Context, days: int, execute: bool, profile: str = ""):
    import fnmatch
    import time as _time
    from .store import MARKER, remove_tree
    store, folder = _store(ctx), _instances(ctx)
    cutoff = _time.time() - days * 86400
    in_use = set()
    for marker in folder.glob(f"*/{MARKER}") if folder.exists() else []:
        record = json.loads(marker.read_text(encoding="utf-8"))
        in_use.update(filter(None, [record.get("recipe"), record.get("world")]))
    keep = list(ctx.config.get("keep_worlds") or [])
    templates = [p for p in store.templates.glob("*") if (p / "template.json").is_file() and p.name not in in_use
                 and store.last_used(p) < cutoff] if store.templates.exists() else []
    worlds = [p for p in store.worlds.glob("*") if (p / "world.json").is_file() and p.name not in in_use
              and store.last_used(p) < cutoff and not any(fnmatch.fnmatch(p.name, k) for k in keep)] \
        if store.worlds.exists() else []
    if execute:
        for path in templates + worlds:
            remove_tree(path)
    orphans = store.orphan_blobs()
    freed = sum(p.stat().st_size for p in orphans)
    if execute:
        for path in orphans:
            os.chmod(path, 0o600)
            path.unlink()
    verb = "removed" if execute else "would remove"
    ctx.summary(f"{verb} {len(templates)} template(s), {len(worlds)} snapshot(s) and {len(orphans)} unused blob(s) "
                f"({freed / 1e9:.2f} GB)" + ("" if execute else "; nothing changed (run with execute=true)"))
    return {"templates": [p.name for p in templates], "worlds": [p.name for p in worlds], "blobs": len(orphans),
            "bytes": freed, "executed": execute}
