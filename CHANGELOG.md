# Changelog

## 0.1.0 (unreleased)

- The core: config folder with deeper-wins merge and origins; static module manifests; activation by
  requirement; durable jobs with quotes and approval; scheduling by GPU lease, CPU budget and quiet machine;
  one worker process per job; caching of deterministic commands; `/v1` on localhost with a token; Minecraft-style
  logs with a JSONL sidecar; retention.
- Admission that cannot deadlock: a running job's descendants go ahead of the queue, a job that ends cancels
  them, every queued job says why it waits (`waiting` in the envelope), health names stalled jobs. Commands may
  declare named locks and `quiet_if` params, and `inline` commands a running job calls in its own reservation.
- A tool runs until its process exits, not its pipe; deadlines fire without output; nothing a job started
  outlives it (a Job Object on Windows, a child subreaper on Linux); a run stopped at its limit is a finding with
  reason kind timeout, never cached.
- From the folder you stand in, on the host: `wb setup` installs scrub and push-gate hooks that need no daemon;
  `wb check`, `wb job`, `wb work` (start, add, adopt, finish), `wb backup`. `wb deploy` tests, builds, drains and
  restarts the daemon and host runner together onto an immutable release; every part reports its commit and a
  CLI or host runner on other code is named with the fix.
- The Store: a producing job's artifacts are kept by SHA-256 and by project and tree, and in the pin cache;
  `wb store` finds them, gives a kept file's path, or seeds bytes no container builds. A pinned repository with
  `passed` must have passed its own checks, so a consumer need not build and test it again.
  `store.verify` rebuilds a kept build cold (every job, no build cache) and records whether its bytes came out
  the same; `store.export` and `store.import` carry builds and seeded files between Stores, every file
  re-hashed. A lab input step with `store` takes CI's JARs for a clean checkout's tree instead of building.
  `toolchain.rebuild`, `build.freeze` and `build.stage` are gone: `store.verify` is how a pin is shown to rebuild.
- The actions engine runs inside its job's reservation: no run lock, the job id as its run id, the job's memory
  as its budget. A project's steps get an allow-listed environment, none of Workbench's own.
- Each job calls the API with a token of its own, revoked when it ends, that can never approve a quote: approval
  takes the person's token.
- The Set: set.json (schema 1) names each project's line (branch, commit and the SHA-256 of its files), holds
  with seeded bytes and the platform. `wb land` is the only route to a set-named branch: its gate checks run
  first, `set.land` composes and scrubs under lock `set`, and the CLI publishes with the caller's git (landing
  refs, the Set commit as the commit point, then the branches); an unfinished publish is finished or cleaned up by
  the next landing. The push gate takes a set-named branch only from a landing. Each worktree builds against the
  revision in its `.set`; `wb work sync` moves it, and every verb says how far behind it is. `wb work --all`
  lists each line, open landings, and unconverted projects with the pins they still carry. `store.platform`
  fetches the platform's JDKs and Gradle into the Store by SHA-256 from allowed hosts.
- project.toml (schema 1): a project's checks, gate and artifacts, read before actions.toml. A check that reads
  another project's files declares them (`tools`), and `ci.run` lays them beside the clone at the set's commit.
- Third entries: `wb land --third` fetches a file by Maven coordinate from an allowed host, or, for a file with no
  Maven publication, takes the one JAR dropped in `<out>/third/<name>/`, seeds it into the Store with its source,
  and lands it by SHA-256 with its licence.
- `wb gc` replaces `wb prune`: each kind of thing Workbench creates has a retention rule (`wb gc --rules`); the
  list names every item, its class, size and reason, with a digest, and `wb gc --yes <digest>` removes exactly
  that list or nothing. Pass records, set exports and every byte a set names, seeded and indexed Store files and
  kept sessions are never listed. Lab-home items go only under the GPU lease; `wb gc --nightly` removes the
  unattended classes only. Folders are removed with every link in them unlinked first. Old releases are gc's
  (all but the one in service and the one before it), no longer removed by `wb deploy`.
- The README's module table and docs/commands.md (every command, where it runs, its summary) are generated
  from the module manifests by `tools/command_tables.py`; a test fails when either differs from them.
- Workbench's own project.toml: its suite as four checks (generated, core, modules, engine; the first and
  last pure, with inputs), all four gating a push; `tests/run_all.py --part` runs one.
- `lab.resolve`: a project's `[launch.<name>]` resolved once into launch.json, every member by SHA-256 from the
  worktree's set revision and the project itself from the Store; two builds of one mod id are refused. The
  platform's Cleanroom files go into its `runtime.files`.
- `lab.build`: an instance built from a launch file alone (runtime, content, base, configure hook, receipt), one
  template per launch file hard-linked into each instance, with `instance.json` naming every file by SHA-256 under
  `{instance}`, `{cache}` and `{jdk:N}` and the command lines that start it. The server's port is a session's
  input, not the template's.
- `[lab]` in machine config: `home`, `trusted` (the projects whose configure hooks and drivers may run here),
  `eula` and `env` (more variable names a driver gets the turn token, lease or instance in); `wb setup` names each
  key not yet set. A configure hook of a project not trusted is refused before anything is built.
- `lab.run`: a session of a built instance from its receipt alone, under the GPU lease: open, tour (the launch's
  tour rules drive the game through the views, each filed by label), measure (quiet, a fresh copy, only for the
  launch file named) and driver (a trusted project's declared driver, given the turn token, the lease and the
  instance.json path). Every cache, JDK and member file is hashed against the receipt first; a JDK of another
  build is refused, the same build elsewhere accepted. The port, JVM options, window, views, edits and debugger
  are the session's and go into `session.json` with the member hashes, JDK build, GPU and driver; `debug` opens
  JDWP on loopback and writes an attach configuration. A stop waits at least 180 s for the server; a failure names
  the decisive line, the crash report and the next command. `lab.keep` keeps a session as a retention root.
  `lab.resolve` carries a launch's `tour` rules and `drivers` into launch.json.
- `wb gpu`: the lease's holder, its waiters in the order they are served and stuck lanes; `wb gpu run` gives any
  command a turn in a lane of its own, with a note, a limit, `--detach` (outlives the shell) and `--quiet`; a
  waiter started again keeps its place; `wb gpu leave` withdraws a lane. The old lock path and variable names stay
  configured aliases (`[runner] lease_file`, `token_env`, `lane_env`, `[lab] env`).
- Host checks: `ci.host-run` runs a project's `on = "host"` checks natively from a clean clone, only for a project
  in `[lab] trusted`, the `gpu = true` ones under a turn of the GPU lease whose token their steps get, and records
  their passes beside `ci.run`'s; `wb check` sends the host checks it is asked for there and the rest to `ci.run`,
  printing which ran where, and `ci.run` leaves host checks out.
- `wb play`: resolves a launch at the folder's set revision (or replays `--from` a launch file), builds a missing
  member by its producing check first (or refuses with `--no-rebuild`), builds or reuses the instance and plays a
  session on this machine under the lease, printing the job, launch, instance and session at once, streaming the
  consoles, and naming the decisive line, crash report and next command on a failure.
- The actions engine is a library inside the job: `ci.run` and `ci.host-run` call `actions.execute` in their worker,
  giving it the steps' whole environment, the project's interpreter as `{python}` and the set's identities, instead
  of starting the engine as a process and reading its JSON from the output. Steps still run as processes of their
  own under the project's interpreter, and cancelling the job kills them; the `actions` command line still reads the
  identities from WB_SET_IDENTITY, WB_JDK_IDENTITY and WB_TOOLS_ROOTS.
- `[paths]` names each folder once, as the host names it: `out` (the read-write root, mounted at `/mnt/out`, holding
  the Store, the run records and the one pin cache as `store`, `ci/records` and `cache/pins`), `releases`, `backup`
  and `[paths.roots]` for the rest, each mounted at `/mnt/<name>`. The old spellings, `[paths.roots.out]`, a root's
  `container`, `[modules.ci] store`, `records` and `pin_cache` and `[modules.toolchain] pin_cache`, are read for one
  more release with a warning in the daemon's log, `wb status` and `wb config show --local`.
- Typed module tables: a config object may name its `fields` (a table of fixed keys) or its `entries` (one spec for
  every value of a table by name). `[modules.ci.projects]` (repo, config, jdk, env, python, workflow, gate),
  `[modules.ci] tool_env`, `[modules.toolchain] substitutions` and `[modules.ledger] parsers` are checked at load
  like every other key, so a wrong key or type stops the daemon with its file and line instead of failing the first
  command that reads it.
- Modules: build, toolchain, jar, mixin, probe, source, content, assets, world, render, runs, profile, lab, drive,
  ledger, vast, ci, release, plan, frames, tools (the former standalone tools, 0.6.0), selftest.
- The lab reads a profile's checkouts at a ref, each from a snapshot fixed at its commit for the whole job
  (`lab.checkouts` shows and prunes them), so no checkout is moved by hand after a push or under a running job.
- The Windows host runner, sharing the GPU lease and lanes with gpu_queue.py; host routines that need no GPU run
  beside the lease holder.
- SDKs: Python with the `wb` CLI, Node/TypeScript, Java with a Gradle plugin (the same JAR bytes on every machine,
  and `WbRun` tasks that keep a receipt), PowerShell.
- The image: Ubuntu 26.04 by digest, apt from a pinned snapshot, Temurin 8, 17, 25, 26, 27 and 28 EA by SHA-256;
  optional toolchains (Node, Zig), each pinned by SHA-256 and built in only for a machine that names them.
  The entrypoint gives Workbench's user the pin caches and records folder configured inside an rw root
  (`WB_OWNED_DIRS` from `wb up`), as it does the state volume.
- The drive agent: a client mod for 1.12.2 on Cleanroom that captures views once they settle and answers
  `drive.send`.
