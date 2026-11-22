"""profile.settings: JFR settings (a .jfc file) that record what profile.jfr and profile.attribute read.

Every event the analyses use is enabled with a stack trace where one is useful; waits, file I/O and
safepoints over a threshold; thread CPU load often enough to split CPU between owners; and, for the
methods of a stage map (or any others), JFR's method tracing (``jdk.MethodTrace``, JDK 25+). Older
runtimes ignore the settings of events they do not have; TLAB allocation events are the only allocation
record a runtime before JDK 16 keeps.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .stages import filter_name

WAITED = ("jdk.ThreadPark", "jdk.ThreadSleep", "jdk.JavaMonitorEnter", "jdk.JavaMonitorWait", "jdk.FileRead",
          "jdk.FileWrite", "jdk.FileForce", "jdk.SafepointBegin", "jdk.ExecuteVMOperation")
JVM_OPTIONS = ["-XX:+UnlockDiagnosticVMOptions", "-XX:+DebugNonSafepoints"]


def _span(seconds: float) -> str:
    nanos = round(seconds * 1e9)
    for unit, size in (("s", 10**9), ("ms", 10**6), ("us", 10**3)):
        if nanos and nanos % size == 0:
            return f"{nanos // size} {unit}"
    return f"{nanos} ns"


def settings(methods: list[str], *, sample_period: float, threshold: float, method_threshold: float,
             allocation_rate: str, cpu_period: float, compilation_threshold: float, tlab: bool) -> dict[str, dict]:
    """Event name -> its settings (each enabled)."""
    out: dict[str, dict] = {
        "jdk.ExecutionSample": {"period": _span(sample_period)},
        "jdk.NativeMethodSample": {"period": _span(sample_period)},
        "jdk.ObjectAllocationSample": {"throttle": allocation_rate, "stackTrace": "true"},
        "jdk.GarbageCollection": {},
        "jdk.GCPhasePause": {"threshold": "0 ms"},
        "jdk.ThreadCPULoad": {"period": _span(cpu_period)},
        "jdk.CPULoad": {"period": _span(cpu_period)},
        "jdk.Compilation": {"threshold": _span(compilation_threshold)},
        "jdk.Deoptimization": {"stackTrace": "false"},
        "jdk.CompilationFailure": {},
        "jdk.DataLoss": {},
        "jdk.ZAllocationStall": {"stackTrace": "true", "threshold": "0 ms"},
        "jdk.JVMInformation": {"period": "beginChunk"},
        "jdk.CPUInformation": {"period": "beginChunk"},
    }
    for name in WAITED:
        out[name] = {"threshold": _span(threshold), "stackTrace": "true"}
    if tlab:
        out["jdk.ObjectAllocationInNewTLAB"] = {"stackTrace": "true"}
        out["jdk.ObjectAllocationOutsideTLAB"] = {"stackTrace": "true"}
    if methods:
        out["jdk.MethodTrace"] = {"filter": ";".join(filter_name(m) for m in methods),
                                  "threshold": _span(method_threshold), "stackTrace": "true"}
    return out


def write(path: Path, events: dict[str, dict], label: str) -> None:
    root = ET.Element("configuration", version="2.0", label=label, provider="Workbench",
                      description="Events the profile commands read; sampling and tracing add overhead.")
    for name, values in events.items():
        event = ET.SubElement(root, "event", name=name)
        for key, value in {"enabled": "true", **values}.items():
            ET.SubElement(event, "setting", name=key).text = value
    ET.indent(root)
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n")
