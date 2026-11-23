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
  as its budget.
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
