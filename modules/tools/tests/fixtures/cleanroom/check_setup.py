"""Fail unless the Cleanroom workflow's setup holds: JAVA_HOME is the Gradle JDK and the
Gradle user properties name every installed JDK as a toolchain."""

import os
import sys
from pathlib import Path

home = Path(os.environ.get("GRADLE_USER_HOME") or Path.home() / ".gradle")
properties = (home / "gradle.properties").read_text(encoding="utf-8")
line = next(
    entry
    for entry in properties.splitlines()
    if entry.startswith("org.gradle.java.installations.fromEnv=")
)
names = [n for n in line.split("=", 1)[1].split(",") if n]
print("toolchains:", names)
majors = {n.split("_")[2] for n in names}
assert {"25", "26"} <= majors, majors
for name in names:
    assert Path(os.environ[name], "bin").is_dir(), name
release = Path(os.environ["JAVA_HOME"], "release").read_text(encoding="utf-8")
assert 'JAVA_VERSION="25' in release, release
print("ok")
sys.exit(0)
