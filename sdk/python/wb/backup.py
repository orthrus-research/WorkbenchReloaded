"""``wb backup``: the state that cannot be rebuilt, copied off the drive the projects and the lab live on.

    wb backup [--to DIR]

It takes SQLite's online backup of the daemon's database (jobs, their records and the ledger) while the daemon
runs, and copies beside it the machine's scrub rules file, a git bundle of the Set's home (every landing, even one
its public remote has lost), and the seeded Store entries (bytes no container can build, with their sources), into
<DIR>/<date-time>/ with a manifest of what each file holds. DIR is [paths] backup in machine config, else wb-backup in
the user's home folder. ``wb setup --yes`` runs it nightly from the operating system's scheduler. Once a backup is
made, the backups it made before beyond the newest ``--keep`` (14) are removed: only folders named as it names them
that hold its manifest, never anything else in DIR."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config as wbconfig

CONTAINER = "wb-wb-1"
KEEP = 14  # backups kept: two weeks of nightly runs
_MADE = re.compile(r"\d{8}-\d{6}\Z")  # how main() names a backup's folder
STATE_DB = "/var/lib/wb/db/wb.sqlite"
SNAPSHOT = "/var/lib/wb/db/backup.sqlite"


def _target(config, given: str | None) -> Path:
    if given:
        return Path(given)
    configured = config.get("paths.backup")
    return Path(os.path.expanduser(str(configured))) if configured else Path.home() / "wb-backup"


def snapshot_script(source: str, target: str) -> str:
    """The program the container's Python runs for the copy: SQLite's online backup of ``source`` (read-only, while
    the daemon writes it) into ``target``, a database that opens on its own, every job in the state it had."""
    return (f"import sqlite3; s = sqlite3.connect('file:{source}?mode=ro', uri=True); "
            f"d = sqlite3.connect('{target}'); s.backup(d); d.close(); s.close()")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _counts(path: Path) -> dict:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT state, COUNT(*) FROM job GROUP BY state").fetchall()
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        ledger = {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables
                  if t.startswith("ledger_")}
        return {"jobs": dict(rows), "ledger": ledger, "check": connection.execute("PRAGMA quick_check").fetchone()[0]}
    finally:
        connection.close()


def _set_bundle(config, folder: Path) -> dict:
    home = str((config.values.get("workspace") or {}).get("set") or "")
    if not home or not (Path(home) / "HEAD").is_file():
        return {}
    target = folder / "set.bundle"
    done = subprocess.run(["git", "bundle", "create", str(target), "--all"], cwd=home, capture_output=True, text=True)
    if done.returncode != 0:
        print(f"wb: the Set could not be bundled: {done.stderr.strip()[-300:]}", file=sys.stderr)
        return {}
    head = subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=home, capture_output=True, text=True).stdout
    return {"set.bundle": {"sha256": _sha256(target), "holds": f"the Set's home, main at {head.strip()[:12]}"}}


def _seeded(config, folder: Path) -> dict:
    """Seeded Store entries with their notes: the one part of the Store no build can make again."""
    from .workspace import Workspace
    store = Workspace(config).store_root()
    if store is None or not (store / "seeded").is_dir():
        return {}
    kept = {}
    for note in sorted((store / "seeded").glob("*.json")):
        meta = json.loads(note.read_text(encoding="utf-8"))
        source = store / "files" / meta["sha256"] / meta["name"]
        if not source.is_file():
            continue
        target = folder / "seeded" / meta["sha256"] / meta["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        shutil.copy2(note, folder / "seeded" / note.name)
        kept[f"seeded/{meta['sha256']}/{meta['name']}"] = {"sha256": _sha256(target),
                                                           "holds": f"seeded: {meta.get('as')}"}
    return kept


def main(args) -> int:
    config = wbconfig.load()
    folder = _target(config, args.to) / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    script = snapshot_script(STATE_DB, SNAPSHOT)
    try:
        subprocess.run(["docker", "exec", CONTAINER, "python3", "-c", script], check=True, capture_output=True,
                       text=True)
        subprocess.run(["docker", "cp", f"{CONTAINER}:{SNAPSHOT}", str(folder / "wb.sqlite")], check=True,
                       capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        print(f"wb: the database could not be backed up: {detail.strip()[-300:]}", file=sys.stderr)
        return 3
    finally:
        subprocess.run(["docker", "exec", CONTAINER, "rm", "-f", SNAPSHOT], capture_output=True)
    manifest = {"made": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": {}}
    database = folder / "wb.sqlite"
    manifest["files"]["wb.sqlite"] = {"sha256": _sha256(database), "bytes": database.stat().st_size,
                                      "holds": "jobs, their events and records, the ledger", **_counts(database)}
    rules = Path(config.folder or wbconfig.default_folder()) / "scrub-rules.toml"
    if rules.is_file():
        shutil.copy2(rules, folder / rules.name)
        manifest["files"][rules.name] = {"sha256": _sha256(folder / rules.name), "holds": "the machine's scrub rules"}
    manifest["files"].update(_set_bundle(config, folder))
    manifest["files"].update(_seeded(config, folder))
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    jobs = manifest["files"]["wb.sqlite"]["jobs"]
    print(f"backed up to {folder}: {sum(jobs.values())} jobs ({', '.join(f'{n} {s}' for s, n in sorted(jobs.items()))}), "
          f"integrity {manifest['files']['wb.sqlite']['check']}")
    if manifest["files"]["wb.sqlite"]["check"] == "ok":  # a good backup is made before an older one goes
        gone = prune(folder.parent, getattr(args, "keep", None) or KEEP)
        if gone:
            print(f"removed {len(gone)} older backup(s), keeping the newest {getattr(args, 'keep', None) or KEEP}")
    return 0


def prune(folder: Path, keep: int = KEEP) -> list[Path]:
    """Removes the backups this command made in ``folder`` beyond the newest ``keep``: folders named <date-time>
    that hold its manifest. Nothing else there is touched. Returns what it removed."""
    made = sorted(p for p in folder.iterdir() if p.is_dir() and _MADE.match(p.name) and (p / "manifest.json").is_file())
    gone = made[:-keep] if keep > 0 else []
    for old in gone:
        shutil.rmtree(old, ignore_errors=True)
    return gone
