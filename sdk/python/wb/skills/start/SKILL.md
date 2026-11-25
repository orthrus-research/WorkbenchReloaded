---
name: start
description: How to operate Workbench (wb) through this server's tools, from finding a command to reading its result. Load it before the first Workbench call of a session, or when unsure which tool or verb fits a task.
---

# Operating Workbench

Workbench runs commands for Minecraft modding (builds and checks, JAR analysis, game sessions, rentals,
releases) and answers every one of them in the same shape. Three words carry the whole model:

- **Command**: `module.command`, such as `ci.run` or `jar.audit`. Modules declare them; nothing else runs.
- **Job**: one execution of a command, named `j_...`. A job is durable: it outlives the call that started it,
  and anyone can look at it later.
- **Envelope**: the one result of a job, the same fields for every command.

## The tools

| Tool | Use it to |
|---|---|
| `commands` | find a command: no argument gives every module with its command names; `module` or `query` gives one line per match |
| `describe` | read one command's parameters, where it runs, whether it spends or is cached |
| `run` | start a command and wait for its envelope |
| `job` | look at a job again, or wait longer for it |
| `jobs`, `logs`, `status` | list recent jobs, read a job's or module's log lines, see the daemon's health |
| `cancel` | end a job that has not settled |
| `approve` | put a quote to the person (see the `spend` skill) |
| `cli` | run a verb that acts on a folder or the machine: `check`, `work`, `land`, `play`, `store`, `gpu`, `gc` |

## Running a command

1. `describe` the command once before its first use. Parameter names are exact: an unknown one is refused.
2. `run` it with `params` as an object, each value typed as `describe` shows (a list as a list, a flag as
   true or false). Give paths as absolute host paths, or give `cwd` and write them relative to it. Workbench
   maps host paths into its container itself.
3. Read the answer's first line: outcome, command, job id, summary.

`run` waits 120 seconds unless `wait` says otherwise. When the time passes first, the answer names the job and
what it waits for, and the job goes on. Follow it with `job(job=..., wait=...)`. Do not run the command again:
that queues a second job behind the first.

## Reading an envelope

A settled job has one of four outcomes, and they call for different next steps:

| Outcome | Means | Next |
|---|---|---|
| `ok` | it ran and found nothing wrong | use `data` and the outputs |
| `finding` | it ran, and what it examined is not right: a failed check, a difference, a violation | this is the result; report or fix what it found, do not retry |
| `usage` | the request was wrong: a parameter, a path, a name | correct the request from `reasons`, then run again |
| `environment` | the machine lacks something the command needs | each reason carries a `fix`; tell the person rather than work around it |

A job that has not settled is `queued` (its `waiting` reason says for what: a lock, the GPU lease, the budget),
`running`, or `awaiting_approval` (it stopped at a quote). `cancelled` and `interrupted` jobs produced no result.
Waiting is normal: one GPU job runs at a time, and a lock is held by the job its reason names. Do not cancel
another job to get ahead.

The default answer is a few lines: summary, reasons, the start of `data`, outputs with their host paths. Ask for
`detail="full"` only when a field you need is not there; it is the whole envelope as JSON and can be long.
[references/envelope.md](references/envelope.md) lists every field.

## Rules

- A command marked `$` spends money or publishes. It stops at a quote, and only the person approves it. Load
  `spend` before running one.
- An inactive command says why and how to fix it. Report that; do not look for another route to the same end.
- A deterministic command (`=`) answers from its cache when its inputs are the same. `fresh=true` runs it again.
- `cli` runs verbs in the folder you give as `cwd`. Always give `cwd` for `check`, `work`, `land`, `play` and
  `store`: they read the worktree they stand in. [references/verbs.md](references/verbs.md) lists the verbs and
  their arguments.
- Starting, stopping and deploying Workbench itself are not served here. They belong to a terminal and to the
  person.

## The other skills

- `checks`: run a project's checks and read what failed; what a push needs.
- `land`: worktrees with a lifecycle, the Set, and landing commits in it.
- `play`: game sessions under the GPU lease.
- `spend`: quotes and approval, for rentals and publishing.
- `diagnose`: a job that waits or failed, an inactive command, a daemon that does not answer.
