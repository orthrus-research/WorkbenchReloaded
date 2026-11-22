"""A Gradle home that knows the machine's JDKs, as the one CI builds with does: a local build then passes no JDK paths
of its own (never `org.gradle.java.installations.paths` on the command line). Workbench writes one marked block of
gradle.properties and leaves every other line alone, so the block can be updated or removed later."""

from __future__ import annotations

import os
from pathlib import Path

BEGIN = "# >>> Workbench: this machine's JDKs (toolchain.gradle-home)"
END = "# <<< Workbench"


def block(homes: list[str], auto_detect: bool, auto_download: bool) -> str:
    # Forward slashes: a backslash escapes in a properties file, and Gradle reads either on Windows.
    paths = ",".join(dict.fromkeys(Path(h).as_posix() for h in homes))
    return "\n".join([BEGIN, f"org.gradle.java.installations.paths={paths}",
                      f"org.gradle.java.installations.auto-detect={str(auto_detect).lower()}",
                      f"org.gradle.java.installations.auto-download={str(auto_download).lower()}", END]) + "\n"


def merged(existing: str, new_block: str) -> str:
    """The properties text with Workbench's block replaced, or appended; every other line as it was."""
    start = existing.find(BEGIN)
    end = existing.find(END, start) if start != -1 else -1
    if start != -1 and end != -1:
        tail = existing.find("\n", end)
        return existing[:start] + new_block + (existing[tail + 1:] if tail != -1 else "")
    separator = "" if not existing or existing.endswith("\n") else "\n"
    return existing + separator + new_block


def home_folder(given: str | None) -> Path:
    return Path(given or os.environ.get("GRADLE_USER_HOME") or Path.home() / ".gradle")


def jdk_homes(jdks: dict) -> list[str]:
    """The JDK homes by major, in major order (a machine's named JDKs, as its host runner knows them)."""
    majors = sorted((k for k in jdks if str(k).isdigit()), key=int)
    return [str(jdks[k]) for k in majors]
