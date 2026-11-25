---
name: play
description: Play and drive Minecraft game sessions with Workbench under the GPU lease - resolve a launch, build its instance, run a tour, a measurement or a driver, send requests to a running client, and keep a session. Use it when asked to launch the game, capture views, measure, or when a job waits for the GPU.
---

# Game sessions

A launch (`[launch.<name>]` in a project's `project.toml`) lists what a game is made of. Resolved once, by
SHA-256, it is a launch file; an instance is built from that file; a session is one run of an instance. Every
session runs under the GPU lease: one holder at a time on the machine, the rest wait in lanes.

## The GPU lease

`cli(args=["gpu"])` shows the holder, the waiters in the order they are served, and stuck lanes. A job whose
`waiting` reason names the GPU is in that line. Wait for it; do not cancel the holder, and do not start a game
or a GPU program outside the lease. `["gpu", "stuck"]` lists what needs a person; report it. A turn for a
command of your own is `wb gpu run --note "<what for>" -- <command>` in a terminal; `cli` does not run it.

## Playing

```
cli(args=["play", "<project>:<launch>", "--tour", "<tour file>"], cwd="<the worktree>", timeout=3600)
```

`play` resolves the launch at the folder's set revision, builds any member the Store lacks by running its
check, builds or reuses the instance, and plays the session. It prints the job id, the launch file, the
instance and the session folder at once, and on a failure the decisive line, the crash report and the next
command. Report those as printed.

A session through `cli` must end on its own, so choose a kind that does:

| Form | Session |
|---|---|
| `--tour <file>` | plays a tour and files its captures as `tour/NN-<label>.png` |
| `--tour <file> --measure` | the tour as evidence: quiet, on a fresh copy of the template |
| `--driver <name>` | runs a driver the launch declares, for a project the machine trusts |

An open session, where a person plays, lasts until they close the game: ask them to run `wb play` in a
terminal. `--desktop hidden` (Windows) runs the client on a desktop of its own that never takes the person's
input; use it for sessions nobody watches.

More options, as `describe("lab.run")` and the verb's help explain them: `@<ref>`, `@set` or `@head` after
the project, `--from <launch.json>` (replay a launch file's exact bytes), `--name`, `--param k=v`, `--jvm`,
`--view k=v`, `--window WxH`, `--edit side/file:key=value`, `--fresh-world` or `--keep-world`, `--no-rebuild`,
`--debug client|server|both`.

## Evidence

- A measurement is evidence only as `--measure`. A session with `--debug`, with `--no-rebuild` after a refusal,
  or with a stand-in is not, and says so.
- What a session chose goes into its `session.json`; the template is never changed.
- Retention removes old sessions. `run(command="lab.keep", params={"session": "<id>", "reason": "<why>"})` keeps
  one: its session, instance, template and launch file. Keep what a report cites.

## A running client

The drive agent, a small client mod a session adds, answers requests on the game's own thread. `drive.send`
sends one and waits for its answer: `status`, `screenshot`, `chat`, `key`, `click`, `close` or `quit`.
`describe("drive.send")` gives the parameters; the session folder holds the inbox.

## Without the game

Much needs no session and no GPU: what a JAR contains (`jar.*`), a world on disk (`world.*`), shader packs
(`render.validate`), frames already captured (`frames.*`), Mixin application (`mixin.*`). `commands()` lists
them. Reach for a session only when the question is about the running game.
