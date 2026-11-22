# orthrus-jdk

Finds an installed JDK by major version, exact version or vendor, the same way on Windows,
Linux and macOS. Build scripts use it instead of hard-coding install paths.

## Use

```sh
orthrus-jdk find 26                       # the java executable of a Java 26 JDK
orthrus-jdk find 26 --exact 26.0.2.1      # only that release (a +build suffix may follow)
orthrus-jdk find 25 --vendor Adoptium --home
orthrus-jdk list
```

```python
from orthrus_tools.jdk import find

java = find(26, exact="26.0.2.1").java
```

## Where it looks

In order, the first match wins:

1. `--java`, a java executable or a JDK home.
2. `JAVA_HOME_<major>_X64` or `_ARM64`, which GitHub's `setup-java` sets for each version.
3. `JAVA_HOME`, then `java` on `PATH`.
4. Per platform:
   - Windows: `Program Files` under Eclipse Adoptium, Java, Microsoft, Zulu, Amazon Corretto
     and BellSoft; per-user installs under `%LOCALAPPDATA%\Programs`; Scoop.
   - Linux: `/usr/lib/jvm`, `/usr/java`, `/opt/java`, `/opt`.
   - macOS: `/Library/Java/JavaVirtualMachines` and the per-user equivalent.
5. IntelliJ's `~/.jdks`, Gradle's `~/.gradle/jdks` and SDKMAN.

A JDK's version and vendor come from its `release` file, so nothing is run unless that file is
missing. Exit status: 0 found, 2 usage error, 3 no matching JDK on this machine. With `--json`
the `result` holds the JDK's `home`, `java`, `version`, `major` and `vendor`.
