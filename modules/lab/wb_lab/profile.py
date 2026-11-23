"""Instance profiles: what the lab needs to know about a launcher, so the lab names no project.

A profile says how a launcher checkout prepares, plays and checks instances, and how its instances are laid out
(which parts are worlds, which files name the instance, which files the game only reads). It comes from config
only — ``[modules.lab.profile]``, for example the file ``modules/lab/profile.toml`` in the config folder. Workbench
ships no launcher's profile: a launcher is supported by writing one, never by changing code. Each harness may have a
named profile of its own, ``[modules.lab.profiles.<name>]``, which a job picks with ``profile=<name>``; one that
starts ``from`` another profile (empty: the default) writes only the keys it changes, such as its ``checkout``.

Command templates take ``{python}``, ``{name}``, ``{name_repr}``, ``{tour}`` and ``{window}``. A path in
``recipe_files``, ``default_artifact`` or an ``env`` value may start with ``{<checkout>}``, a name from
``checkouts`` (other checkouts the launcher reads, relative to the launcher checkout or absolute).

The ``checkout`` and each of the ``checkouts`` is a folder, read as it is, or a table naming a repository and a ref:
each job then reads a snapshot fixed at the commit it resolves to (wb_lab.snapshots).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

REQUIRED = ("launcher", "instances", "prepare", "play")
DEFAULTS = {
    "checkout": None,            # the launcher's checkout for this profile; unset: [modules.lab] launcher; a table
                                 # {repo, ref, fetch} reads a snapshot at the commit ref names (wb_lab.snapshots)
    "launcher": None,            # the launcher script, relative to the checkout; it must exist there
    "instances": None,           # the folder instances live in, relative to the checkout
    "prepare": None,             # command template that prepares an instance named {name}
    "play": None,                # command template that plays {tour} on {name} in a {window}
    "play_open": None,           # command template for an open session on {name} in a {window}: no tour, it ends
                                 # when the person playing closes the client; unset: every play needs a tour
    "seed_flag": None,           # how prepare takes a seed; a seed is refused when this is unset
    "scale_flag": None,          # how prepare takes a scale; a scale is refused when this is unset
    "options_env": None,         # the variable play's JVM options go in
    "env": {},                   # environment for every launcher command
    "load": [],                  # command template that checks {name} loads; empty: no check
    "load_ok": None,             # a line load prints when the instance is sound
    "collect": [],               # [file in the instance, name to keep it as] after a play
    "captures_pattern": None,    # a regular expression on play's output; group 1 is a folder of captures
    "failures": [],              # regular expressions, most telling first: the first line one finds in play's output
                                 # or the files it collected explains a failed play
    "recipe_files": [],          # files whose content is part of every recipe
    "default_artifact": {},      # {flag, path}: a file prepare reads unless the flag is given
    "file_flags": [],            # prepare flags whose next argument is a file, hashed into the recipe
    "checkouts": {},             # other checkouts by name, for {name} in paths: folders, or tables {repo, ref, fetch}
                                 # read as snapshots at the commit the ref names
    "manifest": None,            # the instance's JSON manifest, read for the world key
    "world_key": {},             # name -> dotted path in the manifest; a leading # keeps only its SHA-256
    "world_writer": None,        # dotted path in the manifest naming the build that writes worlds
    "world_parts": [],           # the instance's world folders, kept and restored by snapshots
    "rewrite": [],               # JSON files that hold the instance's own folder and name
    "linkable_dirs": [],         # folders the game only reads: shared by hard link
    "linkable_suffixes": [],     # file suffixes the game only reads: shared by hard link
    "packs_dir": None,           # where an instance keeps packs (shader or resource packs); unset: packs are refused
    "lab_root": None,            # the checkout's folder of lab items, for lab.migrate
    "lab_items": [],             # [item, what it is] in lab_root, moved by lab.migrate
    "stays": [],                 # lab items that must never move
    "inputs": [],                # lab.inputs' steps, in order: {name, run, checkout}; run is a command template
                                 # with {python} and {cwd}, run in checkout (a name from checkouts; empty: the
                                 # launcher's) with env, and brings the launch set to its state before lab.ensure
    "agree": {},                 # dependency -> [{file, pattern, checkout}]: where each project of the launch set
                                 # pins a dependency they share (pattern's group 1 is the commit); lab.inputs reports
                                 # and lab.ensure refuses pins that disagree
}

STEP_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class ProfileError(ValueError):
    pass


@dataclass
class Layout:
    """The parts of an instance the store treats differently."""
    world_parts: list[str] = field(default_factory=list)
    rewrite: list[str] = field(default_factory=list)
    linkable_dirs: list[str] = field(default_factory=list)
    linkable_suffixes: list[str] = field(default_factory=list)

    def linkable(self, relative: str) -> bool:
        """Files the game only reads: shared by hard link. Everything else is a private copy."""
        rel = relative.replace("\\", "/")
        if any(rel == part or rel.startswith(part + "/") for part in self.world_parts):
            return False
        if any(rel.startswith(part + "/") for part in self.linkable_dirs):
            return True
        return any(rel.endswith(suffix) for suffix in self.linkable_suffixes)


def _keys(config: dict, name: str, seen: tuple[str, ...] = ()) -> dict:
    """A profile's keys as written: ``[modules.lab] profile`` for no name, else ``[modules.lab.profiles.<name>]`` over
    the profile its ``from`` names (empty: the default profile), so a harness that differs in a few keys (its
    checkout) writes only those."""
    if not name:
        return dict(config.get("profile") or {})
    named = config.get("profiles") or {}
    if name not in named:
        raise ProfileError(f"no instance profile {name} (known: {', '.join(sorted(named)) or 'none'})")
    given = dict(named[name] or {})
    if "from" not in given:
        return given
    base = given.pop("from")
    if not isinstance(base, str):
        raise ProfileError(f"instance profile {name}: from names the profile it starts from (empty: the default one)")
    if base in seen + (name,):
        raise ProfileError(f"instance profile {name}: from makes a cycle ({' -> '.join(seen + (name, base))})")
    return {**_keys(config, base, seen + (name,)), **given}


def merged(config: dict, name: str = "") -> dict:
    """An instance profile over the neutral defaults, checked: ``[modules.lab.profiles.<name>]`` when a name is given
    (each harness its own; ``from`` starts it from another profile), else ``[modules.lab] profile``. Unknown keys and
    missing required ones are refused with the key's name."""
    given = _keys(config, name)
    unknown = sorted(set(given) - set(DEFAULTS))
    if unknown:
        raise ProfileError(f"unknown instance profile key(s) {', '.join(unknown)} (known: {', '.join(DEFAULTS)})")
    profile = copy.deepcopy(DEFAULTS)
    profile.update(given)
    for pattern in profile["failures"]:
        try:
            re.compile(pattern)
        except (re.error, TypeError) as error:
            raise ProfileError(f"the instance profile's failures: {pattern!r} is not a regular expression ({error})") from None
    from .snapshots import check as check_checkouts
    check_checkouts(profile, ProfileError)
    _check_inputs(profile)
    _check_agree(profile)
    missing = [key for key in REQUIRED if not profile.get(key)]
    if missing:
        raise ProfileError(f"the instance profile needs {', '.join(missing)} (set [modules.lab.profile]; see "
                           "config.example/modules/lab.toml)")
    return profile


def _check_inputs(profile: dict) -> None:
    """Each input step names itself (a plain name: it names its log), runs a non-empty command, and runs in the
    launcher checkout or one the profile's checkouts name."""
    if not isinstance(profile["inputs"], list):
        raise ProfileError("the instance profile's inputs is a list of steps")
    seen = set()
    for step in profile["inputs"]:
        if not isinstance(step, dict) or set(step) - {"name", "run", "checkout"}:
            raise ProfileError(f"the instance profile's inputs: {step!r} is not a step (keys: name, run, checkout)")
        name = step.get("name")
        if not isinstance(name, str) or not STEP_NAME.match(name) or name in seen:
            raise ProfileError(f"the instance profile's inputs: {name!r} is not a plain, distinct step name")
        seen.add(name)
        run = step.get("run")
        if not isinstance(run, list) or not run or not all(isinstance(part, str) for part in run):
            raise ProfileError(f"the instance profile's input step {name}: run is a non-empty command template")
        checkout = step.get("checkout") or ""
        if checkout and checkout not in (profile["checkouts"] or {}):
            raise ProfileError(f"the instance profile's input step {name}: its checkouts name no {checkout}")


def _check_agree(profile: dict) -> None:
    """Each shared dependency names two or more pins, each a file, a pattern with a group for the commit, and the
    launcher checkout or one the profile's checkouts name."""
    if not isinstance(profile["agree"], dict):
        raise ProfileError("the instance profile's agree is a table of dependencies")
    for dependency, pins in profile["agree"].items():
        if not isinstance(pins, list) or len(pins) < 2:
            raise ProfileError(f"the instance profile's agree.{dependency}: two or more pins to compare")
        for pin in pins:
            if not isinstance(pin, dict) or set(pin) - {"file", "pattern", "checkout"} or \
                    not isinstance(pin.get("file"), str) or not isinstance(pin.get("pattern"), str):
                raise ProfileError(f"the instance profile's agree.{dependency}: {pin!r} is not a pin "
                                   "(keys: file, pattern, checkout)")
            try:
                groups = re.compile(pin["pattern"]).groups
            except re.error as error:
                raise ProfileError(f"the instance profile's agree.{dependency}: {pin['pattern']!r} is not a regular "
                                   f"expression ({error})") from None
            if groups < 1:
                raise ProfileError(f"the instance profile's agree.{dependency}: {pin['pattern']!r} has no group for "
                                   "the commit")
            checkout = pin.get("checkout") or ""
            if checkout and checkout not in (profile["checkouts"] or {}):
                raise ProfileError(f"the instance profile's agree.{dependency}: its checkouts name no {checkout}")


def layout(profile: dict) -> Layout:
    return Layout(list(profile["world_parts"]), list(profile["rewrite"]), list(profile["linkable_dirs"]),
                  list(profile["linkable_suffixes"]))


def checkout_of(relative: str) -> tuple[str | None, str]:
    """``{name}/rest`` -> (name, rest); a plain path -> (None, path)."""
    if relative.startswith("{") and "}" in relative:
        name, _, rest = relative[1:].partition("}")
        return name, rest.lstrip("/\\")
    return None, relative


def dotted(data, path: str):
    for part in path.split("."):
        data = data.get(part) if isinstance(data, dict) else None
    return data


def world_key(manifest: dict, profile: dict) -> tuple[dict, str | None]:
    """What a world depends on, read from the instance manifest by the profile's dotted paths (a leading # keeps
    only the SHA-256 of the value), and the build that wrote it."""
    import hashlib
    import json
    key = {}
    for name, path in profile["world_key"].items():
        if path.startswith("#"):
            value = json.dumps(dotted(manifest, path[1:]) or {}, sort_keys=True)
            key[name] = hashlib.sha256(value.encode()).hexdigest()
        else:
            key[name] = dotted(manifest, path)
    writer = profile.get("world_writer")
    return key, dotted(manifest, writer) if writer else None


def command(template: list[str], **values) -> list[str]:
    values.setdefault("name_repr", repr(values.get("name")))
    return [str(part).format(**values) for part in template]
