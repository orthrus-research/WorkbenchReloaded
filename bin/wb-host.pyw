"""Starts wb-host from this checkout with no console window and no global PYTHONPATH (the Windows logon task runs
this under pythonw). Output goes to the run folder's wb-host.log."""
import os
import sys
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(repo / "sdk" / "python"), str(repo / "core"), str(repo / "host")]
run = Path(os.environ.get("WB_CONFIG") or Path.home() / ".config" / "wb") / "run"
run.mkdir(parents=True, exist_ok=True)
log = open(run / "wb-host.log", "a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = log

from wb_host.cli import main  # noqa: E402

sys.exit(main(["serve"]))
