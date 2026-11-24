"""Rental bundles made from a launch file (``vast.bundle-launch``): the game a rental plays is exactly the one
lab.resolve wrote into launch.json, built on the rental by the same lab.build code that builds it here.

A launch bundle's ``files/`` (uploaded to ``/root/bundle``) holds:

- ``launch.json``, the launch file's exact bytes, and the tour file;
- ``store/files/<sha256>/<name>``: every member and the runtime's own files (``runtime.files``: the installer and the
  launcher instance) from this machine's Store, each hashed as it is copied;
- ``jdk/<archive>``: the Linux archive of the JDK build the launch names, from the Store by the SHA-256 the launch's
  set gives it in ``platform.jdk``; the run script checks and unpacks it into ``store/jdks/<build>/linux``, where
  lab.build looks, so the rental's bootstrap installs no JDK of its own;
- ``sources/<id>/``: each overlay folder and the configure script the launch file names, exported from the projects'
  repositories by those ids (a configure script only for a project in this machine's ``[lab] trusted``);
- ``runner/``: Workbench's own ``wb_core``, ``wb`` and ``wb_lab`` packages as they are in the Workbench that made the
  bundle, and ``launch_run.py`` (``wb_vast.launch_runner``), which builds the instance with ``wb_lab.instance.build``
  and plays the tour with ``wb_lab.run.run``;
- ``run.sh``: checks every file in ``checksums.sha256``, unpacks the JDK, builds the instance, starts a display
  (Xvfb, stopping with a RUN line when it does not start; VirtualGL on the GPU when the bundle asks for it, Mesa's
  software OpenGL otherwise, which the bundle's ``needs.display`` has the bootstrap install) and plays the tour
  headless; ``results/`` gets the build's and
  the session's logs, ``instance.json``, ``session.json`` and the session folder, packed into ``results.tar``.
  ``run.sh <bundle> build`` stops once the instance is built;
- ``bundle-launch.json`` (``wb.bundle-launch.v1``): what the bundle carries and was made from.

Every load of such a bundle (vast.bundle-check, the quote and vast.rent) checks it against its launch file before
anything else, and refuses one whose member, runtime file or JDK archive is missing or is other bytes.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import urllib.parse
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError, sha256_file

from . import bundle as bundles

SCHEMA = "wb.bundle-launch.v1"
INFO = "bundle-launch.json"
INSTANCE = "rental"
RUNTIME_PARTS = ("installer", "prism")  # the universal JAR is the installer's own output, checked by lab.build
# Workbench's packages a rental needs to build and play with the same code: (folder in the checkout, package).
CODE = (("core", "wb_core"), ("sdk/python", "wb"), ("modules/lab", "wb_lab"))

RUN_SH = r"""#!/bin/bash
# The entry script of a launch bundle (vast.bundle-launch). vast.rent runs it on a machine its bootstrap made ready
# (Python and Xvfb, with VirtualGL when the bundle needs the GPU and Mesa's software OpenGL when it does not), with
# the bundle in $1. A second argument "build"
# stops once the instance is built. bundle.env names the JDK build and archive, the window and the GPU.
set -uo pipefail
BUNDLE=${1:?bundle directory}
UNTIL=${2:-session}
LAB=${WB_BUNDLE_LAB:-/root/lab}
log() { echo "RUN $*"; }
cd "$BUNDLE" || exit 1
# shellcheck disable=SC1091
source ./bundle.env
mkdir -p results "$LAB"
finish() {
  tar -cf "$BUNDLE/results.tar" -C "$BUNDLE" results
  log "done; results.tar $(du -h "$BUNDLE/results.tar" | cut -f1)"
  exit "$1"
}
step() {  # step <name> <log>: the runner's step, its RUN lines shown, everything in results/<log>
  python3 -B runner/launch_run.py "$1" "$BUNDLE" "$LAB" 2>&1 | tee "results/$2" | grep --line-buffered '^RUN '
  return "${PIPESTATUS[0]}"
}
log "verify $(wc -l < checksums.sha256) files"
if ! sha256sum -c --quiet checksums.sha256 > results/verify.log 2>&1; then
  log "verify failed: $(head -1 results/verify.log)"
  finish 1
fi
jdk="store/jdks/$JDK_BUILD/linux"
if [ ! -d "$jdk" ] || [ -z "$(ls -A "$jdk")" ]; then
  log "jdk $JDK_BUILD"
  mkdir -p "$jdk" && tar -xzf "$JDK_ARCHIVE" -C "$jdk" || { log "verify failed: $JDK_ARCHIVE does not unpack"; finish 1; }
fi
log "build $INSTANCE"
step build build.log || finish 1
[ "$UNTIL" = "build" ] && finish 0
if ! pgrep -x Xvfb >/dev/null; then
  Xvfb :1 -screen 0 3840x2160x24 > results/xvfb.log 2>&1 &
  xvfb=$!
  for _ in $(seq 1 30); do  # up to 15 s for the display's socket, unless Xvfb has already exited
    [ -S /tmp/.X11-unix/X1 ] && break
    kill -0 "$xvfb" 2>/dev/null || break
    sleep 0.5
  done
  if ! kill -0 "$xvfb" 2>/dev/null || [ ! -S /tmp/.X11-unix/X1 ]; then
    log "session failed: the display did not start (Xvfb :1): $(tail -1 results/xvfb.log 2>/dev/null)"
    finish 1
  fi
fi
export DISPLAY=:1
if [ "$GPU" != "1" ] && command -v glxinfo >/dev/null; then
  glxinfo -B > results/display.txt 2>&1  # the software renderer the client draws with (llvmpipe)
  log "display :1, $(grep -m1 'Device:' results/display.txt | sed 's/^ *//')"
fi
WRAP=""
sampler=""
if [ "$GPU" = "1" ]; then
  WRAP="/opt/VirtualGL/bin/vglrun -d egl"
  nvidia-smi --query-gpu=name,driver_version,memory.total,pcie.link.gen.current,pcie.link.width.current --format=csv \
    > results/gpu.txt 2>&1
  nvidia-smi --query-gpu=timestamp,pstate,clocks.sm,clocks.mem,power.draw,temperature.gpu,utilization.gpu,memory.used \
    --format=csv -l 1 > results/gpu.csv 2>&1 &
  sampler=$!
fi
log "session tour $TOUR"
free -m > results/memory-before.txt 2>&1
# shellcheck disable=SC2086
$WRAP python3 -B runner/launch_run.py tour "$BUNDLE" "$LAB" 2>&1 | tee results/session.log | grep --line-buffered '^RUN '
code=${PIPESTATUS[0]}
[ -n "$sampler" ] && kill "$sampler" 2>/dev/null
free -m > results/memory-after.txt 2>&1
(dmesg 2>/dev/null || journalctl -k --no-pager 2>/dev/null) | tail -40 > results/kernel.txt
if [ "$code" -gt 128 ]; then  # killed by a signal, so it printed no reason of its own: say which (9 is the OOM killer's)
  log "session failed: the runner was killed by signal $((code - 128)); results/kernel.txt and memory-*.txt say more"
elif [ "$code" -ne 0 ] && ! grep -q '^RUN session' results/session.log; then
  log "session failed: the runner exited $code without a result; results/session.log ends where it stopped"
fi
finish "$code"
"""


HEAP = re.compile(r"-Xmx([0-9]+)([gGmM])")


def ram_needed(doc: dict) -> int:
    """The system memory, in GB, a launch's session needs on a rental: its server and its client each with the
    largest heap the launch file names (-Xmx), and 4 GB for the JVMs' own memory, the display and the runner."""
    heaps = [int(n) / (1024 if unit.lower() == "m" else 1) for n, unit in HEAP.findall(json.dumps(doc))]
    return int(-(-(2 * max(heaps or [4]) + 4) // 1))


def _git_bytes(repo: Path, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", *args], capture_output=True)
    if done.returncode != 0:
        raise EnvironmentProblem(f"git {' '.join(args[:2])} in {repo} failed: "
                                 f"{done.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return done.stdout


def _repository(project: str) -> Path:
    from wb.workspace import Workspace
    entry = Workspace().projects().get(project)
    if entry is None or not Path(entry.repo).is_dir():
        raise EnvironmentProblem(f"project {project} is not registered on this machine ([modules.ci.projects]), so "
                                 "the tree its launch file names cannot be exported")
    return Path(entry.repo)


def _lab(ctx: Context) -> dict:
    """This machine's [lab]: the job's, else the machine config's, checked."""
    from wb_core.settings import SettingsError, check_lab
    given = (ctx.spec or {}).get("lab")
    if not isinstance(given, dict):
        try:
            from wb import config as wbconfig
            given = wbconfig.load().values.get("lab")
        except Exception:  # noqa: BLE001 - no machine config: the defaults
            given = None
    try:
        return check_lab(given)
    except SettingsError as error:
        raise EnvironmentProblem(str(error)) from None


def _workbench() -> dict:
    """The Workbench whose code the bundle carries: its version, and its commit when it is a git checkout."""
    from wb_core import __version__
    from wb_core.service import REPO_ROOT
    found = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    commit = found.stdout.strip() if found.returncode == 0 else None
    dirty = None
    if commit:
        status = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=no", "--",
                                 *[folder for folder, _ in CODE], "modules/vast"], capture_output=True, text=True)
        dirty = bool(status.stdout.strip())
    return {"version": __version__, "commit": commit, "changed": dirty}


def _code_files() -> dict[str, Path]:
    """runner/<package>/... -> the Workbench file, for every Python and TOML file of the packages a rental runs."""
    from wb_core.service import REPO_ROOT
    files = {}
    for folder, package in CODE:
        root = REPO_ROOT / folder / package
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".toml") and "__pycache__" not in path.parts:
                files[f"runner/{package}/{path.relative_to(root).as_posix()}"] = path
    files["runner/launch_run.py"] = Path(__file__).with_name("launch_runner.py")
    return files


def _store_file(store: Path, digest: str, name: str | None) -> Path | None:
    folder = store / "files" / digest
    if name is None:
        names = sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []
        name = names[0] if len(names) == 1 else None
    path = folder / name if name else None
    return path if path is not None and path.is_file() else None


def _jdk(ctx: Context, store: Path, doc: dict, problems: list[str]) -> dict | None:
    """The Linux archive of the launch's JDK build: its line in the launch's set (platform.jdk), its file in the
    Store by that SHA-256."""
    from wb_core import setfile
    runtime = doc.get("runtime") or {}
    major, build = str(runtime.get("java") or ""), str(runtime.get("jdk") or "")
    try:
        platform = setfile.read_export(store.parent / "sets", str(doc.get("set") or "")).get("platform") or {}
    except setfile.SetError as error:
        problems.append(f"JDK {build}: {error}, so the Linux archive of the launch's JDK is unknown")
        return None
    line = (platform.get("jdk") or {}).get(major) or {}
    linux = line.get("linux") or {}
    if line.get("build") != build:
        problems.append(f"JDK: the launch names build {build}, and its set {str(doc.get('set'))[:12]} names "
                        f"{line.get('build') or 'none'} for Java {major}; resolve the launch again")
        return None
    if not linux.get("sha256") or not linux.get("url"):
        problems.append(f"JDK {build}: the set's platform.jdk.{major} names no Linux archive")
        return None
    name = Path(urllib.parse.unquote(urllib.parse.urlsplit(linux["url"]).path)).name
    found = _store_file(store, linux["sha256"], None)
    if found is None:
        problems.append(f"JDK {build}: the Store has no Linux archive {name} (SHA-256 {linux['sha256'][:12]}); fetch "
                        f"the platform of the launch's set: wb run store.platform revision={doc.get('set')}")
        return None
    actual = sha256_file(found)
    if actual != linux["sha256"]:
        problems.append(f"JDK {build}: the Store's {found.name} is {actual[:12]}, not the platform's "
                        f"{linux['sha256'][:12]}")
        return None
    return {"major": major, "build": build, "archive": f"jdk/{found.name}", "sha256": linux["sha256"],
            "url": linux["url"], "source": found}


def make(ctx: Context, launch: str, tour: str, name: str | None, window: str, gpu: bool, eula: bool,
         params: dict, limit: float, disk_gb: int, purpose: str, readable) -> dict:
    """Writes the bundle into ``ctx.out``; every reason to refuse is a finding and nothing is written."""
    from wb_build.files import set_digest, write_atomic
    launch_path, tour_path = readable(launch), readable(tour)
    problems: list[str] = []
    if not launch_path.is_file():
        raise ParamError(f"launch: {launch} is not a file (wb run lab.resolve folder=<worktree> writes one)")
    data = launch_path.read_bytes()
    try:
        doc = json.loads(data)
    except ValueError as error:
        raise ParamError(f"launch: {launch_path.name} is not JSON ({error})") from None
    if not isinstance(doc, dict) or doc.get("schema") != 1 or not doc.get("members") or not doc.get("store"):
        raise ParamError(f"launch: {launch_path.name} is not a launch file of schema 1 with members and a Store")
    if not tour_path.is_file():
        raise ParamError(f"tour: {tour} is not a file")
    try:
        json.loads(tour_path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ParamError(f"tour: {tour_path.name} is not JSON ({error})") from None
    if not (doc.get("tour") or {}).get("captured"):
        # lab.run plays a tour only by the launch's tour rules (wb_lab.run, "the launch has no tour rules"), which
        # lab.resolve carries into launch.json from the launch and every launch it inherits: refused here, before a
        # rental is spent finding out.
        chain = ", ".join(f"{s.get('name')} at {str(s.get('commit'))[:12]}" for s in doc.get("inherits") or [])
        raise ParamError(f"tour: launch {doc.get('project')}:{doc.get('launch')} has no tour rules (tour.captured: "
                         "the client's line for a captured view)"
                         + (f", and none of the launches it inherits gives them ({chain})" if chain else "")
                         + f", so lab.run on the rental would refuse to play {tour_path.name}; name them in "
                         f"[launch.{doc.get('launch')}.tour] of {doc.get('project')}'s project.toml (or of a launch "
                         "it inherits), then resolve the launch again (wb run lab.resolve)")
    if window and not re.match(r"^[0-9]+x[0-9]+$", window):
        raise ParamError(f"window: {window!r} is not WIDTHxHEIGHT")
    store = readable(str(doc["store"]))
    files: dict[str, Path | bytes] = {"launch.json": data, "tour.json": tour_path.read_bytes()}
    checks: dict[str, str] = {"launch.json": hashlib.sha256(data).hexdigest(),
                              "tour.json": sha256_file(tour_path)}

    # members and the runtime's own files, from the Store by SHA-256
    carried = []
    for member in doc["members"]:
        source = store / member["path"]
        where = f"member {member['member']}"
        if not source.is_file():
            fix = (f"wb check in {member['name']} at {str(member.get('commit'))[:12]}" if member.get("commit")
                   else "the landing that recorded it puts it there")
            problems.append(f"{where}: the Store has no {member['path']} ({fix})")
            continue
        actual = sha256_file(source)
        if actual != member["sha256"]:
            problems.append(f"{where}: the Store's {member['path']} is {actual[:12]}, not the launch's "
                            f"{member['sha256'][:12]}")
            continue
        files[f"store/{member['path']}"] = source
        checks[f"store/{member['path']}"] = actual
        carried.append({"member": member["member"], "path": f"store/{member['path']}", "sha256": actual})
    runtime_files = ((doc.get("runtime") or {}).get("files") or {})
    runtime = []
    for part in RUNTIME_PARTS:
        entry = runtime_files.get(part) or {}
        if not entry.get("sha256") or not entry.get("file"):
            problems.append(f"runtime {part}: the launch file names no runtime file (runtime.files); land the "
                            "platform's Cleanroom files, then resolve the launch again")
            continue
        source = _store_file(store, entry["sha256"], entry["file"])
        if source is None or sha256_file(source) != entry["sha256"]:
            problems.append(f"runtime {part}: the Store has no {entry['file']} with SHA-256 {entry['sha256'][:12]} "
                            "(lab.build on this machine keeps it there once it has fetched it)")
            continue
        relative = f"store/files/{entry['sha256']}/{entry['file']}"
        files[relative] = source
        checks[relative] = entry["sha256"]
        runtime.append({"part": part, "path": relative, "sha256": entry["sha256"]})
    jdk = _jdk(ctx, store, doc, problems)
    if jdk:
        files[jdk["archive"]] = jdk["source"]
        checks[jdk["archive"]] = jdk["sha256"]

    # the trees and the script the launch file names, by id
    lab = _lab(ctx)
    trusted = []
    sources = []
    overlays = [{"project": doc["base"]["project"], **doc["base"]["overlay"]}] \
        if (doc.get("base") or {}).get("overlay") else []
    overlays += list(doc.get("overlays") or ([doc["overlay"]] if doc.get("overlay") else []))
    for overlay in overlays:
        try:
            archive = _git_bytes(_repository(overlay["project"]), "archive", "--format=tar", overlay["tree"])
        except EnvironmentProblem as error:
            problems.append(f"overlay {overlay['path']} of {overlay['project']}: {error}")
            continue
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for item in tar.getmembers():
                if item.isfile():
                    files[f"sources/{overlay['tree']}/{item.name}"] = tar.extractfile(item).read()
                elif not item.isdir():
                    problems.append(f"overlay {overlay['path']} of {overlay['project']}: {item.name} is a link or a "
                                    "special file; a bundle holds regular files only")
        sources.append({"id": overlay["tree"], "kind": "tree", "project": overlay["project"], "path": overlay["path"]})
    script = doc.get("configure")
    if script:
        if script["project"] not in lab["trusted"]:
            problems.append(f"configure script {script['path']} is {script['project']}'s code, and {script['project']} "
                            f"is not in [lab] trusted on this machine ({', '.join(lab['trusted']) or 'none trusted'}); "
                            "a bundle carries a project's script only where its owner trusts it")
        else:
            trusted.append(script["project"])
            try:
                files[f"sources/{script['blob']}/{Path(script['path']).name}"] = _git_bytes(
                    _repository(script["project"]), "cat-file", "blob", script["blob"])
                sources.append({"id": script["blob"], "kind": "blob", "project": script["project"],
                                "path": script["path"]})
            except EnvironmentProblem as error:
                problems.append(f"configure script {script['path']} of {script['project']}: {error}")

    eula = bool(eula) or bool(lab["eula"])
    label = name or f"{doc['project']}-{doc['launch']}-{checks['launch.json'][:12]}"
    if problems:
        for problem in problems:
            ctx.finding(problem)
        ctx.log.warning("nothing was written")
        return {"bundle": None, "name": label, "problems": problems}

    files.update(_code_files())
    files["run.sh"] = RUN_SH.encode("utf-8")
    files["bundle.env"] = "".join(f"{key}={_shell(value)}\n" for key, value in (
        ("JDK_BUILD", jdk["build"]), ("JDK_ARCHIVE", jdk["archive"]), ("INSTANCE", INSTANCE),
        ("TOUR", tour_path.name), ("WINDOW", window), ("GPU", "1" if gpu else "0"))).encode("utf-8")
    files["checksums.sha256"] = "".join(f"{digest}  {path}\n" for path, digest in sorted(checks.items())).encode()
    info = {"schema": SCHEMA, "name": label,
            "launch": {"file": "launch.json", "sha256": checks["launch.json"],
                       **{k: doc.get(k) for k in ("project", "launch", "commit", "tree", "set")},
                       **({"inherits": [{k: s.get(k) for k in ("name", "commit", "chosen")} for s in doc["inherits"]]}
                          if doc.get("inherits") else {})},
            "tour": {"file": "tour.json", "name": tour_path.name, "sha256": checks["tour.json"]},
            "instance": INSTANCE, "window": window, "gpu": bool(gpu), "eula": eula, "trusted": trusted,
            "params": dict(params or {}), "limit": limit, "store": "store", "sources": "sources",
            "members": carried, "runtime": runtime,
            "jdk": {k: jdk[k] for k in ("major", "build", "archive", "sha256", "url")},
            "trees": sources, "metadata": _metadata(),
            "workbench": _workbench()}
    files[INFO] = (json.dumps(info, indent=1) + "\n").encode("utf-8")

    out = ctx.out
    shutil.rmtree(out / bundles.FILES, ignore_errors=True)
    for stale in ("bundle.toml", bundles.MANIFEST):
        (out / stale).unlink(missing_ok=True)
    for relative, source in sorted(files.items()):
        target = out / bundles.FILES / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, bytes):
            target.write_bytes(source)
        else:
            shutil.copyfile(source, target)
    text = bundles.render(
        {"name": label, "purpose": purpose, "kind": "launch"},
        {"gpu": bool(gpu), "display": True, "disk_gb": disk_gb, "own_jdk": True, "ram_gb": ram_needed(doc)},
        [{"path": bundles.FILES, "contents": True}],
        {"script": f"{bundles.FILES}/run.sh", "args": [bundles.REMOTE], "progress": "^RUN ", "done": "RUN done",
         "failed": "^RUN (verify|build|session) failed", "results": [f"{bundles.REMOTE}/results.tar", "/root/run.log"],
         "stop": "pkill -f launch_run.py", "partial": "cd /root/bundle && tar -cf results.tar results"},
        comment=f"made by vast.bundle-launch from launch {doc['project']}:{doc['launch']} at "
                f"{str(doc.get('commit'))[:12]}; {bundles.MANIFEST} holds every file's SHA-256 and "
                f"{bundles.FILES}/{INFO} what the bundle carries")
    (out / "bundle.toml").write_text(text, encoding="utf-8", newline="\n")
    entries = bundles.listed_files(out)
    tree = set_digest({e["path"]: e["sha256"] for e in entries})
    manifest = {"schema": bundles.SCHEMA, "name": label, "kind": "launch", "launch": info["launch"],
                "files": entries, "files_sha256": tree}
    write_atomic(out / bundles.MANIFEST, (json.dumps(manifest, indent=1) + "\n").encode("utf-8"))
    made = bundles.load(out, lambda v: v, verify_uploads=False)
    ctx.output("bundle", out)
    ctx.measure("upload_bytes", made.upload_bytes(), "B")
    if not eula:
        ctx.log.warning("the bundle records no acceptance of the Minecraft EULA ([lab] eula, or eula=true): its "
                        "instance builds, but its server stops at the EULA, so vast.rent refuses it before any quote")
    ctx.summary(f"bundle {label} from launch {doc['project']}:{doc['launch']} at {str(doc.get('commit'))[:12]}: "
                f"{len(carried)} members, {len(runtime)} runtime files, JDK {jdk['build']}, {len(sources)} tree(s) "
                f"and script(s), {made.upload_bytes() / 1e6:.0f} MB to upload")
    return {"bundle": ctx.host_path(out), "name": label, "problems": [], "files": len(entries),
            "bytes": made.upload_bytes(), "files_sha256": tree, "members": len(carried), "jdk": jdk["build"],
            "eula": eula}


def _shell(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _metadata() -> str:
    """The machine's [modules.lab] metadata, the URL template of the launcher metadata lab.build reads."""
    try:
        from wb import config as wbconfig
        return str(((wbconfig.load().values.get("modules") or {}).get("lab") or {}).get("metadata") or "")
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------------------------
# checking a made bundle against its launch file

def info(folder: Path) -> dict | None:
    """A launch bundle's ``bundle-launch.json``, or None for any other bundle."""
    path = Path(folder) / bundles.FILES / INFO
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ParamError(f"{bundles.FILES}/{INFO} is not JSON: {error}") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ParamError(f"{bundles.FILES}/{INFO} is not {SCHEMA}")
    return data


def verify(folder: Path) -> dict | None:
    """Refuses a launch bundle whose launch file, member, runtime file, JDK archive or tour is missing or is other
    bytes than the launch file and its set name; every problem is named. Returns the bundle's info (None for a bundle
    that is not a launch bundle)."""
    data = info(folder)
    if data is None:
        return None
    files = Path(folder) / bundles.FILES
    problems = []

    def check(relative: str, digest: str, what: str):
        path = files / relative
        if not path.is_file():
            problems.append(f"{what}: the bundle has no {relative}")
        elif sha256_file(path) != digest:
            problems.append(f"{what}: {relative} is {sha256_file(path)[:12]}, not {digest[:12]}")

    check(data["launch"]["file"], data["launch"]["sha256"], "the launch file")
    check(data["tour"]["file"], data["tour"]["sha256"], "the tour")
    doc = None
    if not problems:
        doc = json.loads((files / data["launch"]["file"]).read_bytes())
    if doc is not None:
        for member in doc.get("members") or []:
            check(f"{data['store']}/{member['path']}", member["sha256"], f"member {member['member']}")
        for part in RUNTIME_PARTS:
            entry = ((doc.get("runtime") or {}).get("files") or {}).get(part) or {}
            if entry.get("sha256"):
                check(f"{data['store']}/files/{entry['sha256']}/{entry['file']}", entry["sha256"], f"runtime {part}")
        if str((doc.get("runtime") or {}).get("jdk")) != str(data["jdk"]["build"]):
            problems.append(f"JDK: the bundle carries {data['jdk']['build']}, and the launch names "
                            f"{(doc.get('runtime') or {}).get('jdk')}")
    check(data["jdk"]["archive"], data["jdk"]["sha256"], f"JDK {data['jdk']['build']}")
    if problems:
        raise ParamError(f"launch bundle {data.get('name')}: " + "; ".join(problems) + " (make the bundle again: "
                         "vast.bundle-launch)")
    return data
