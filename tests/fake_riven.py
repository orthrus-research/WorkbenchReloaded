"""A fake Riven checkout for tests: tools/runtime.py prepares instances shaped like the real ones (server
libraries, mods on both sides, natives, a manifest naming the instance's own folder) and load() checks them the
way the real runtime.load does before a play."""

from pathlib import Path

# The fake launcher's instance profile: the same shape as a real launcher's, written as config.
PROFILE_TOML = r'''
launcher = "tools/runtime.py"
instances = ".local/cubic-standalone"
prepare = ["{python}", "tools/runtime.py", "prepare", "--name", "{name}"]
seed_flag = "--seed"
scale_flag = "--scale"
play = ["{python}", "tools/runtime.py", "play", "--name", "{name}", "--tour", "{tour}", "--window", "{window}"]
options_env = "JAVA_TOOL_OPTIONS"
load = ["{python}", "-c", "import sys; sys.path.insert(0, 'tools'); import runtime; runtime.load({name_repr}); print('VERIFIED')"]
load_ok = "VERIFIED"
recipe_files = ["runtime.lock.json", "tools/runtime.py", "tools/checkouts.py", "{cubature}/tools/cubic_standalone.py"]
default_artifact = { flag = "--artifact", path = "build/libs/riven-0.0.1-cleanroom.jar" }
file_flags = ["--cubic-artifact", "--client-mod", "--artifact"]
manifest = "standalone.json"
world_key = { seed = "seed", scale = "generator.layoutScale", definitions = "#generator.definitions" }
world_writer = "cubicJar.sha256"
world_parts = ["server/world", "server/world.cubature-cache", "client/Distant_Horizons_server_data"]
rewrite = ["standalone.json", "client/client-runtime.json"]
linkable_dirs = ["server/libraries", "client/natives"]
linkable_suffixes = [".jar"]
packs_dir = "client/shaderpacks"
lab_root = ".local"
stays = ["toolchains"]
lab_items = [["cubic-standalone", "instances"], ["cubic-lab", "lab instances"], ["frozen", "frozen builds"],
             ["builds", "build records"], ["actions", "ci pass records"], ["downloads", "downloads"],
             ["coordination", "lanes"], ["settings.json", "JDK paths"]]

[checkouts]
cubature = "../Cubature"
'''

RUNTIME = r'''
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STANDALONE = ROOT / ".local" / "cubic-standalone"


def target(name):
    return STANDALONE / name


def prepare(name, seed, args):
    home = target(name)
    if home.exists():
        raise SystemExit("instance exists: " + name)
    with open(ROOT / "prepare-calls.txt", "a", encoding="utf-8") as log:
        log.write(name + "\n")
    server, client = home / "server", home / "client"
    (server / "libraries" / "org" / "x").mkdir(parents=True)
    (server / "libraries" / "org" / "x" / "lib.jar").write_bytes(b"L" * 4096)
    for side in (server, client):
        (side / "mods").mkdir(parents=True)
        (side / "mods" / "riven.jar").write_bytes(b"R" * 2048)
        (side / "config").mkdir()
        (side / "config" / "cubicchunks.cfg").write_text("general {\n}\n", encoding="utf-8")
    for arg in args:
        if arg.endswith(".jar") and Path(arg).is_file():
            (client / "mods" / Path(arg).name).write_bytes(Path(arg).read_bytes())
    (client / "natives").mkdir()
    (client / "natives" / "lwjgl.dll").write_bytes(b"N" * 1024)
    (client / "options.txt").write_text("fancyGraphics:true\n", encoding="utf-8")
    (server / "world").mkdir()
    (client / "client-runtime.json").write_text(json.dumps({"natives": str(client / "natives")}), encoding="utf-8")
    (home / "installer.log").write_text("installed\n", encoding="utf-8")
    manifest = {"name": name, "server": str(server), "client": str(client), "seed": seed,
                "generator": {"layoutScale": 1.0, "definitions": {"riven-base.json": {"revision": 1}}},
                "cubicJar": {"sha256": "c" * 64}}
    (home / "standalone.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")


def load(name):
    home = target(name)
    manifest = json.loads((home / "standalone.json").read_text(encoding="utf-8"))
    if manifest["name"] != name or Path(manifest["server"]) != home / "server":
        raise RuntimeError("instance manifest names another folder")
    runtime = json.loads((home / "client" / "client-runtime.json").read_text(encoding="utf-8"))
    if Path(runtime["natives"]) != home / "client" / "natives":
        raise RuntimeError("client runtime names another folder")
    for path in (home / "server/libraries/org/x/lib.jar", home / "server/mods/riven.jar", home / "client/mods/riven.jar"):
        if not path.is_file():
            raise RuntimeError("missing " + str(path))
    return home, manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("--name", required=True)
    parser.add_argument("--seed", type=int, default=8675309)
    parser.add_argument("--scale", type=int)
    known, rest = parser.parse_known_args()
    if known.command == "prepare":
        prepare(known.name, known.seed, rest)
    else:
        raise SystemExit("unsupported in the fake: " + known.command)
'''


def make(root: Path) -> Path:
    riven = Path(root)
    (riven / "tools").mkdir(parents=True, exist_ok=True)
    (riven / "tools" / "runtime.py").write_text(RUNTIME, encoding="utf-8")
    (riven / "tools" / "checkouts.py").write_text("# fake\n", encoding="utf-8")
    (riven / "runtime.lock.json").write_text('{"profile": "cleanroom"}\n', encoding="utf-8")
    (riven / "build" / "libs").mkdir(parents=True, exist_ok=True)
    (riven / "build" / "libs" / "riven-0.0.1-cleanroom.jar").write_bytes(b"R" * 2048)
    (riven / ".local" / "cubic-standalone").mkdir(parents=True, exist_ok=True)
    return riven


def prepare_calls(riven: Path) -> list[str]:
    path = Path(riven) / "prepare-calls.txt"
    return path.read_text(encoding="utf-8").split() if path.exists() else []
