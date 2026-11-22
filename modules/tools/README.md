# tools: the former standalone tools

The former standalone tools (0.6.0), vendored for Workbench (D18) and trimmed to the tools. Every tool keeps its own
contract: one JSON result with `--json`, and exit codes 0 ok, 1 finding, 2 usage, 3 environment
([the contract](docs/contract.md)). Workbench runs each one as a command, and one adapter
(`wb_tools/adapter.py`) maps the result to the envelope: `status` becomes the outcome, `result` the data.

```powershell
wb run tools.pins argv=verify cwd=D:\work\Riven  # scrub: allow
wb run tools.stats argv=paired.json
wb help tools.frames
```

Absolute host paths in `argv` are translated to the container's mounts.

| Tool | Job | Docs |
| --- | --- | --- |
| `frames` | Compare captured frames against references under a tolerance profile | [frames](docs/frames.md) |
| `mod-survey` | List which GL calls and rendering-class patches each Minecraft mod JAR refers to | [mod survey](docs/mod-survey.md) |
| `strict-math` | Keep deterministic Java code on `StrictMath` | [strict math](docs/strict-math.md) |
| `pins` | Download third-party inputs pinned by SHA-256 | [pins](docs/pins.md) |
| `jdk` | Find an installed JDK by major or exact version | [jdk](docs/jdk.md) |
| `scrub` | The commit scrub: refuse attribution trailers, tooling files, local paths and personal data | |
| `stats` | Bootstrap paired run-level timing changes | [measurement](docs/measurement.md) |
| `workload-guard` | Exclude timing runs that overlap other Java workloads | [measurement](docs/measurement.md) |
| `actions` | Run a project's build and test jobs locally, like a small CI | [actions](docs/actions.md) |
| `receipt` | Run a build and record what it was built from and what it produced | [receipt](docs/receipt.md) |
| `jar-audit` | Check every class in JARs for its class file version, names and forbidden references | [jar-audit](docs/jar-audit.md) |
| `java-format` | Format Java sources with a pinned google-java-format, or check them | [java-format](docs/java-format.md) |

Its own tests run from this folder: `python -m unittest discover -s tests -t .` with `src` on `PYTHONPATH`.
