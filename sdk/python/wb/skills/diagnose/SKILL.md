---
name: diagnose
description: Find out why Workbench does not do what was asked - a job that waits or failed, a command that is inactive, a daemon that does not answer, a full disk, a folder that is behind. Use it when a Workbench call fails or stalls, before retrying anything.
---

# When something does not work

Read what Workbench says before trying again. Every refusal names its reason, and most name the fix. A second
attempt without a change gets the same answer and queues more work behind the first.

## A job that waits

`job(job=...)` shows `waiting` with the reason:

| Reason | Means | Do |
|---|---|---|
| `lock <name> held by <job>` | another job holds a named lock, such as one commit's checks | wait; `job` on the holder shows how far it is |
| the GPU lease | a game or measurement holds the GPU | wait; `cli(args=["gpu"])` shows the line |
| the budget | the machine's CPU or memory is in use | wait |
| `behind <job>` | it queues behind an earlier job | wait |
| a quiet machine | a measurement waits until nothing else runs | wait, and start nothing heavy |
| approval | it stopped at a quote | the `spend` skill |

`status()` names stalled jobs with their reason. A stalled job, or a lane `cli(args=["gpu", "stuck"])` lists,
needs the person: report it with the reason, and do not cancel jobs you did not start.

## A job that failed

- `finding`: not a failure of Workbench. The command found something; `reasons`, `data` and `findings` say
  what.
- `usage`: the request was wrong. `reasons` names the parameter; `describe` the command and correct it.
- `environment`: something is missing on the machine. Each reason has a `fix`. Report it; do not install
  things or change config to get past it unless the person asked.
- `interrupted`: the daemon or the machine stopped under the job. Run it again once `status()` is healthy.

`logs(target="<job>", limit=80)` has the job's log lines, the newest last; raise `limit` only when the cause
is further up. A job's outputs often hold a log per step, by host path.

## A command that is inactive

`commands(module=...)` and `describe` mark it `INACTIVE` with the reason and the fix: a secret not set, a
binary or module missing, a host runner that is off. Tell the person what it needs. The config that decides it
is `cli(args=["config", "show", "--origin"])`, each value with the file and line it came from.

## The daemon does not answer

A tool answering "Workbench is not reachable" means the container is not running, or its address changed.
Starting it (`wb up`) and deploying (`wb deploy`) belong to the person and a terminal: say so, with the
message. An answer that the CLI and the daemon run different commits is cured by `wb deploy`, also theirs.

`status()` shows the rest: draining for a deploy (jobs queue and run after it), a host runner on another
commit, a start-up hook that failed, config keys in an old spelling.

## A project, a folder, a disk

- `cli(args=["status", "--doctor"])` names, with file and line, what still ties a project to routes Workbench
  retired. Exit 1 means it found something; report the lines.
- `set <rev>, N behind` before a verb's answer: the folder builds against an older set revision.
  `cli(args=["work", "sync"], cwd=...)` moves it to the head (`land` skill).
- A refused push: the `checks` skill.
- A full disk: `cli(args=["gc"])` lists what the retention rules would remove, with a digest, and removes
  nothing. `["gc", "--yes", "<digest>"]` removes exactly that list. Show the list first, and remove only when
  the person asked.
