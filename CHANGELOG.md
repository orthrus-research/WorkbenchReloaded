# Changelog

## 0.1.0 (unreleased)

- The core: config folder with deeper-wins merge and origins; static module manifests; activation by
  requirement; durable jobs with quotes and approval; scheduling by GPU lease, CPU budget and quiet machine;
  one worker process per job; caching of deterministic commands; `/v1` on localhost with a token; Minecraft-style
  logs with a JSONL sidecar; retention.
- Modules: build, toolchain, jar, mixin, probe, source, content, assets, world, render, runs, profile, lab, drive,
  ledger, vast, ci, release, plan, frames, tools (the former standalone tools, 0.6.0), selftest.
- The Windows host runner, sharing the GPU lease and lanes with gpu_queue.py; host routines that need no GPU run
  beside the lease holder.
- SDKs: Python with the `wb` CLI, Node/TypeScript, Java with a Gradle plugin (the same JAR bytes on every machine,
  and `WbRun` tasks that keep a receipt), PowerShell.
- The image: Ubuntu 26.04 by digest, apt from a pinned snapshot, Temurin 8, 17, 25, 26, 27 and 28 EA by SHA-256;
  optional toolchains (Node, Zig), each pinned by SHA-256 and built in only for a machine that names them.
- The drive agent: a client mod for 1.12.2 on Cleanroom that captures views once they settle and answers
  `drive.send`.
