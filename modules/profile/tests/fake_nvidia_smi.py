"""A stand-in for nvidia-smi with two devices, for profile.gpu-sample's tests: --help-query-gpu, and --query-gpu
once or looping (-lms) in the csv,noheader,nounits form, as the real tool answers.

Device 0 idles (P8) for two rounds, then runs at P0, held by its power limit every fourth round; device 1 reports
no power draw and no memory utilisation. Knobs: FAKE_GPU_REFUSE (fields it does not know, comma-separated) and
FAKE_GPU_ROUNDS (rounds before it fails, as a lost device does)."""

import os
import sys
import time
from datetime import datetime

NAMES = [("timestamp",), ("index",), ("name", "gpu_name"), ("kmd_version",), ("driver_version",), ("pstate",),
         ("clocks_event_reasons.active", "clocks_throttle_reasons.active"), ("memory.total",), ("memory.used",),
         ("utilization.gpu",), ("utilization.memory",), ("temperature.gpu",), ("power.draw",),
         ("enforced.power.limit", "gpu.power.current_limit"), ("clocks.current.graphics", "clocks.gr"),
         ("clocks.current.sm", "clocks.sm"), ("clocks.current.memory", "clocks.mem")]
CANONICAL = {alias: names[0] for names in NAMES for alias in names}
HELP = 'List of valid properties to query for the switch "--query-gpu":\n\n' + "\n\n".join(
    " or ".join(f'"{n}"' for n in names) + "\nWhat the field holds." for names in NAMES) + "\n"


def cell(field: str, device: int, round_: int, now: str) -> str:
    busy, capped = round_ >= 2, round_ >= 2 and round_ % 4 == 3
    clock = ("1395" if capped else "1845") if busy else "210"
    values = {
        "timestamp": now, "index": str(device), "name": f"Example GPU {'AB'[device]}", "kmd_version": "999.01",
        "driver_version": "999.01", "pstate": "P0" if busy else "P8",
        "clocks_event_reasons.active": ("0x0000000000000004" if capped else "0x0000000000000000") if busy
        else "0x0000000000000001",
        "memory.total": "12288", "memory.used": str(2600 + round_), "utilization.gpu": "97" if busy else "2",
        "utilization.memory": "N/A" if device else "40", "temperature.gpu": str(40 + min(round_, 30)),
        "power.draw": "[N/A]" if device else ("165.50" if busy else "17.10"), "enforced.power.limit": "170.00",
        "clocks.current.graphics": clock, "clocks.current.sm": clock,
        "clocks.current.memory": "7500" if busy else "405"}
    return values[CANONICAL[field]]


def main(args: list[str]) -> int:
    if "--help-query-gpu" in args:
        print(HELP)
        return 0
    query = next((a.split("=", 1)[1] for a in args if a.startswith("--query-gpu=")), None)
    if not query:
        print("fake nvidia-smi: give --query-gpu or --help-query-gpu")
        return 2
    fields = query.split(",")
    refused = set(filter(None, os.environ.get("FAKE_GPU_REFUSE", "").split(",")))
    for name in fields:
        if name not in CANONICAL or name in refused:
            print(f'Field "{name}" is not a valid field to query.\n')
            return 2
    ids = [a.split("=", 1)[1] for a in args if a.startswith("--id=")]
    devices = [d for d in (0, 1) if not ids or str(d) == ids[0]]
    if not devices:
        print("No devices were found")
        return 6
    period = int(args[args.index("-lms") + 1]) / 1000 if "-lms" in args else None
    rounds = int(os.environ.get("FAKE_GPU_ROUNDS") or 0)
    began, round_ = time.monotonic(), 0
    while True:
        now = datetime.now().strftime("%Y/%m/%d %H:%M:%S.%f")[:-3]
        try:
            for device in devices:
                print(", ".join(cell(name, device, round_, now) for name in fields), flush=True)
        except OSError:
            return 0  # the reader went away
        round_ += 1
        if period is None:
            return 0
        if rounds and round_ >= rounds:
            print("Unable to determine the device handle for GPU0: GPU is lost.", file=sys.stderr, flush=True)
            return 15
        if time.monotonic() - began > 120:
            return 0  # never outlive a test
        time.sleep(period)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
