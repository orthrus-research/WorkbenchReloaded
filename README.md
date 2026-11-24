<p align="center"><img src=".github/assets/banner.svg" alt="Workbench Reloaded by Orthrus Research" width="100%"></p>

<p align="center">
<a href="#status"><img alt="Status: in development" src="https://img.shields.io/badge/status-in%20development-000000?style=flat-square&labelColor=000000"></a>
<a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-000000?style=flat-square&labelColor=000000"></a>
<a href="https://discord.gg/qCVqhXtSzR"><img alt="Discord" src="https://img.shields.io/badge/discord-join-000000?style=flat-square&labelColor=000000"></a>
</p>

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

## Status

In development: version 0.1.0 is unreleased, and there are no releases yet;
[CHANGELOG.md](CHANGELOG.md) lists what it holds. Workbench Reloaded succeeds
[Workbench](https://github.com/orthrus-research/Workbench).

## How it fits together

- **The core** (`core/wb_core`, standard library only) reads a config folder, discovers modules from their
  static `module.toml` manifests, decides which commands are active, keeps every job durably in SQLite, schedules
  them (one GPU job per machine, CPU jobs within a budget, measurements on a quiet machine), runs each in its own
  worker process, caches deterministic commands, and serves `/v1` on localhost with a bearer token.
- **Modules** (`modules/*`) are add-ons that declare commands, parameters, requirements and results.
- **The host runner** (`host/wb_host`, `wb host serve`) runs declared routines natively on the host (game
  sessions on the local GPU) under a GPU lease that every GPU user on the machine follows: one lock file and a folder
  of lanes. Launchers join a held lease through `WB_TURN_TOKEN` (or variable names of their own, `[runner]
  token_env`). `wb gpu` shows the holder, the waiters in the order they are served and stuck lanes; `wb gpu run
  [--note T] [--limit D] [--detach] [--quiet] -- <command>` gives any command a turn in a lane of its own (one that
  was waiting keeps its place when started again, and a detached one outlives its shell); `wb gpu leave <lane>`
  withdraws one.
- **SDKs** (`sdk/`) for Python (with the `wb` CLI), Node/TypeScript, Java and PowerShell,
  their envelope types generated from `schema/envelope.schema.json`.

Modules follow a taxonomy of what a command does in Minecraft modding, one module per kind of operation, so a
utility lands beside its kin: what a JAR contains is `jar.*`, a world on disk is `world.*`, a build's provenance is
`build.*`. `plan` orchestrates commands of every kind.

<!-- begin generated modules: python tools/command_tables.py writes this table from the module manifests -->
| Module | What it does | Commands |
|---|---|---|
| `assets` | Assets and resource packs: content JSON generated from one manifest, reference blockstates copied from the client JAR into a pack complete but for its textures, and texture readability metrics. | `generate`, `blockstates`, `texture-metrics` |
| `build` | Builds with receipts and reproducible archives. | `receipt`, `archive` |
| `ci` | Run a project's checks, gate pushes on them, and keep commits clean. | `run`, `host-run`, `gate`, `status`, `hook`, `actions`, `scrub`, `matrix`, `store-verify`, `store-export`, `store-import`, `land`, `reach`, `store-platform` |
| `content` | Content and data: modpack scripts, recipes and placement data, read without running the game. | `scripts`, `recipe-graph`, `placement` |
| `drive` | Workload drivers: what a game session is made to do and see. Tours checked before a session plays them, camera paths made from keyframes and checked against the frames captured along them, and block galleries laid out for review. | `tour-lint`, `path`, `path-check`, `gallery`, `send` |
| `frames` | Score captured frames: pass or fail against references, capture noise, wrong-view checks, and PSNR/SSIM quality (from Quadrature's frames.py). | `compare`, `measure`, `check`, `quality` |
| `jar` | Static analysis of compiled mods: class files, references and what each JAR patches. | `audit`, `survey`, `index`, `references`, `hook-targets`, `diff` |
| `lab` | Game sessions: native client sessions on the host through wb-host (instances, the GPU lease, the lab home), and headless dedicated-server sessions in the container. | `gpu`, `resolve`, `turn`, `install-server`, `pack`, `assemble`, `build`, `run`, `send`, `keep`, `freeze`, `thaw`, `server` |
| `ledger` | Formal results, costs and market prices in one queryable database. | `query`, `runs`, `findings`, `show`, `costs`, `calls`, `add-run`, `add-finding`, `supersede`, `link-run`, `add-machine`, `prune`, `import`, `import-ci`, `import-results`, `import-json`, `parse-log` |
| `mixin` | Mixin and patches: verifying Mixin and patch application without the game. | `remap-proof`, `apply`, `doctor` |
| `plan` | Declarative turns and campaigns: pinned inputs, a declared protocol, steps that are Workbench commands; checked first, then run on the host. | `check`, `run` |
| `probe` | Probes and harnesses: JVM runs without the game. Small Java programs compiled against an artifact and run in throwaway JVMs on known JDKs. | `jdk-matrix`, `api-consumer` |
| `profile` | Profiling and diagnostics of Java game processes and the GPU they run on: JFR recordings over time windows, cost charged to the code that incurred it, native memory by owner, the JFR settings that feed them, and whole-device GPU telemetry sampled beside a run and read by window. | `jfr`, `attribute`, `memory`, `settings`, `gpu-sample`, `gpu-report` |
| `release` | Publish built mod artifacts to GitHub releases, Modrinth, CurseForge, Maven repositories, Maven Central and our own Maven mirror: checked against the publication allow-list, quoted as a plan, sent only after an explicit yes. | `check`, `publish`, `status` |
| `render` | Rendering: shader pack checks (the pack contract, every stage compiled and linked per profile, driver hazards), whether a pack really loaded, and GL qualification of a card. | `validate`, `load-verdict`, `gl-gate`, `gl-run` |
| `runs` | Run custody (from orun): steps in a clean tree at an exact commit, repeated runs, interleaved A/B with paired statistics, and evidence records that refuse a comparison of runs differing in more than their treatment. | `at-commit`, `repeat`, `ab`, `evidence`, `compare-evidence`, `stats`, `workload-guard`, `interleaved` |
| `selftest` | Exercises the core: echo, sleep, fail, a zero-cost approval, and a call into another command. | `echo`, `sleep`, `fail`, `spend`, `copy`, `sizes`, `checkout`, `count`, `chain`, `host-echo`, `quiet-echo`, `nest`, `inline-echo`, `orphan`, `tool` |
| `source` | Formatting, lint and determinism of source code. | `format`, `strict-math` |
| `toolchain` | JDKs, pinned inputs, dependency repair and the rescue repository. | `jdk`, `pins`, `patch`, `repo-add`, `repo-list`, `repo-verify`, `repo-init`, `gradle-home` |
| `tools` | The engine of the former standalone tools. Its tools are commands of the taxonomy's modules: toolchain, build, jar, source, runs and ci. | none (aliases only) |
| `vast` | Rent GPU machines on Vast.ai, run bundles on them, and record market prices and charges. | `offers`, `market`, `charges`, `instances`, `destroy`, `keys`, `rent`, `probe`, `bundle-check`, `bundle-launch` |
| `world` | Worlds on disk and as generated: what a stopped save holds, how two worlds differ (save by save or by digest), and a generator's windows against golden digests, sampled without the game. | `read`, `diff`, `digest-diff`, `light-diff`, `golden`, `preview` |
<!-- end generated modules -->

Every command, where it runs and what it does: [docs/commands.md](docs/commands.md), generated from the same manifests.

The former standalone tools' commands kept their earlier names as aliases (`tools.jar-audit` runs `jar.audit`).

## CI

`ci.run project=NAME` runs a project's jobs (its `actions.toml`) from a clean clone, with every repository the jobs
file names checked out at the commit the project pins. The checkouts are only read. Jobs get the image's JDKs
(`JAVA_HOME`, `JAVA_HOME_<major>_X64`), a Gradle home that survives the container and serves the rescue repository
(below), and the Python environments the project names, built once per set of requirements. The run records stay in Workbench, where `ci.gate` reads them:
`ci.hook project=NAME` installs a pre-push hook that refuses a commit whose tree has not passed. `commit=worktree`
runs the checkout's working tree as it would be committed. Projects are declared in `[modules.ci.projects]`.
A run's summary keeps each check's and step's time and peak memory (`peakRssBytes`: the most resident memory the
step's process tree held at once, sampled every half second while it ran; a Gradle daemon counts, the step's own or
one it reuses), and `wb show` prints them under the job.

Host checks (`on = "host"`, `gpu = true` in project.toml) run only when named and only for a project in the machine's
`[lab] trusted`: `wb check gl` sends them to `ci.host-run`, which runs them natively from a clean clone on the host,
the GPU ones under a turn of the GPU lease that their steps join, and records their passes beside `ci.run`'s. The
container checks among the names still go to `ci.run`; `wb check` prints which ran where
(see [modules/tools/docs/actions.md](modules/tools/docs/actions.md#host-checks)).

A commit from before its project's conversion (it carries `actions.toml` and no `project.toml`, while the project's
set-named branch carries `project.toml`) is answered first: `wb check` says that the commit predates the set and
names the setup command its own tree carries (its README's build section, else its `tools/setup.py` or a root
script's `setup` action), with the pin cache folder it builds with outside Workbench. Its checks still run from its
`actions.toml` for the grace period; a commit with no checks file at all is answered and not run.

`wb status --doctor` reads git and config on the host, with or without the daemon, and names with project, branch,
file and line what still ties a converted project to the old routes, at its landed commit and at its set-named
branch's published tip: a `WB_PIN_CACHE` read, a `sourceCommit`, a pin file (`cc.lock.json`, a `pins/` folder), a
refresh or sync task in its build or checks files, and a sibling commit (a commit the Set lists for another project,
or one in another registered repository); paths in set.json `doctor.exempt`, every project's or that project's, are
skipped. It also names any schedule that refreshes pins, and confirms that the old lock path (`[runner] lease_file`)
is the lease file and that each checkout's old in-tree route to it, and to the lanes folder, resolves there. It exits
1 when it finds anything. A path that records a commit on purpose, such as an inspection's inputs, is exempted by a
landing: `wb land --doctor-exempt <project>:<glob> --reason <why>` (repeatable; the project has a line or is
registered, the glob is relative to its top), and `--doctor-unexempt <project>:<glob>` removes one.

`ci.matrix` is a portability lane: on a host (Windows, macOS or Linux, no GPU needed, so it runs beside a game) it
clones a checkout's commit clean and runs one command per Python, `{python}` standing for each interpreter:

```sh
wb run ci.matrix checkout=. 'command=["{python}","-m","unittest","discover"]' 'pythons=["3.12","3.14"]'
```

A version is looked for where hosts keep them: the `py` launcher and every interpreter it lists on Windows (uv's and
conda's too), `python3.12` on PATH elsewhere, and uv's wherever uv is installed, each asked its version before it is
used. An interpreter path is used as given. `requirements=[...]` gives each version an environment of its own;
`folder=` runs the command in a folder of the checkout and `env=` adds variables (`'env={"PYTHONPATH":"src"}'`).
Each version's exit, time and output are kept, with `matrix.json` over all of them; a failure, or a Python the host
lacks, is a finding.

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
  downloads a pinned file looks there first and stores what it made, so each is made once per machine. The pin
  cache, the run records and the Store are folders under `[paths] out` (`cache/pins`, `ci/records`, `store`), each
  named once; they are given to Workbench's user each time the container starts, so a folder made on the host or by
  root still takes writes.
- **Outputs go under `{run_dir}`**, never into the tree. A file a job writes into the tree changes the tree the run
  vouches for. What a build tool writes inside the tree on its own (`build/libs`) is kept by listing it in the
  job's `artifacts`: the records then hold each file by SHA-256 beside the run that passed it, where the host can
  use it.
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
`lab.install-server` installs a dedicated server with its loader's own installer, pinned by SHA-256. `lab.pack` gets
a release-exact modpack from its pack file, every file checked by the hash the pack names: a Modrinth pack
directly, a CurseForge pack through CurseForge's API with the machine's key. The game itself is built and played from
a launch file (below): `wb play`, `lab.build` and `lab.run`.

The drive agent (`mods/drive-agent`) is a small client mod a session adds: it captures views once they settle and
answers requests on the game's own thread (status, screenshot, chat, keys, clicks), which `drive.send` writes.

## Config layers

A machine's config folder can lie over shared folders: `[core.config] layers = [...]` in its `workbench.toml`. A
team keeps its common config (launcher profiles, CI projects, card lists, plans) in one versioned folder, and each
machine adds only its paths and secrets. The machine folder merges last and wins, and `wb config show --origin` names
the layer behind every value.

A machine's paths are named once, as the host names them, in `[paths]`: `out` (the read-write root, mounted at
`/mnt/out`, holding the Store, the run records and the pin cache), `releases`, `backup`, and `[paths.roots.<name>]`
for the other folders the container may read or write (mounted at `/mnt/<name>`). The old spellings
(`[paths.roots.out]`, a root's `container`, `[modules.ci] store`, `records` and `pin_cache`, `[modules.toolchain]
pin_cache`) are still read for one release, each with a warning in `wb status`, `wb config show --local` and the
daemon's log.

## Game instances

An instance is built from a launch file, not by a project's own launcher: Workbench ships no instance profile and
runs no launcher script. The profile-era commands (`lab.inputs`, `lab.checkouts`, `lab.ensure`, `lab.script`,
`lab.userdev`, `lab.session`, `lab.server-scenario`, `lab.queue`, `lab.snapshot`, `lab.reset`, `lab.remove`,
`lab.instances`, `lab.prune-instances`, `lab.store-gc`, `lab.dedupe`, `lab.migrate`, `lab.migrate-plan`) are
retired and answer with what replaces them, as do `lab.prepare` and `lab.play`, the template rental route's game
steps: a rental is a bundle made from a launch file (`vast.bundle-launch`), and here a launch is played by `wb play`.

A project that is launched defines its launches in its own `project.toml` (`[launch.<name>]`: members as
`project:artifact@side` or `third@side`, the runtime, typed params, JVM arguments, where a file's name matters the
name a member is placed under, the base it starts from, its configure script under `launch/`, the tour rules and its
drivers), and a member may add its own switches (`[launch.member]`). A base is another project's table without members
(`base = "cubature:standalone"` names Cubature's `[launch.standalone]`: the installer, the command lines, the player,
the JVM profile, the view keys and an overlay folder under `launch/`). `lab.resolve folder=<worktree>` resolves one
once into `launch.json`: every member by SHA-256 from the worktree's set revision (a line, a hold or a third entry),
the project itself from the Store by the tree CI built, with the mod ids each declares; the base's table at the
commit the set names for its project with its overlay folder's tree id, and the product's configure script and
`launch/` tree at its own commit. A base project whose commit has no such table, a member the set or the Store
lacks, or two builds of one mod id, is refused before anything is written. Once the set's platform names the runtime's own files
(`platform.cleanroom.{installer,universal,prism}`), the launch file carries them too, in `runtime.files`.

`lab.build launch=<launch.json> name=<name>` builds an instance from that file and nothing else: the server by the
runtime's installer on the launch's exact JDK from the Store, the client from the runtime's launcher instance (as
`lab.assemble` does), each runtime file by SHA-256 from the Store, a candidate folder or an allowed host; every member's
Store file hashed again and placed by side; the base's files, JVM profile and overlay folder (exported from the
base project's repository by the tree id the launch file names), then the product's configure script for its own
changes. Its `instance.json` names every file by SHA-256 relative to
`{instance}`, `{cache}` and `{jdk:N}`, with the launch file's SHA-256 and the server's and client's command lines. One
template is built per launch file, base and parameters, and each further instance is hard-linked from it in under a
second. `modules/lab/tools/compare_instances.py` compares such an instance with one the old route prepared, file by
file and command line by command line, and with `--host <instance> --container <instance or tar>` two instances of
one launch file built on two systems, naming each difference it can explain (natives, line endings, the files that
name their system). A configure script runs only for a project the machine's `[lab] trusted` names,
and `eula.txt` is written only where `[lab] eula = true` (or the request) accepts the Minecraft EULA. The server's
port is not part of a template: `{port}` stays in the client's command line for the session to fill.

`lab.run instance=<name> kind=open|tour|measure|driver` plays a session of such an instance from its receipt alone,
under the GPU lease. It first hashes every cache, JDK and member file against `instance.json`: a JDK of another build
is refused, the same build at another path (`jdk=`) is accepted. A tour is driven by the launch's tour rules
(`[launch.<name>.tour]`: the server's set-up commands, the client's capture switches and the line that says a view
was captured), view by view, and filed as `tour/NN-<label>.png`; `measure` is such a tour that is evidence: quiet, on
a fresh copy of the template, and only for the launch file it names. `driver` runs a trusted project's declared
driver (`[launch.<name>.drivers]`) with the turn token, the lease and the instance.json path in `WB_TURN_TOKEN`,
`WB_LEASE_FILE`, `WB_INSTANCE` and the names `[lab] env` adds; the runtime cache the receipt's `{cache}` paths are
under goes only in the name `[lab] env` gives `cache` (`WB_RUNTIME_CACHE = 'cache'` for Cubature's lab tools). Its
`{root}` is the project's tree: `wb play --driver <name>` runs it from the worktree the launch was resolved from. What a session chooses (`port`, `jvm`, `window`,
`view`, `edits`, `debug`) goes into its `session.json`, beside the member hashes, the JDK build and the GPU and its
driver, and never into the template: edited files are put back when it ends. `debug=client|server|both` opens JDWP
on loopback, writes `attach/launch.json` (VS Code) and `attach/jdb.txt`, and marks the session as not evidence. A
stop waits at least 180 s for the server's own exit before the tree is killed; a failure names the decisive line
(the launch's `failures` first), the crash report and the next command. `lab.keep session=<id> reason=<text>` keeps
a session; what it names is a retention root (`kept.json`).

The old route's instances (a checkout's `.local/cubic-standalone/<name>`, with no `instance.json`) are kept as
evidence by `lab.freeze folder=<folder> apply=true`: a manifest of every file by size, time and SHA-256, the world
in the world store by its world key, `tours/` and `runs/` copied into a kept session, then every file read-only.
Without `apply` it only inventories; a folder reached through a link is refused. `lab.thaw folder=<folder>` is the
undo: it clears exactly the marks the manifest says the freeze made.

`wb play [project][@set|@head][:launch]` does all of it from the folder you stand in: it resolves the launch at the
folder's `.set` (or the head where there is none) and says which (`set <rev> (this folder, N behind)` or `set <rev>
(head)`), submits the producing check of any member the Store lacks first (`--no-rebuild` refuses instead, naming
it), builds or reuses the instance, and plays the session on this machine under the GPU lease in a lane of its own.
It prints the job id, launch file, instance and session folder at once, streams both consoles, and on a failure
prints the decisive line, the crash report and the next command. `--from <launch.json>` replays a launch file's
exact bytes; `--tour`, `--measure`, `--driver`, `--debug`, `--fresh-world`, `--param`, `--jvm`, `--view`,
`--window` and `--edit` pass through to the build and the session.

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

The image holds its JDKs; other toolchains are optional. `image/pins.toml` pins them (`[tool.<name>]`: a download
and its SHA-256, today Node and Zig), and a machine names the ones its projects need in `[core.image] tools =
["node"]`: `wb build` builds just those in, checked, with their programs on `PATH`. A toolchain another project needs
is one more `[tool]` table and `python image/pins.py lock`.

## Community

Questions and discussion: [Discord](https://discord.gg/qCVqhXtSzR). Bugs and requests: this repository's issues. Security
reports: [SECURITY.md](https://github.com/orthrus-research/.github/blob/main/SECURITY.md). Pull requests are welcome; see
[CONTRIBUTING.md](https://github.com/orthrus-research/.github/blob/main/CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE), except `modules/tools`, which keeps its own license (NRR 1.0). See
[NOTICE.md](NOTICE.md).

---

<sub>Workbench Reloaded is an [Orthrus Research](https://orthrus.cc) project ·
[All projects](https://orthrus.cc/projects) · [Texts](https://orthrus.cc/texts) ·
[Discord](https://discord.gg/qCVqhXtSzR)</sub>
