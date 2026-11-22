"""Reaching a rented machine: SSH and scp with the lab's key. The key arrives as a mounted secret whose
permissions ssh may reject (Docker Desktop mounts files world-readable), so a private copy with mode 0600
is made in the job's work folder and removed when the job ends."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


class Transport:
    """SSH to one host. Tests substitute a fake with the same three methods."""

    def __init__(self, host: str, port: int, key: Path, known_hosts: Path):
        self.host, self.port, self.key, self.known_hosts = host, int(port), key, known_hosts

    def _options(self) -> list[str]:
        return ["-i", str(self.key), "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "ServerAliveInterval=30",
                "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes"]

    def run(self, command: str, timeout: float = 600, check: bool = True) -> str:
        done = subprocess.run(["ssh", *self._options(), "-p", str(self.port), f"root@{self.host}", command],
                              capture_output=True, text=True, timeout=timeout)
        if check and done.returncode != 0:
            raise RuntimeError(f"remote {command[:60]!r} exited {done.returncode}: {done.stderr[-600:]}")
        return done.stdout

    def put(self, files: list[Path], destination: str, timeout: float = 3600):
        if not files:
            return
        subprocess.run(["scp", *self._options(), "-P", str(self.port), "-r", "-q", *map(str, files),
                        f"root@{self.host}:{destination}"], check=True, timeout=timeout)

    def get(self, remote: list[str], local: Path, timeout: float = 1800) -> list[Path]:
        local.mkdir(parents=True, exist_ok=True)
        got = []
        for path in remote:
            done = subprocess.run(["scp", *self._options(), "-P", str(self.port), "-q", f"root@{self.host}:{path}",
                                   str(local)], timeout=timeout, capture_output=True, text=True)
            if done.returncode == 0:
                got.append(local / Path(path).name)
        return got


def private_key_copy(secret: Path, work: Path) -> Path:
    folder = work / ".ssh"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "id"
    shutil.copyfile(secret, target)
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    return target


def public_key(private: Path, pub_secret: Path | None = None) -> str:
    if pub_secret and pub_secret.is_file():
        text = pub_secret.read_text(encoding="ascii").strip()
    else:
        done = subprocess.run(["ssh-keygen", "-y", "-f", str(private)], capture_output=True, text=True, timeout=30)
        if done.returncode != 0:
            raise RuntimeError(f"cannot derive the public key: {done.stderr.strip()} (a passphrase-free key is needed)")
        text = done.stdout.strip()
    if not text.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
        raise RuntimeError("the lab's public key is not an SSH public key")
    return text


def deadman(seconds: int) -> str:
    """The remote dead man's switch (S15): the container stops itself after ``seconds``, ending GPU billing even if
    Workbench or this PC is gone."""
    return (f"nohup sh -c 'sleep {int(seconds)}; echo \"RUN deadman: stopping\" >> /root/run.log; kill -TERM 1; "
            f"sleep 30; kill -KILL 1' >/dev/null 2>&1 < /dev/null &")
