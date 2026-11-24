"""Every Workbench test suite, one after another: python tests/run_all.py [--quick] [--part PART]. Exit 0 only if
all pass.

The suites fall in four parts, which Workbench's own project.toml runs as four checks: ``generated`` (lock files,
schema types and command tables that must match their sources), ``core`` (tests/), ``modules`` (each module's
tests) and ``engine`` (the vendored actions engine and tools). With no --part, every part runs, as wb deploy does."""

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUITES = [
    ["image/pins.py", "check"],
    ["schema/generate.py", "--check"],
    ["tests/test_command_tables.py"],
    ["tests/test_core.py"],
    ["tests/test_scheduler.py"],
    ["tests/test_process.py"],
    ["tests/test_cli.py"],
    ["tests/test_workspace.py"],
    ["tests/test_doctor.py"],
    ["tests/test_work.py"],
    ["tests/test_releases.py"],
    ["tests/test_gc.py"],
    ["tests/test_artifacts.py"],
    ["tests/test_platform.py"],
    ["tests/test_checks.py"],
    ["tests/test_runs.py"],
    ["tests/test_ci.py"],
    ["tests/test_plan.py"],
    ["tests/test_host.py"],
    ["modules/vast/tests/test_vast.py"],
    ["modules/ledger/tests/test_ledger.py"],
    ["modules/frames/tests/test_qframes.py"],
    ["modules/world/tests/test_world.py"],
    ["modules/render/tests/test_render.py"],
    ["modules/assets/tests/test_assets.py"],
    ["modules/profile/tests/test_profile.py"],
    ["modules/jar/tests/test_jar.py"],
    ["modules/mixin/tests/test_mixin.py"],
    ["modules/probe/tests/test_probe.py"],
    ["modules/lab/tests/test_server.py"],
    ["tests/test_land.py"],
    ["tests/test_platform_fetch.py"],
    ["tests/test_gradlehome.py"],
    ["modules/lab/tests/test_assemble.py"],
    ["modules/lab/tests/test_install.py"],
    ["modules/lab/tests/test_pack.py"],
    ["modules/lab/tests/test_resolve.py"],
    ["modules/lab/tests/test_instance.py"],
    ["modules/lab/tests/test_run.py"],
    ["modules/lab/tests/test_play.py"],
    ["modules/content/tests/test_content.py"],
    ["modules/drive/tests/test_drive.py"],
    ["modules/release/tests/test_release.py"],
    ["modules/build/tests/test_build.py"],
    ["modules/toolchain/tests/test_toolchain.py"],
    ["tests/test_sdks.py"],
]
VENDORED = ["-m", "unittest", "discover", "-s", "tests", "-t", "."]  # run in modules/tools with src on the path
PARTS = ("generated", "core", "modules", "engine")
GENERATED = {"image/pins.py", "schema/generate.py", "tests/test_command_tables.py"}


def part_of(suite: list[str]) -> str:
    if suite[0] in GENERATED:
        return "generated"
    return "modules" if suite[0].startswith("modules/") else "core"


def main() -> int:
    parser = argparse.ArgumentParser(description="Every Workbench test suite.")
    parser.add_argument("--quick", action="store_true", help="skip the slowest suites")
    parser.add_argument("--part", action="append", choices=PARTS, help="only this part (repeatable)")
    args = parser.parse_args()
    quick, parts = args.quick, set(args.part or PARTS)
    failed = []
    for suite in SUITES:
        if part_of(suite) not in parts:
            continue
        if quick and suite[0] in ("tests/test_sdks.py", "tests/test_host.py"):
            continue
        started = time.time()
        done = subprocess.run([sys.executable, "-W", "ignore", *suite], cwd=REPO, capture_output=True, text=True)
        tail = (done.stdout + done.stderr).strip().splitlines()[-1:] or [""]
        print(f"{'ok  ' if done.returncode == 0 else 'FAIL'} {' '.join(suite):<44} {time.time() - started:5.1f}s  {tail[0]}")
        if done.returncode != 0:
            failed.append(suite[0])
    import os
    if "engine" not in parts:
        print(f"\n{'all suites passed' if not failed else 'failed: ' + ', '.join(failed)}")
        return 1 if failed else 0
    env = dict(os.environ, PYTHONPATH=str(REPO / "modules" / "tools" / "src"))
    started = time.time()
    done = subprocess.run([sys.executable, "-W", "ignore", *VENDORED], cwd=REPO / "modules" / "tools", env=env,
                          capture_output=True, text=True)
    summary = [line for line in (done.stdout + done.stderr).splitlines() if line.startswith(("Ran ", "OK", "FAILED"))]
    print(f"{'ok  ' if done.returncode == 0 else 'FAIL'} {'modules/tools (the actions engine and tools)':<44} "
          f"{time.time() - started:5.1f}s  {' '.join(summary)}")
    if done.returncode != 0:
        failed.append("modules/tools")
    print(f"\n{'all suites passed' if not failed else 'failed: ' + ', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
