"""Every tool this package ships, in one place: the umbrella command, Workbench's tools module and the
consistency tests all read this list, so a tool missing from it is missing everywhere."""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType

BOTH = ("windows", "linux")
ALL = ("windows", "linux", "macos")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    """The umbrella subcommand: ``orthrus-tools <name>``."""
    module: str
    """The module under ``orthrus_tools``."""
    summary: str
    use_when: str
    doc: str
    """Repository-relative path of the tool's documentation."""
    kind: str = "command"
    """``command`` (has a CLI) or ``library`` (import it)."""
    platforms: tuple[str, ...] = ALL
    requires: str | None = None
    """The pip extra the tool needs, if any."""
    api: tuple[str, ...] = field(default_factory=tuple)
    """Public entry points to import, for a library or a command that is also a library."""
    tests: tuple[str, ...] = field(default_factory=tuple)
    """Test modules covering it; ``tests/test_<module>.py`` when empty."""

    @property
    def alias(self) -> str | None:
        """The standalone console script, kept for existing scripts and hooks."""
        return f"orthrus-{self.name}" if self.kind == "command" else None

    @property
    def test_files(self) -> tuple[str, ...]:
        return self.tests or (f"tests/test_{self.module}.py",)

    def load(self) -> ModuleType:
        return importlib.import_module(f"orthrus_tools.{self.module}")

    def build_parser(self) -> argparse.ArgumentParser:
        build: Callable[[], argparse.ArgumentParser] = self.load().build_parser
        return build()

    def main(self, argv: list[str]) -> int:
        entry: Callable[[list[str]], int] = self.load().main
        return entry(argv)


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="frames",
        module="frames",
        summary="Compare captured PNG frames against references under a tolerance profile.",
        use_when="a render or capture must match a reference image set, or to measure capture noise",
        doc="docs/frames.md",
        requires="frames",
        api=("compare", "measure_runs", "suggest_profile", "Profile"),
    ),
    ToolSpec(
        name="mod-survey",
        module="mod_survey",
        summary="List the GL calls and rendering-class patches each Minecraft mod JAR refers to.",
        use_when="checking which mods touch GL state or patch rendering classes, without running them",
        doc="docs/mod-survey.md",
        api=("survey_jar", "summarize"),
    ),
    ToolSpec(
        name="strict-math",
        module="strict_math",
        summary="Ratchet inexact java.lang.Math calls toward StrictMath in deterministic Java code.",
        use_when="a Java project must produce bit-identical output on every CPU and JDK",
        doc="docs/strict-math.md",
    ),
    ToolSpec(
        name="pins",
        module="pins",
        summary="Download third-party inputs pinned by SHA-256 and verify them offline.",
        use_when="a build needs JARs or archives that are not committed",
        doc="docs/pins.md",
    ),
    ToolSpec(
        name="jdk",
        module="jdk",
        summary="Find an installed JDK by major or exact version and vendor.",
        use_when="a script needs a specific Java without assuming where it is installed",
        doc="docs/jdk.md",
        api=("find", "discover"),
    ),
    ToolSpec(
        name="scrub",
        module="scrub",
        summary="Refuse commits carrying attribution trailers, tooling files, local paths or personal data.",
        use_when="checking commits, a push or a whole tree before it leaves the machine",
        doc="docs/scrub.md",
    ),
    ToolSpec(
        name="stats",
        module="stats",
        summary="Bootstrap the median paired change between baseline and candidate runs.",
        use_when="judging whether a timing change is real from a few matched runs",
        doc="docs/measurement.md",
        api=("paired_interval", "window_quality"),
    ),
    ToolSpec(
        name="workload-guard",
        module="workload_guard",
        summary="Exclude timing runs that overlap Java work the run did not start.",
        use_when="before or during a timing run, to check the machine is free of other JVMs",
        doc="docs/measurement.md",
        platforms=BOTH,
        api=("JavaWorkloadGuard",),
    ),
    ToolSpec(
        name="process-lifecycle",
        module="process_lifecycle",
        summary="Read an owned process's memory when it reports ready and just before it quits.",
        use_when="a harness launches a process and must record its memory without guessing PIDs",
        doc="docs/measurement.md",
        kind="library",
        platforms=BOTH,
        api=("ProcessLifecycle",),
    ),
    ToolSpec(
        name="classfile",
        module="classfile",
        summary="Read a JVM class file's constant pool and method bodies without loading it.",
        use_when="writing a check over compiled classes or JARs",
        doc="docs/classfile.md",
        kind="library",
        api=("parse", "ClassFile"),
        tests=("tests/test_mod_survey.py",),
    ),
    ToolSpec(
        name="actions",
        module="actions",
        summary="Run a project's build and test jobs locally, like a small CI, and gate pushes on them.",
        use_when="a project needs its checks run before a commit or push, within the machine's cores and memory",
        doc="docs/actions.md",
    ),
    ToolSpec(
        name="receipt",
        module="receipt",
        summary="Run a build and record what it was built from and what it produced.",
        use_when="a build's outputs must be tied to the exact sources, tools and test reports behind them",
        doc="docs/receipt.md",
    ),
    ToolSpec(
        name="jar-audit",
        module="jar_audit",
        summary="Check every class in JARs for its class file version, names and forbidden references.",
        use_when="a JAR must target one Java release and must not refer to certain packages",
        doc="docs/jar-audit.md",
    ),
    ToolSpec(
        name="java-format",
        module="java_format",
        summary="Format Java sources with a pinned google-java-format, or check that they are formatted.",
        use_when="a Java project's formatting must be checked or applied the same way on every machine and in CI",
        doc="docs/java-format.md",
    ),
    # `orthrus-tools new` adds tools above this line.
)

BY_NAME = {tool.name: tool for tool in TOOLS}
COMMANDS = tuple(tool for tool in TOOLS if tool.kind == "command")
