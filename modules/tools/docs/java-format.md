# java-format

Format Java sources with a pinned google-java-format, or check that they are formatted. Use it
when a Java project's formatting must be checked or applied the same way on every machine and
in CI.

```sh
orthrus-tools java-format check [PATH...]     # exit 1 lists the files that would change
orthrus-tools java-format apply [PATH...]     # rewrite them in place
orthrus-tools java-format fetch               # download and verify the formatter only
```

Each takes `--root DIR`, `--java PATH` (a java executable or JDK home) and `--json`.

The formatter is google-java-format's all-dependencies JAR, downloaded from Maven Central into
`.local/tools/` on first use and checked against its pinned SHA-256 on every run. A cached JAR
that no longer matches is refused, not replaced. The default pin is 1.35.0. To change it, pin
both fields:

```toml
[java-format]
version = "1.35.0"
sha256 = "bfb7f9ead6cd328389bc2da53860443bc0e805dfd08cc889bfdf43b26cb2a6e8"
paths = ["src/main/java/**/*.java", "tools/checks/*.java"]   # default src/**/*.java
exclude = ["src/generated/**"]
args = ["--aosp", "--skip-reflowing-long-strings", "--skip-javadoc-formatting"]
java = 25          # run it on this JDK major; default: the newest JDK 21 or later
```

Paths given on the command line (files, or folders searched for `.java`) replace `paths`;
`exclude` applies to both. Long file lists are split across several formatter runs.

`check` exits 1 when a file would change or does not parse; `result.changed` lists the files
and `result.errors` the formatter's messages. `apply` reports the files it changed, and exits 1
only for errors. `result.files` is the number of files given to the formatter. No JDK is exit
3, and so is a download that fails.

For a project that already formats with Spotless, run `spotlessCheck` instead, for example
through the [Cleanroom workflow](workflows.md)'s `run` input.
