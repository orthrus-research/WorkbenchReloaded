# Workbench Reloaded

Rich tooling for Minecraft developers, 1.12.2 first, behind one terse API. Ask Workbench for a command,
get a promise back at once, and receive one predictable result: a `wb/result/v1` envelope, the same shape for
every command and in every language.

```powershell
wb up                                                     # start the local container (127.0.0.1 only)
wb ls                                                     # every command, active or not, and why
wb run frames.compare reference=D:\work\ref candidate=D:\work\cand profile=near --out D:\work\out\cmp-01  # scrub: allow
wb run vast.offers gpu="RTX 3060" max_dph=0.10
wb run plan.run plan=D:\work\plans\turn.toml --out D:\work\out\turn-01  # scrub: allow
```

```python
import wb
result = wb.run("frames.compare", reference=ref, candidate=cand, profile="near").result()
result.outcome, result.data.failed          # "finding", ["07.png"]
```

## How it fits together

- **The core** (`core/wb_core`, standard library only) reads a config folder, discovers modules from their
  static `module.toml` manifests, decides which commands are active, keeps every job durably in SQLite, schedules
  them (one GPU job per machine, CPU jobs within a budget, measurements on a quiet machine), runs each in its own
  worker process, caches deterministic commands, and serves `/v1` on localhost with a bearer token.
- **Modules** (`modules/*`) are add-ons that declare commands, parameters, requirements and results.
- **The host runner** (`host/wb_host`, `wb host serve`) runs declared routines natively on the host (game
  sessions on the local GPU) under a GPU lease that every GPU user on the machine follows: one lock file and a folder
  of lanes. Launchers join a held lease through `WB_TURN_TOKEN` (or variable names of their own, `[runner]
  token_env`).
- **SDKs** (`sdk/`) for Python (with the `wb` CLI), Node/TypeScript, Java with a Gradle plugin, and PowerShell,
  their envelope types generated from `schema/envelope.schema.json`.

Modules follow a taxonomy of what a command does in Minecraft modding, one module per kind of operation, so a
utility lands beside its kin: what a JAR contains is `jar.*`, a world on disk is `world.*`, a build's provenance is
`build.*`. `plan` orchestrates commands of every kind.

| Module | Kind of operation | Commands |
|---|---|---|
| `toolchain` | JDKs, pinned inputs, pinned rebuilds, dependency repair, artifact rescue | `jdk`, `pins`, `rebuild`, `patch`, `repo-add`, `repo-list`, `repo-verify`, `repo-init`, `gradle-home` |
| `build` | builds and their provenance | `receipt`, `freeze`, `stage`, `archive` |
| `jar` | static analysis of compiled mods | `audit`, `survey`, `index`, `references`, `hook-targets`, `diff` |
| `mixin` | Mixin and patches: configurations and application verified without the game | `remap-proof`, `apply`, `doctor` |
| `probe` | probes and harnesses: JVM runs without the game | `jdk-matrix`, `api-consumer` |
| `source` | formatting and determinism of source | `format`, `strict-math` |
| `lab` | game instances and sessions: host routines, dedicated servers in the container, runtimes assembled from launcher metadata, and a server with several clients | `ensure`, `snapshot`, `reset`, `instances`, `remove`, `dedupe`, `store-gc`, `play`, `turn`, `script`, `prepare`, `migrate`, `prune-instances`, `gpu`, `queue`, `server`, `server-scenario`, `session`, `assemble`, `install-server`, `userdev` |
| `drive` | workload drivers: what a game session is made to do and see | `tour-lint`, `path`, `path-check`, `gallery`, `send` |
| `frames` | capture and visual verification | `compare`, `measure`, `check`, `quality` |
| `world` | world saves on disk and generated worlds: reading, comparing, goldens | `read`, `diff`, `digest-diff`, `light-diff`, `golden`, `preview` |
| `render` | shader packs, GPU test runs and GPU qualification | `validate`, `load-verdict`, `gl-gate`, `gl-run` |
| `assets` | assets and resource packs | `generate`, `blockstates`, `texture-metrics` |
| `content` | content and data: modpack scripts, recipes and placement data | `scripts`, `recipe-graph`, `placement` |
| `runs` | measurement, statistics and run evidence | `at-commit`, `repeat`, `ab`, `interleaved`, `evidence`, `compare-evidence`, `stats`, `workload-guard` |
| `profile` | profiling and diagnostics of Java game processes and the GPU they run on | `jfr`, `attribute`, `memory`, `settings`, `gpu-sample`, `gpu-report` |
| `ledger` | results and evidence | `query`, `runs`, `findings`, `show`, `costs`, `calls`, `add-run`, `add-finding`, `supersede`, `link-run`, `add-machine`, `prune`, `import`, `import-ci`, `import-results`, `parse-log` |
| `vast` | rented machines | `offers`, `market`, `charges`, `instances`, `destroy`, `keys`, `rent`, `probe`, `bundle-check`, `bundle`, `lab-inputs` |
| `ci` | project jobs, push gates, commit hygiene | `run`, `gate`, `status`, `hook`, `actions`, `scrub` |
| `release` | release and publishing: GitHub releases, Modrinth, CurseForge, Maven | `check`, `publish`, `status` |
| `plan` | declarative plans of commands | `check`, `run` |
| `tools` | the engine of the former standalone tools, behind toolchain, build, jar, mixin, probe, source, runs and ci | — |
| `selftest` | exercises the core end to end | |

The former standalone tools' commands kept their earlier names as aliases (`tools.jar-audit` runs `jar.audit`).

## CI

`ci.run project=NAME` runs a project's jobs (its `actions.toml`) from a clean clone, with every repository the jobs
file names checked out at the commit the project pins. The checkouts are only read. Jobs get the image's JDKs
(`JAVA_HOME`, `JAVA_HOME_<major>_X64`), a Gradle home that survives the container and serves the rescue repository
(below), and the Python environments the project names, built once per set of requirements. The run records stay in Workbench, where `ci.gate` reads them:
`ci.hook project=NAME` installs a pre-push hook that refuses a commit whose tree has not passed. `commit=worktree`
runs the checkout's working tree as it would be committed. Projects are declared in `[modules.ci.projects]`.

### Writing jobs for Workbench

A project's CI fits Workbench, not the other way around. A jobs file that follows these rules runs the same in
every clean clone:

- **One file, `actions.toml`, at the project root**, in the shared actions format.
- **Java comes from the environment:** `JAVA_HOME` (the standard JDK) and `JAVA_HOME_<major>_X64` for every other.
  Gradle already knows every JDK through the Gradle home. Never pass `org.gradle.java.installations.paths`, and never
  read JDK paths from a settings file of the checkout.
- **Python is `{python}`:** the project's environment, built from the requirements files its CI config lists
  (`python = [...]`). Jobs name no interpreter paths and no `.local` environments.
- **Third-party files outside the build tool are pinned by SHA-256** and fetched (`toolchain.pins`), never found by
  scanning a cache. Every job gets `WB_PIN_CACHE`, a folder of files named by their SHA-256: a tool that builds or
  downloads a pinned file looks there first and stores what it made, so each is made once per machine.
- **Outputs go under `{run_dir}`**, never into the tree. A file a job writes into the tree changes the tree the run
  vouches for.
- **No init scripts or Maven folders of the project's own** for what public repositories stopped serving: it goes into
  the rescue repository, which every build gets.
- **Other checkouts are `[repositories.<name>]`, with a `pin`** when the project fixes their commit. A checkout jobs
  only build pinned commits from (verified by SHA-256) is `tested = false`: it is cloned, but its tree is not part of
  what a pass stands for, so its new commits leave the project's passes standing.
- **`ci` is what the gate requires.** Work that needs a GPU, a window or a particular OS is not CI: it is a plan of lab
  routines. Pure checks list their `inputs`, so they are cached; `[quick]` rules keep local loops short.

## Artifact rescue

A build plugin or library version its public repositories no longer serve goes into toolchain's rescue repository, a
local Maven repository with a receipt for every file. `toolchain.repo-add coordinates=group:artifact:version` with
`file=` or `url=` and the expected `sha256=` refuses other bytes, never replaces bytes it holds, and writes a minimal
POM when the original is lost (`dependencies=`), a marker for each Gradle plugin a JAR implements (`plugins=`), the
metadata, and a receipt with its `reason=`. Coordinates that cannot come back under their own name (a renamed release,
a retained or patched build) are mapped in `[modules.toolchain] substitutions`. Every build `ci.run` runs reads both
through its Gradle home's `init.d`, and `toolchain.repo-init gradle_home=...` writes the same script for builds on the
host (the repository then lives under a root, such as the out root). Gradle asks the repository only for the exact
versions it holds, so it never stands in for what a public repository still serves. `repo-list` shows what it holds
and why; `repo-verify` hashes it all again.

## Game sessions without a launcher

`lab.assemble` turns a launcher instance (a Prism or MultiMC folder, or the .zip it exports) into a runtime: every
library by the target's rules, the natives, the client JAR and the assets, each fetched against the SHA-1 its
metadata names into a shared cache, and `runtime.json` naming the main class, arguments and class path.
`lab.install-server` installs a dedicated server with its loader's own installer, pinned by SHA-256. `lab.session`
then runs one server and several clients started straight from the runtime, each with an offline name, waits for
them to join, can kill one on cue, and keeps every console, log and capture; `desktop = "hidden"` keeps the clients
on a desktop of their own that never takes the input (Windows). `lab.userdev` runs a project's development client
from its own build in a fresh run folder, and an exit code alone never passes it: the receipt the game writes does.

The drive agent (`mods/drive-agent`) is a small client mod a session adds: it captures views once they settle and
answers requests on the game's own thread (status, screenshot, chat, keys, clicks), which `drive.send` writes.

## The Gradle plugin

`sdk/gradle` is a Gradle plugin, `cc.orthrus.wb`, with two task types. `WbTask` runs a Workbench command and fails
the build on the outcomes in `failOn`, keeping the settled envelope. `WbRun` runs an entry point on a class path the
build prepares, a mod build's remapped configurations included, with no game client or server: it refuses to start
when a `pin(file, sha256)` no longer matches or a class in `singleProvider` comes from none or several class path
entries, and keeps a receipt (`build/wb/<task>.run.json`) of the main class, arguments, JVM, every class path entry
with its SHA-256, and the exit code. `sdk/gradle/sample` uses both.

A build applies it from an included build, `pluginManagement { includeBuild('<checkout>/sdk/gradle') }`, or by id
and version from the rescue repository. Its JAR is the same bytes from the same sources and JDK on every machine, so
it goes in like any other file:

```
gradle -p sdk/gradle jar
wb run toolchain.repo-add coordinates=cc.orthrus:wb-gradle-plugin:0.1.0 plugins=cc.orthrus.wb \
  file=sdk/gradle/build/libs/wb-gradle-plugin-0.1.0.jar sha256=<its SHA-256> reason="the Workbench Gradle plugin, built from this checkout"
```

Then `plugins { id 'cc.orthrus.wb' version '0.1.0' }` works in every build that reads the repository: those
`ci.run` runs, and builds on the host once `toolchain.repo-init gradle_home=...` has written its init script.

## Config layers

A machine's config folder can lie over shared folders: `[core.config] layers = [...]` in its `workbench.toml`. A
team keeps its common config (launcher profiles, CI projects, card lists, plans) in one versioned folder, and each
machine adds only its paths and secrets. The machine folder merges last and wins, and `wb config show --origin` names
the layer behind every value.

## Game instances

An instance is a recipe over shared layers, not a fresh full copy. How a launcher prepares, plays and lays out
its instances is an instance profile in config (`[modules.lab.profile]`; Workbench ships none). `lab.ensure` hashes
everything the launcher's `prepare` reads into a recipe, prepares one template per recipe, and builds each instance from it in under a
second: libraries, mod jars and natives are read-only hard links to one stored copy, and only what the game
writes is the instance's own. `lab.snapshot` keeps a generated world and `lab.reset` restores it, so every run
can start from the same settled state. `lab.dedupe` turns existing full copies into linked ones in place.

## Money

A command that spends money (`vast.rent`, `vast.probe`) stops in `awaiting_approval` with one exact quote (card,
driver, host, price, cap, the most it can cost) and a ready-made prompt. It runs only after an explicit yes:
`wb approve JOB --quote Q`, given by you, or relayed by another program after your yes. A quote expires after five
minutes and is replaced if the price rises; no setting, budget or schedule can approve. A rental is destroyed in
`finally`, at its cap, when billed above its quote, by its own dead man's switch, and by the next start's clean-up.

## Exit codes

`0` ok, `1` finding, `2` usage, `3` environment, `4` cancelled or interrupted, `5` still pending, `6` awaiting
approval. A settled envelope's `code` is the same number.

## Developing

```powershell
python tests/run_all.py            # every suite; --quick skips the SDK and host suites
python image/pins.py check         # image locks match image/pins.toml
python schema/generate.py --check  # SDK envelope types match the schema
wb build                           # docker build -f image/Dockerfile -t wb-core:dev .
```

Run the CLI from a checkout with `bin\wb.cmd` (or `bin/wb`). The config folder is `~/.config/wb`; start from
`config.example`.

## License

[Apache License 2.0](LICENSE), except `modules/tools`, which keeps its own license (NRR 1.0). See
[NOTICE.md](NOTICE.md).
