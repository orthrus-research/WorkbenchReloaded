# receipt

Run a build and record what it was built from and what it produced. Use it when a build's
outputs must be tied to the exact sources, tools and test reports behind them.

```sh
orthrus-tools receipt run --run-id build-001 \
    --artifact "build/libs/*.jar" --junit "build/test-results/**/TEST-*.xml" \
    --identity "$JAVA_HOME/release" -- ./gradlew build --rerun-tasks --no-build-cache
orthrus-tools receipt verify .local/receipts/build-001/receipt.json [--artifacts-only]
```

Options go before `--`; everything after it is the build command, run from the project root
with its output in `build.log`. Force the build to do the work (`--rerun-tasks`, no build
cache): an artifact or report the command did not write during this run fails the receipt.

`run` writes `<out>/<run-id>/receipt.json` and `build.log`. A run id is never reused, so a
receipt always describes one build. It passes (exit 0) only when:

- the command exited 0;
- every `--artifact` glob matched, and each file was written during the build;
- every `--junit` glob matched fresh reports holding at least one test, no failures or errors,
  and no skipped tests unless `--allow-skipped`;
- every `--identity` file exists;
- no source changed while the build ran, and no output changed while it was recorded.

Settings in `orthrus.toml` stand in for the options:

```toml
[receipt]
out = "evidence/receipts"   # default .local/receipts
artifacts = ["build/libs/*.jar"]
junit = ["build/test-results/**/TEST-*.xml"]
identity = ["gradle/wrapper/gradle-wrapper.properties"]
exclude = ["docs"]          # folders left out of the source inventory
allow_skipped = false
```

## The receipt

`orthrus.receipt.v1` fields:

| Field | Holds |
| --- | --- |
| `runId`, `status`, `errors` | `passed` or `failed`, and why |
| `startedAtUtc`, `finishedAtUtc`, `exitCode`, `command` | the build as run |
| `platform`, `git` | OS and machine type; HEAD commit and whether the tree was dirty |
| `source` | `files` (path to SHA-256 of every file Git would track), `digest`, `excluded` |
| `identity` | path, bytes and SHA-256 of each identity file |
| `artifacts` | path, bytes, SHA-256, modification time and `fresh` of each artifact |
| `tests` | `totals` and each report's counts, hash and `fresh` |
| `sourceUnchanged`, `log` | whether the sources held still; the log's hash |

Paths are relative to the project root. The command is recorded as given, so pass relative
paths if receipts are shared. Identity files outside the root are recorded by their full path.

`verify` re-hashes the artifacts and the sources and exits 1 when the receipt failed, an
artifact is missing or changed, or (without `--artifacts-only`) the sources differ.
