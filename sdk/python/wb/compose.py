"""``wb up``: the compose file is generated from the config folder (spec S9), so the published port, the
mounts and the secrets can never drift from the config.

- The API is published on 127.0.0.1 only; anything wider is refused (D3).
- A fresh token is written at every ``wb up`` to ``run/token`` (user-only) and mounted read-only (D14).
- Roots become bind mounts (``ro`` or ``rw``); secrets become read-only files under /run/secrets.
- State lives in a named volume (D22).
- The image carries no Workbench code. The code the container runs is mounted read-only at /opt/wb/release: the
  release in service (or the one wb deploy starts), whose first start copies it into the state volume, else this
  checkout, read in place (image/code.sh). So the image is rebuilt only when its own inputs change
  (``image_inputs``); its label records their digest, which wb deploy compares.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import secrets as pysecrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import config as wbconfig

DEFAULT_PORT = 7377
STATE = "/var/lib/wb"
# The folders Workbench itself writes under [paths] out, as container paths: the entrypoint gives each to Workbench's
# user. The old [modules.toolchain] pin_cache is still one of them for this release (see wbconfig.OLD_FOLDER_KEYS).
OWNED_FOLDERS = ("pin_cache", "records", "store")
RELEASE_MOUNT = "/opt/wb/release"  # the code the container runs, read-only (image/code.sh)
INPUTS_LABEL = "cc.orthrus.wb.inputs"  # the digest of the inputs an image was built from


class ComposeError(Exception):
    pass


def repo_root() -> Path | None:
    explicit = os.environ.get("WB_REPO")
    if explicit:
        return Path(explicit)
    candidate = Path(__file__).resolve().parents[3]
    return candidate if (candidate / "image" / "Dockerfile").exists() else None


@dataclass
class Code:
    """The Workbench code a container runs: a host folder, mounted read-only at RELEASE_MOUNT."""
    host: Path
    release: str = ""  # its release name, copied into the state volume at first start; "" for a checkout
    commit: str = ""   # a checkout's commit (a release names its own in RELEASE.json)


def code_source(config: wbconfig.Config, release: Path | None = None) -> Code | None:
    """What the container should run: ``release`` (wb deploy names the one it starts), else the release in service,
    else this checkout read in place (a machine with no release yet), else None (only an image with code inside
    could start)."""
    if release is None:
        from . import releases
        try:
            release = releases.serving(config)
        except (OSError, RuntimeError):
            release = None
    if release is not None:
        return Code(Path(release), Path(release).name)
    root = repo_root()
    if root is None or not (root / "core" / "wb_core").is_dir():
        return None
    from wb_core.identity import source_commit
    return Code(root, "", source_commit(root) or "")


def restrict(path: Path):
    """Makes a file readable by the current user only."""
    if os.name == "nt":
        user = os.environ.get("USERNAME") or getpass.getuser()
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                       capture_output=True, check=False)
    else:
        os.chmod(path, 0o600)


def write_secret_file(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    path.write_text(value, encoding="ascii")
    restrict(path)


def ensure_host_token(run: Path) -> Path:
    path = run / "host.token"
    if not path.exists():
        write_secret_file(path, pysecrets.token_urlsafe(32))
    return path


def owned_dirs(config: wbconfig.Config) -> list[str]:
    """The folders of OWNED_FOLDERS inside an rw root or the state volume: never a root itself, and nothing under a
    ro root. A folder made on the host, or by root, does not belong to the user Workbench runs as, so a job could not
    write there; the entrypoint, still root, makes each one and gives it and its contents to that user (WB_OWNED_DIRS)."""
    writable, readonly = [PurePosixPath(STATE)], []
    for name, root in wbconfig.roots(config).items():
        mount = PurePosixPath(wbconfig.root_mount(name, root))
        (writable if root.get("mode", "ro") == "rw" else readonly).append(mount)
    owned = []
    folders = wbconfig.folders(config, container=True)
    for value in [folders.get(key) for key in OWNED_FOLDERS] + [config.get("modules.toolchain.pin_cache")]:
        if not isinstance(value, str) or not value.startswith("/"):
            continue  # empty: the default under the state volume, which the entrypoint already owns
        path = PurePosixPath(value)
        if ".." in path.parts or any(path.is_relative_to(base) for base in readonly):
            continue
        if any(path != base and path.is_relative_to(base) for base in writable) and str(path) not in owned:
            owned.append(str(path))
    return owned


def generate(config: wbconfig.Config, run: Path, profile=None, warnings: list | None = None,
             code: Code | None = None) -> dict:
    """The compose document for this machine's platform profile (see wb.platform). A configured secret whose file
    is missing is left out with a warning: the commands that need it show as inactive, the rest of Workbench runs.
    ``code`` is mounted read-only for the entrypoint to run (``code_source``)."""
    warnings = warnings if warnings is not None else []
    from . import platform
    profile = profile or platform.profile()
    core = config.table("core")
    api = core.get("api", {}) if isinstance(core.get("api"), dict) else {}
    port = int(api.get("port", DEFAULT_PORT))
    publish = api.get("publish", "127.0.0.1")
    if publish not in ("127.0.0.1", "localhost", "::1"):
        raise ComposeError(f"core.api.publish = {publish!r}: Workbench is published on loopback only (D3)")
    image = (core.get("image") or {}).get("name", "wb-core:dev")
    volume = (core.get("image") or {}).get("volume", "wb-state")
    volumes = [f"{volume}:{STATE}"]
    if config.folder:
        volumes.append(f"{config.folder}:/etc/wb:ro")
    layer_mounts = [f"/etc/wb-layers/{index}" for index in range(len(config.layers))]
    for layer, mount in zip(config.layers, layer_mounts):
        volumes.append(f"{layer}:{mount}:ro")
    for name, root in wbconfig.roots(config).items():
        host = os.path.expanduser(root["host"])
        container = wbconfig.root_mount(name, root)
        mode = root.get("mode", "ro")
        if mode not in ("ro", "rw"):
            raise ComposeError(f"paths.roots.{name}.mode must be ro or rw")
        if not Path(host).exists():
            raise ComposeError(f"paths.roots.{name}.host {host} does not exist")
        volumes.append(f"{host}:{container}:{mode}")
    if code is not None:
        volumes.append(f"{code.host}:{RELEASE_MOUNT}:ro")
    secrets = {"wb_token": {"file": str(run / "token")}}
    service_secrets = ["wb_token"]
    host_token = run / "host.token"
    if host_token.exists():
        secrets["wb_host_token"] = {"file": str(host_token)}
        service_secrets.append("wb_host_token")
    for name, entry in (config.table("secrets") or {}).items():
        host = Path(os.path.expanduser(entry["host"]))
        if not host.exists():
            warnings.append(f"secret {name}: {host} does not exist; left out (commands that need it are inactive)")
            continue
        secrets[name] = {"file": str(host)}
        service_secrets.append(name)
    service = {
        "image": image,
        "restart": "unless-stopped",
        "init": True,
        "volumes": volumes,
        "secrets": service_secrets,
        "environment": {"WB_CONFIG": "/etc/wb", "WB_IN_CONTAINER": "1", "WB_HOST_URL": profile.host_url,
                        "WB_CONFIG_LAYERS": ":".join(layer_mounts)},  # the container's own path separator
        "stop_grace_period": "2m",
        "labels": {"cc.orthrus.wb.profile": f"{profile.os}/{profile.docker}"},
    }
    if code is not None:
        service["environment"]["WB_RELEASE"] = code.release
        if code.commit:
            service["environment"]["WB_SOURCE_COMMIT"] = code.commit
    owned = owned_dirs(config)
    if owned:
        service["environment"]["WB_OWNED_DIRS"] = ":".join(owned)  # the container's own path separator
    if profile.os == "linux" and hasattr(os, "getuid"):
        # Bind-mounted roots belong to this user; the image's entrypoint runs Workbench as them (image/entrypoint.sh).
        service["environment"]["WB_UID"] = str(os.getuid())
        service["environment"]["WB_GID"] = str(os.getgid())
    if profile.host_networking:
        # A native Linux engine: share the host's network and listen on its loopback only. Nothing is published,
        # and wb-host stays on 127.0.0.1 too.
        service["network_mode"] = "host"
        service["environment"]["WB_API_LISTEN"] = "127.0.0.1"
        service["environment"]["WB_API_PORT"] = str(port)
    else:
        # Docker Desktop: publish on the host's loopback; host.docker.internal reaches the host's loopback.
        service["ports"] = [f"127.0.0.1:{port}:{port}"]
        service["extra_hosts"] = ["host.docker.internal:host-gateway"]
    return {"name": "wb", "services": {"wb": service}, "volumes": {volume: {}}, "secrets": secrets}


def write(config: wbconfig.Config, warnings: list | None = None, release: Path | None = None) -> tuple[Path, int]:
    folder = config.folder or wbconfig.default_folder()
    run = folder / "run"
    run.mkdir(parents=True, exist_ok=True)
    write_secret_file(run / "token", pysecrets.token_urlsafe(32))
    document = generate(config, run, warnings=warnings, code=code_source(config, release))
    service = document["services"]["wb"]
    if service.get("network_mode") == "host":
        if service["environment"].get("WB_API_LISTEN") != "127.0.0.1":
            raise ComposeError("host networking must listen on 127.0.0.1 only")
        port = int(service["environment"]["WB_API_PORT"])
    else:
        port = int(service["ports"][0].split(":")[1])
        for mapping in service["ports"]:
            if not mapping.startswith(("127.0.0.1:", "[::1]:")):
                raise ComposeError(f"refusing to publish {mapping}: loopback only")
    path = run / "compose.generated.yaml"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")  # JSON is valid YAML
    (run / "endpoint.json").write_text(json.dumps({"url": f"http://127.0.0.1:{port}"}), encoding="utf-8")
    return path, port


def docker(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, text=True, capture_output=capture)


def up(config: wbconfig.Config, wait: float = 90, release: Path | None = None) -> dict:
    """Starts the container on ``release`` (default: the release in service, else this checkout) and waits until
    it answers."""
    warnings: list[str] = []
    path, port = write(config, warnings, release)
    for warning in warnings:
        print(f"wb: {warning}", file=sys.stderr)
    docker("compose", "-f", str(path), "up", "-d", "--remove-orphans")
    from .client import Client, WbError
    client = Client()
    deadline = time.time() + wait
    last = None
    while time.time() < deadline:
        try:
            return client.health()
        except WbError as error:
            last = error
            time.sleep(1)
    raise ComposeError(f"Workbench did not answer within {wait:.0f} s: {last}")


def down(config: wbconfig.Config):
    folder = config.folder or wbconfig.default_folder()
    path = folder / "run" / "compose.generated.yaml"
    if path.exists():
        docker("compose", "-f", str(path), "down")


def image_inputs(root: Path) -> list[Path]:
    """The files an image is built from, relative to ``root`` (a checkout or a release): the image folder (the
    Dockerfile, the entrypoint and code script, the pinned package, JDK and tool lists), every other source a COPY or
    ADD in the Dockerfile names, and .dockerignore. The image installs no Python requirements; a requirements file it
    ever copies is one of those sources. Workbench's own code is not an input: it runs from a release."""
    found: set[Path] = set()

    def add(path: Path):
        if path.is_dir():
            found.update(p.relative_to(root) for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        elif path.is_file():
            found.add(path.relative_to(root))

    add(root / "image")
    add(root / ".dockerignore")
    dockerfile = root / "image" / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8") if dockerfile.is_file() else ""
    for line in text.replace("\\\n", " ").splitlines():
        words = line.split()
        if not words or words[0].upper() not in ("COPY", "ADD") or any(w.startswith("--from") for w in words[1:]):
            continue  # not a copy, or one from another stage of this file
        for source in [w for w in words[1:-1] if not w.startswith("--")]:
            for path in (root.glob(source) if any(c in source for c in "*?[") else [root / source]):
                add(path)
    return sorted(found, key=lambda p: p.as_posix())


def inputs_digest(root: Path, tools: list[str] | None = None) -> str:
    """SHA-256 over the image's inputs (each path and its bytes, as Docker copies them) and the tools built in."""
    digest = hashlib.sha256()
    for relative in image_inputs(root):
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256((root / relative).read_bytes()).hexdigest().encode("ascii") + b"\n")
    digest.update(("tools=" + ",".join(sorted(tools or []))).encode("utf-8"))
    return digest.hexdigest()


def image_label(tag: str) -> str | None:
    """The inputs digest the image ``tag`` was built from: None when there is no such image, "" when it records
    none (an image from before the label)."""
    done = subprocess.run(["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", tag],
                          capture_output=True, text=True)
    if done.returncode != 0:
        return None
    try:
        labels = json.loads(done.stdout.strip() or "null") or {}
    except ValueError:
        labels = {}
    return str(labels.get(INPUTS_LABEL) or "")


def build_reason(tag: str, digest: str, label: str | None) -> str | None:
    """Why the image must be built, or None when the image ``tag`` was built from these inputs (``label``, from
    ``image_label``)."""
    if label is None:
        return f"there is no image {tag}"
    if not label:
        return f"the image {tag} records no inputs digest (it was built with Workbench's code inside)"
    if label != digest:
        return f"the image's inputs changed (built from {label[:12]}, now {digest[:12]})"
    return None


def build(tag: str = "wb-core:dev", tools: list[str] | None = None, root: Path | None = None) -> int:
    """Builds the image from ``root`` (default: this checkout), labelled with its inputs' digest; ``tools`` names
    optional tools image/pins.toml pins ([tool.<name>]) to build in."""
    root = root or repo_root()
    if root is None:
        raise ComposeError("wb build needs a Workbench checkout (set WB_REPO)")
    tools = list(tools or [])
    if tools:
        import tomllib
        pinned = tomllib.loads((root / "image" / "pins.toml").read_text(encoding="utf-8")).get("tool") or {}
        unknown = [name for name in tools if name not in pinned]
        if unknown:
            raise ComposeError(f"[core.image] tools names {unknown[0]}, which image/pins.toml does not pin "
                               f"(pinned: {', '.join(sorted(pinned)) or 'none'})")
    command = ["docker", "build", "-f", str(root / "image" / "Dockerfile"), "-t", tag]
    if tools:
        command += ["--build-arg", "TOOLS=" + ",".join(tools)]
    command += ["--label", f"{INPUTS_LABEL}={inputs_digest(root, tools)}"]
    return subprocess.run([*command, str(root)]).returncode


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(generate(wbconfig.load(), wbconfig.default_folder() / "run"), indent=2))
    sys.exit(0)
