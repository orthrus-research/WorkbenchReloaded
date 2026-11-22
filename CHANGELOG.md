# Changelog

## 0.1.0 (unreleased)

- The core: config folder with deeper-wins merge and origins; static module manifests; activation by
  requirement; durable jobs with quotes and approval; scheduling by GPU lease, CPU budget and quiet machine;
  one worker process per job; caching of deterministic commands; `/v1` on localhost with a token; Minecraft-style
  logs with a JSONL sidecar; retention.
- Modules: vast, ledger, lab, plan, frames, runs, tools (the former standalone tools, 0.6.0), selftest.
- The Windows host runner, sharing the GPU lease and lanes with gpu_queue.py.
- SDKs: Python with the `wb` CLI, Node/TypeScript, Java with a Gradle plugin, PowerShell.
- The image: Ubuntu 26.04 by digest, apt from a pinned snapshot, Temurin 8, 17, 25, 26, 27 and 28 EA by SHA-256.
