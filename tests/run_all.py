"""Every Workbench test suite, one after another: python tests/run_all.py [--quick]. Exit 0 only if all pass."""

import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUITES = [
    ["image/pins.py", "check"],
    ["schema/generate.py", "--check"],
    ["tests/test_core.py"],
    ["tests/test_platform.py"],
    ["tests/test_checks.py"],
    ["tests/test_runs.py"],
    ["tests/test_ci.py"],
    ["tests/test_plan.py"],
    ["tests/test_host.py"],
    ["tests/test_store.py"],
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
    ["modules/lab/tests/test_profiles.py"],
    ["modules/lab/tests/test_snapshots.py"],
    ["modules/lab/tests/test_assemble.py"],
    ["modules/lab/tests/test_session.py"],
    ["modules/lab/tests/test_install.py"],
    ["modules/lab/tests/test_userdev.py"],
    ["modules/lab/tests/test_pack.py"],
    ["modules/content/tests/test_content.py"],
    ["modules/drive/tests/test_drive.py"],
    ["modules/release/tests/test_release.py"],
    ["modules/build/tests/test_build.py"],
    ["modules/toolchain/tests/test_toolchain.py"],
    ["tests/test_sdks.py"],
]
VENDORED = ["-m", "unittest", "discover", "-s", "tests", "-t", "."]  # run in modules/tools with src on the path


def main() -> int:
    quick = "--quick" in sys.argv
    failed = []
    for suite in SUITES:
        if quick and suite[0] in ("tests/test_sdks.py", "tests/test_host.py", "tests/test_store.py"):
            continue
        started = time.time()
        done = subprocess.run([sys.executable, "-W", "ignore", *suite], cwd=REPO, capture_output=True, text=True)
        tail = (done.stdout + done.stderr).strip().splitlines()[-1:] or [""]
        print(f"{'ok  ' if done.returncode == 0 else 'FAIL'} {' '.join(suite):<44} {time.time() - started:5.1f}s  {tail[0]}")
        if done.returncode != 0:
            failed.append(suite[0])
    import os
    env = dict(os.environ, PYTHONPATH=str(REPO / "modules" / "tools" / "src"))
    started = time.time()
    done = subprocess.run([sys.executable, "-W", "ignore", *VENDORED], cwd=REPO / "modules" / "tools", env=env,
                          capture_output=True, text=True)
    summary = [line for line in (done.stdout + done.stderr).splitlines() if line.startswith(("Ran ", "OK", "FAILED"))]
    print(f"{'ok  ' if done.returncode == 0 else 'note'} {'modules/tools (former standalone tools)':<44} "
          f"{time.time() - started:5.1f}s  {' '.join(summary)}")
    print(f"\n{'all suites passed' if not failed else 'failed: ' + ', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
