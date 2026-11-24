# Commands

Every module's commands as its manifest (`modules/<module>/wb_<module>/module.toml`) declares them: the full name
to give `wb run`, where it runs (`container`: the daemon's container; `host`: the host runner, natively on this
machine) and its summary. `wb help <command>` gives its parameters, requirements and result. Workbench's own verbs
(`wb gc`, `wb land`, `wb work`, ...) are listed by `wb --help`.

<!-- begin generated commands: python tools/command_tables.py writes this table from the module manifests -->
### `assets`

Assets and resource packs: content JSON generated from one manifest, reference blockstates copied from the client JAR into a pack complete but for its textures, and texture readability metrics. (modules/assets/wb_assets/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `assets.generate` | container | Write blockstates, block and item models and lang entries from one manifest of blocks, items and families; check=true writes nothing and fails on stale, missing or orphaned files. |
| `assets.blockstates` | container | Copy reference blocks' blockstates and models from the 1.12.2 client JAR into a pack complete but for its textures, naming every texture slot; sort a mod's blocks by blockstate shape. |
| `assets.texture-metrics` | container | Measure how 16 x 16 textures read when tiled (lightness spread, edges, balance, banding, alternates' repetition) and judge them against per-kind limits. |

### `build`

Builds with receipts and reproducible archives. (modules/build/wb_build/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `build.receipt` | container | Run a build and record what it was built from and what it produced. Also as `tools.receipt`. |
| `build.archive` | container | Pack a file tree into a byte-reproducible ZIP (sorted entries, one timestamp and mode) with a manifest and a .sha256; variants freeze #define values in named files. |

### `ci`

Run a project's jobs, gate pushes on them, and keep commits clean. (modules/ci/wb_ci/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `ci.run` | container | Run a project's jobs from a clean clone (pinned repositories at their pins), keeping the records in Workbench; their Gradle builds get toolchain's rescue repository. |
| `ci.host-run` | host | Run a project's host checks (on = "host", gpu = true) natively from a clean clone of its commit, only for a project in [lab] trusted: the gpu checks under one turn of the GPU lease, which their steps join; the records go where ci.run keeps them. |
| `ci.gate` | container | The push gate: refuse pushed commits whose trees have not passed every required job in Workbench's records. |
| `ci.status` | container | What a project's commit has passed, and what it still needs before the gate lets it through. |
| `ci.hook` | host | Install (or remove) the pre-push hook that asks ci.gate, in the project's own checkout on the host. |
| `ci.actions` | container | Run a project's build and test jobs in its own checkout, like a small CI (ci.run builds from clean trees instead). Also as `tools.actions`. |
| `ci.scrub` | container | Refuse commits carrying attribution trailers, tooling files, local paths or personal data. Also as `tools.scrub`. |
| `ci.matrix` | host | Run a project's command once per Python on the host (a portability lane that needs no GPU): a clean clone of its commit, each interpreter by version (py launcher on Windows, pythonX.Y elsewhere) or path, an environment per version from requirements when given, and each version's result kept. |
| `ci.store-verify` | container | Rebuild what the Store keeps for a project cold (a fresh clone, every job, no build cache) at the commit it was built from, and record whether each file came out to its SHA-256; a file that did not is a finding. Also as `store.verify`. |
| `ci.store-export` | container | Copy what the Store keeps (one project's, or its build of one commit) and every seeded file into a folder laid out as a Store, each file re-hashed on the way. Also as `store.export`. |
| `ci.store-import` | container | Take a folder store-export wrote into the Store: every file re-hashed and refused unless it is its SHA-256, build records merged, seeded files' sources kept. Also as `store.import`. |
| `ci.land` | container | One landing under lock set: each commit descends from its line and has passed its gate checks, its files are found in the Store, the new set.json and commit body are composed and scrubbed, and the lock is held while wb land publishes with the caller's git. Also as `set.land`. |
| `ci.store-platform` | container | Fetch the platform a Set revision names into the Store: each JDK build for Linux and Windows and the Gradle distribution, by URL and SHA-256 from hosts in store_hosts only, kept by digest and unpacked into jdks/<build>/<os>/ and gradle/<version>/. Also as `store.platform`. |

### `content`

Content and data: modpack scripts, recipes and placement data, read without running the game. (modules/content/wb_content/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `content.scripts` | container | Analyse a pack's GroovyScript folder without running it: the load order runConfig.json gives per packmode and side, import cycles, ids declared twice, recipe additions and removals by file and mod, references to absent mods, and a log's errors mapped to script file:line. |
| `content.recipe-graph` | container | Read a recipe dump as a graph of items: dead ends (inputs no recipe makes), cycles and those nothing outside leads into, and items only removed recipes made that active ones still need. |
| `content.placement` | container | Placement statistics from a CSV of points, per kind: density, nearest-neighbour spread, the Clark-Evans ratio and the quadrat index of dispersion, with their significance. |

### `drive`

Workload drivers: what a game session is made to do and see. Tours checked before a session plays them, camera paths made from keyframes and checked against the frames captured along them, and block galleries laid out for review. (modules/drive/wb_drive/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `drive.tour-lint` | container | Check a tour before a session plays it: required fields, positions inside the height band and border, unique labels, settle times long enough for the view distance, allowed commands, consistently named captures, and how long it takes. |
| `drive.path` | container | Make a camera path from keyframes: poses at a frame rate, interpolated and paced (eased or constant speed, holds and cuts), written as a route or a tour, its speeds, turn rates and acceleration checked against limits. |
| `drive.path-check` | container | Check frames captured along a path, without the game: frame count and pace against the path, missing frames and pictures repeated while the camera moves (by image data and names), and motion continuity (jumps in the frame-to-frame difference), by segment. |
| `drive.gallery` | container | Lay out blocks for review: the commands that clear a platform and place the blocks on it in rows, and the views that capture each row and an overview, as a tour; with a spacing check for the capture distance and field of view. |
| `drive.send` | host | Send one request to a running client's drive agent (mods/drive-agent) through its inbox and wait for its answer: status, screenshot, chat, key, click, close or quit, done on the game's own thread. |

### `frames`

Score captured frames: pass or fail against references, capture noise, wrong-view checks, and PSNR/SSIM quality (from Quadrature's frames.py). (modules/frames/wb_frames/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `frames.compare` | container | Pass or fail a candidate folder of PNGs against a reference folder under a tolerance profile. |
| `frames.measure` | container | The differences between repeated captures of the same views (how a profile's limits are chosen). |
| `frames.check` | container | Name the frames of a run that another capture of the same instance shows were taken facing elsewhere. |
| `frames.quality` | container | Score frames meant to differ from a reference: PSNR, SSIM, edge detail. |

### `jar`

Static analysis of compiled mods: class files, references and what each JAR patches. (modules/jar/wb_jar/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `jar.audit` | container | Check every class in JARs for its class file version, names and forbidden references. Also as `tools.jar-audit`. |
| `jar.survey` | container | List the GL calls and rendering-class patches each Minecraft mod JAR refers to. Also as `tools.mod-survey`. |
| `jar.index` | container | Index what each JAR patches and declares (Mixin configs, targets and handlers, engine systems, event subscriptions, threads, required mods), with a target -> patchers table across the JARs. |
| `jar.references` | container | Find the classes that subclass, override or call given classes and methods, across JARs, without loading them. |
| `jar.hook-targets` | container | Check that named Class::method(desc) and Class#field hook targets still exist in JARs, and digest packaged resources by pattern. |
| `jar.diff` | container | Why two archives of one build differ, entry by entry: entries, order, metadata, content (line endings, JSON order, line order) or only compression. |

### `lab`

Game sessions: native client sessions on the host through wb-host (instances, the GPU lease, the lab home), and headless dedicated-server sessions in the container. (modules/lab/wb_lab/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `lab.gpu` | host | The host's GPU, driver and lease, as wb-host sees them. |
| `lab.prepare` | host | A rental plan's prepare step: the instance its bundle's script prepares, written into the bundle by vast.bundle (the template's [steps]). Retired on this machine: wb play builds and plays a launch. |
| `lab.play` | host | A rental plan's play step: one tour its bundle's script plays, written into the bundle by vast.bundle (the template's [steps]); the results the template names come back as its outputs. Retired on this machine: wb play builds and plays a launch. |
| `lab.resolve` | host | Resolve a launch once into launch.json: the [launch.<name>] of the project.toml at a worktree's HEAD, every member taken by SHA-256 from the worktree's set revision (a line, a hold or a third entry) or, for the project itself, from the Store by the tree CI built; the base project's [launch.<name>] and the tree id of its launch/ overlay at the commit the set names for it (its branch in its home repository when it has no line), or, when that is another product's launch, that launch inherited (members, base, configure, overlays, params, JVM arguments, tour rules) with the launch's own added; the product's configure script and launch/ tree at its commit; refuses a member the set or the Store lacks and two builds of one mod id. Written to <worktree>/.local/launch/<name>.json unless out is given. |
| `lab.turn` | host | Run host routines in order under ONE GPU lease; stop at the first failure. |
| `lab.install-server` | container | Install a dedicated server with its loader's own installer, the installer pinned by SHA-256: every installed file is kept by size and SHA-256 with a receipt, and the folder is a stopped server for lab.server and lab.session. The EULA is accepted only when the request says eula = true. |
| `lab.pack` | container | Get a release-exact modpack from its pack file: a Modrinth pack (.mrpack), each file for the chosen side checked by its SHA-512, or a CurseForge pack (manifest.json), each file found through the CurseForge API (secret curseforge_api_key) and checked by its SHA-1; downloads only from allowed hosts, a shared cache, the overrides laid over, with a receipt. Starts nothing. |
| `lab.assemble` | container | Assemble a game runtime from a launcher instance's metadata: libraries by the target's rules, natives, the client JAR and assets fetched against their SHA-1 into a shared cache (or found in candidate folders), the natives unpacked, and runtime.json naming the main class, arguments and class path. Starts nothing. |
| `lab.build` | host | Build a game instance from a resolved launch file (lab.resolve's launch.json) and nothing else: the runtime (the server by its installer on the launch's JDK from the Store, the client from its launcher instance, every runtime file by SHA-256), each member's Store file hashed again and placed by side, the base's files, JVM profile and overlay folder (exported by the tree id the launch names), the product's overlay and its configure script for a trusted project, and instance.json naming every file by SHA-256 relative to {instance}, {cache} and {jdk:N} with the server's and client's command lines. One template per launch file, base and parameters; instances are hard-linked from it. Nothing is started. |
| `lab.run` | host | One session of an instance lab.build made, from its receipt (instance.json) alone, under the GPU lease: open (a person plays; console lines appended to the session's commands.txt go to the server), tour (the launch's tour rules drive the game through a tour's views, each captured and filed by label), measure (a tour that is evidence: quiet, on a fresh copy of the template, only for the launch file named) or driver (the launch's declared driver, a trusted project's program, gets the turn token, the lease, the instance.json path and, by the name [lab] env gives it, the runtime cache). Every cache, JDK and member file is hashed against the receipt first, and a JDK of another build is refused. The port, JVM options, window, views, edits and debugger are the session's: they go into session.json with the member hashes, JDK build, GPU and driver, and never into the template. A stop waits at least 180 s for the server's own exit; a failure names the decisive line, the crash report and the next command. |
| `lab.keep` | host | Keep a session: retention never removes the session, its instance, its template or its launch file. |
| `lab.freeze` | host | Keep the old route's instances (folders with no instance.json, as the project launchers prepared them) as read-only evidence: a manifest of every file by size, time and SHA-256, the world imported into the lab's world store by its world key (seed, layout scale, the definitions' SHA-256, the Cubature JAR that wrote it), tours/ and runs/ copied into one session kept with lab.keep, then every file marked read-only. Without apply it only inventories. Never moves, deletes or follows a link; a folder reached through a link is refused. lab.thaw is the undo. |
| `lab.thaw` | host | Undo lab.freeze: clear the read-only mark from exactly the files a freeze's manifest says it marked, each only while its size and time are the ones recorded. The imported world and session are copies and stay. |
| `lab.server` | container | Run a dedicated server from a copy of a stopped server folder: wait until it is ready, send console commands, let it run for ticks, seconds or until a line, stop it gracefully, and keep its console, new crash reports, chosen files and a receipt. |

### `ledger`

Formal results, costs and market prices in one queryable database. (modules/ledger/wb_ledger/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `ledger.query` | container | Run a read-only SQL query (median() and inflate() available). |
| `ledger.runs` | container | List recorded runs with their inputs, optionally by label pattern. |
| `ledger.findings` | container | List findings, optionally by status. |
| `ledger.show` | container | Show one run (by label or id) with its inputs, jobs and measurements, or a finding (by id) with its runs. |
| `ledger.costs` | container | Rental costs: estimated and billed, with phases and hosts. |
| `ledger.calls` | container | Requests made to providers, newest first. |
| `ledger.add-run` | container | Record a run with what it ran on and, optionally, measurements; a label recorded already gets the fields, inputs and measurements given. |
| `ledger.add-finding` | container | Record a finding resting on runs and jobs; the jobs' evidence is pinned (never pruned). |
| `ledger.supersede` | container | Mark a finding superseded by another; refused when a third supersedes it already, or when it supersedes the other itself. |
| `ledger.link-run` | container | Link a run to the Workbench jobs its results came from, and set the machine or rental it ran on. |
| `ledger.add-machine` | container | Record a machine's specs (one of ours, or a host by hand); specs given replace those recorded. |
| `ledger.prune` | container | Drop the stored response bodies of provider calls older than N days, keeping the calls and the offers they listed; a dry run unless execute=true. |
| `ledger.import` | container | Import a results database, mapping ids so references hold (idempotent; repairs earlier imports); an older schema's run columns become inputs. |
| `ledger.import-ci` | container | Import a CI project's run records: one run per CI run (ci/<project>/<run id>), its trees as inputs, each job's seconds and status as measurements (idempotent). |
| `ledger.import-results` | container | Import result files (wb/ledger-result/v1: a run's label, fields, inputs, measurements, jobs and findings), mapping each run to the ledger's of its label (idempotent). |
| `ledger.import-json` | container | Import JSON files a tool writes in its own shape, by a reader of [modules.ledger.readers]: each file, or each entry of one, a run mapped to the ledger's of its label (idempotent). |
| `ledger.parse-log` | container | Parse a log with a parser of [modules.ledger.parsers] into a run's measurements (idempotent: a log parsed again into its run changes nothing). |

### `mixin`

Mixin and patches: verifying Mixin and patch application without the game. (modules/mixin/wb_mixin/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `mixin.remap-proof` | container | Prove a production (reobfuscated) JAR's Mixins were remapped: no refmap entry, renamed member, shadow, overwrite or reference left under a development name. |
| `mixin.apply` | container | Apply Mixin configurations offline through the project's own harness JAR and report its targets, hooks and failures (strict: an injector that hooks nothing fails). |
| `mixin.doctor` | container | Check JARs' Mixin configurations against a loader policy without the game: each parses and lists Mixins that are in the JAR and its package; its refmap, plugin and @Mixin targets exist; its compatibilityLevel, minVersion and injection points are ones the loader supports, the level covering its classes' class-file version; none is duplicated across JARs or left unregistered. |

### `plan`

Declarative turns and campaigns: pinned inputs, a declared protocol, steps that are Workbench commands; checked first, then run on the host or a rented GPU. (modules/plan/wb_plan/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `plan.check` | container | Check a plan without running anything: commands active, parameters known, inputs present and pinned, references sound. |
| `plan.run` | container | Check, then run a plan step by step (consecutive host steps under one GPU lease); stops at the first step its protocol does not accept. |

### `probe`

Probes and harnesses: JVM runs without the game. Small Java programs compiled against an artifact and run in throwaway JVMs on known JDKs. (modules/probe/wb_probe/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `probe.jdk-matrix` | container | Compile probe sources with javac --release N against an artifact's class path, run each in throwaway JVMs on several JDKs, and pass each on exit 0 plus a marker line. |
| `probe.api-consumer` | container | Prove an API JAR stands alone: compile a consumer against it and its declared libraries only with javac --release N, run it phase by phase in fresh JVMs, and check what the JAR links. |

### `profile`

Profiling and diagnostics of Java game processes and the GPU they run on: JFR recordings over time windows, cost charged to the code that incurred it, native memory by owner, the JFR settings that feed them, and whole-device GPU telemetry sampled beside a run and read by window. (modules/profile/wb_profile/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `profile.jfr` | container | A JFR recording window by window: execution samples by package-prefix group, allocation, GC pauses, file I/O, lock waits and tick stages, with a Chrome trace of the windows. |
| `profile.attribute` | container | Charge a recording's thread CPU, samples, allocation, compilation and waits to the owner whose code incurred them, by a package-prefix -> owner map. |
| `profile.memory` | container | A JVM's memory by owner class (heap, JVM internals, code cache, native libraries and drivers, thread stacks, direct buffers, unaccounted) from its NMT summary and memory map, live or from saved files. |
| `profile.settings` | container | Write JFR settings (a .jfc) that record what profile.jfr and profile.attribute read, tracing a stage map's methods, and give the JVM options to use them. |
| `profile.gpu-sample` | host | Sample the host's GPUs beside a run: clocks, power and performance state, utilisation, temperature, memory and throttle reasons, for some seconds or until cancelled, as JSON lines with a summary. |
| `profile.gpu-report` | container | GPU samples window by window: min, median and max of every metric, time in each performance state, throttling episodes, and a finding for each expectation (performance state, clocks, power) a window breaks. |

### `release`

Publish built mod artifacts to GitHub releases, Modrinth, CurseForge and Maven repositories: checked against the publication allow-list, quoted as a plan, sent only after an explicit yes. (modules/release/wb_release/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `release.check` | container | Check files against the publication allow-list, with no network: production JARs (no dev or sources-only JAR), class versions and JVM attributes against the target, declared dependencies, versions that agree; one finding per violation. |
| `release.publish` | container | Publish one version's files to one channel after an explicit yes on its plan (every file with its SHA-256 and size, the notes' digest, how it is released); asks again if anything differs when it runs, never publishes over a version the channel holds, and leaves a receipt. |
| `release.status` | container | What a channel already holds for a version (the release, its files and their digests), compared with local files when given; read-only. |

### `render`

Rendering: shader pack checks (the pack contract, every stage compiled and linked per profile, driver hazards), whether a pack really loaded, and GL qualification of a card. (modules/render/wb_render/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `render.validate` | container | Check an OptiFine-format shader pack: its contract (includes, paired stages, #version, properties, option references), every stage compiled and every program linked per profile with glslangValidator, and loop shapes some drivers mishandle. |
| `render.load-verdict` | container | Decide from a game log whether a shader pack really loaded: no fallback, load, compile, link or option errors, the pack active (and its profile, per dimension), captures only after it loaded. |
| `render.gl-gate` | container | Check a GL probe report (wb/gl-probe/v1 JSON, or a glxinfo dump) against a gate: GL version, profile, vendor, memory and required extensions, to qualify a local or rented card. |
| `render.gl-run` | host | Run a project's GPU test task (a Gradle test task opening a hidden GL context) on the host under the GPU lease: its JUnit results kept, each failing test named, and the capability report it writes (wb/gl-probe/v1, or a glxinfo dump) judged against a gate. |

### `runs`

Run custody (from orun): steps in a clean tree at an exact commit, repeated runs, interleaved A/B with paired statistics, and evidence records that refuse a comparison of runs differing in more than their treatment. (modules/runs/wb_runs/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `runs.at-commit` | container | Run steps in a clean, isolated tree of a repository at one commit; the source checkout is never written. |
| `runs.repeat` | container | Run a command several times (after warm-up runs) and summarise one of its measurements, keeping each measured run's evidence record. |
| `runs.ab` | container | Interleave runs of a command with params A and B (AB, BA, ...), then bootstrap the paired change of a metric; with a treatment declared, refuse a verdict unless the runs differ in it alone. |
| `runs.evidence` | container | Record the facts that define a finished run (its params with their defaults, inputs by digest, toolchain and machine, and the facts and files declared) as evidence.json in the out folder; give it the run's own to keep the record beside the run's outputs. |
| `runs.compare-evidence` | container | Refuse a comparison of two sets of runs that differ in more than the declared treatment: every control field must match in every run and every treatment field must differ between the sides; every difference is reported. |
| `runs.stats` | container | Bootstrap the median paired change between baseline and candidate runs. Also as `tools.stats`. |
| `runs.workload-guard` | container | Exclude timing runs that overlap Java work the run did not start. Also as `tools.workload-guard`. |
| `runs.interleaved` | container | Treatments interleaved within one session, each sample's condition from a hash of its index, paired window by window: median differences, windows led and a bootstrap interval. |

### `selftest`

Exercises the core: echo, sleep, fail, a zero-cost approval, and a call into another command. (modules/selftest/wb_selftest/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `selftest.echo` | container | Return the parameters, write them to a file, and record a measurement. |
| `selftest.sleep` | container | Sleep, reporting progress; cancellable. |
| `selftest.fail` | container | End with the outcome asked for, to check outcome codes. |
| `selftest.spend` | container | A zero-cost command that goes through quote, approval and re-quote like a rental. |
| `selftest.copy` | container | Copy an input file to the output folder (path translation and input hashing). |
| `selftest.sizes` | container | The byte sizes of several input files (a cached command over a list of paths). |
| `selftest.checkout` | container | Name a checkout's HEAD commit (an input recorded by git, not by hashing its files). |
| `selftest.chain` | container | Call selftest.echo through the daemon (as plans do). |
| `selftest.host-echo` | host | Run on the Windows host under the GPU lease and report the turn token's presence. |
| `selftest.quiet-echo` | container | Like echo, but a measurement: needs a quiet machine. |
| `selftest.nest` | container | Hold a CPU reservation, then call selftest.echo as a child job and wait for it (the shape of ci.run calling a helper). |
| `selftest.inline-echo` | container | Like echo, but called inline by a running job, in that job's reservation. |
| `selftest.orphan` | container | Start a process that detaches from this job and outlives it unless the job's processes are killed when it ends. |
| `selftest.tool` | container | Run a tool that sleeps, under a limit (a cached command, to check a run stopped at its limit is never cached). |

### `source`

Formatting, lint and determinism of source code. (modules/source/wb_source/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `source.format` | container | Format Java sources with a pinned google-java-format, or check that they are formatted. Also as `tools.java-format`. |
| `source.strict-math` | container | Ratchet inexact java.lang.Math calls toward StrictMath in deterministic Java code. Also as `tools.strict-math`. |

### `toolchain`

JDKs, pinned inputs, dependency repair and the rescue repository. (modules/toolchain/wb_toolchain/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `toolchain.jdk` | container | Find an installed JDK by major or exact version and vendor. Also as `tools.jdk`. |
| `toolchain.pins` | container | Download third-party inputs pinned by SHA-256 and verify them offline. Also as `tools.pins`. |
| `toolchain.patch` | container | Repair a JAR without forking it: rebuild named classes from sources against its own classpath and swap only those entries, checked against its SHA-256 and recorded in a receipt. |
| `toolchain.repo-add` | container | Rescue a file into the repository under its Maven coordinates, from a path or a URL: bytes other than the expected SHA-256 are refused, bytes already held are never replaced, and a minimal POM, plugin markers, metadata and a receipt go with it. |
| `toolchain.repo-list` | container | List the repository's coordinates with their digests and receipts, and the substitutions builds get. |
| `toolchain.repo-verify` | container | Hash every file the repository holds again against its receipt and digest files, check each version has a POM and each artifact its metadata, and name any file without a receipt. |
| `toolchain.repo-init` | container | Make the Gradle init script that serves the repository read-only (asked only for what it holds) and applies the substitutions; written into a Gradle home's init.d when one is given. |
| `toolchain.gradle-home` | host | Show, or with write=true write, the block of a Gradle home's gradle.properties that lists this machine's JDKs, as CI's Gradle home does, so local builds pass no JDK paths of their own; other lines are kept. |

### `tools`

The engine of the former standalone tools. Its tools are commands of the taxonomy's modules: toolchain, build, jar, source, runs and ci. (modules/tools/wb_tools/module.toml)

No commands of its own.

### `vast`

Rent GPU machines on Vast.ai, run bundles on them, and record market prices and charges. (modules/vast/wb_vast/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `vast.offers` | container | List rentable offers for a card (or every configured card); rents nothing. |
| `vast.market` | container | Record a market snapshot: every listed offer of the configured cards, asked three times. |
| `vast.charges` | container | Record the account's billed charges of the last days. |
| `vast.instances` | container | List the account's instances. |
| `vast.destroy` | container | Destroy one instance (stops its billing). |
| `vast.keys` | container | List the account's SSH keys. |
| `vast.rent` | container | Rent one offer, run a bundle on it, fetch the results, destroy it. |
| `vast.probe` | container | Rent one offer briefly to check it gives OpenGL 4.6 through VirtualGL, then destroy it. |
| `vast.bundle-check` | container | Check a bundle (manifest, pinned files, size) and show the bootstrap it would get; rents nothing. |
| `vast.bundle-launch` | host | Make a rental bundle from a launch file alone: every member and runtime file by SHA-256 from the Store, the platform's Linux archive of the launch's JDK, the overlays and trusted configure script by id, Workbench's own lab.build and lab.run code and a run script that builds the instance and plays one tour session headless, writing session.json. Checked against the launch file, with a manifest of every file's SHA-256. Rents nothing. |
| `vast.bundle` | container | Make a rental bundle from a template in config, filled with a plan's game steps and the template's input archives; checked, with a manifest of every file's SHA-256. Rents nothing. |
| `vast.lab-inputs` | container | Build the input archives a template declares, each from its pinned commit in a throwaway clean tree by its recipe, with a receipt; archives the pin cache holds are taken from it. Rents nothing. |

### `world`

Worlds on disk and as generated: what a stopped save holds, how two worlds differ (save by save or by digest), and a generator's windows against golden digests, sampled without the game. (modules/world/wb_world/module.toml)

| Command | Runs on | Summary |
|---|---|---|
| `world.read` | container | Read a stopped save (Anvil or cubic): block states, block entities, entities, heightmaps and light, by region or box. |
| `world.diff` | container | Compare two stopped saves unit by unit through canonical NBT, blocks named by each world's own registry. |
| `world.digest-diff` | container | Compare two world digests (wb/world-digest/v1) and report what differs, by category. |
| `world.light-diff` | container | Compare the saved light of two stopped saves, only where every neighbouring chunk or cube is saved in both. |
| `world.golden` | container | Check a world generator against golden digests of fixed windows, sampled by its own code without the game, and prove them deterministic: a second run takes the windows in another order on another thread count. |
| `world.preview` | container | Paint a layer of a window of a generated world to a PNG: the generator's sampler returns the layer whole, and a palette colours it. |
<!-- end generated commands -->
