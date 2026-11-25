---
name: checks
description: Run a project's checks with Workbench and read what failed - the worktree you stand in, a named commit, host and GPU checks, and what a push needs before the gate lets it through. Use it when asked to run CI, verify a change, or explain a refused push.
---

# Checks

A check is a named unit of verification a project declares: argument lists run in a clean clone, in the
container or, for a host check, on the host. The gate list names the checks a push and a landing need. A pass
is recorded for the tree that passed, so the same tree is never run twice.

## The worktree you stand in

```
cli(args=["check"], cwd="<the worktree>")
```

This runs the gate list on the tree git would commit from that folder, uncommitted changes included, so nothing
has to be committed first. It prints the job id at once, then the result.

- Named checks: `["check", "<name>", ...]`. `all` names every check the project declares.
- A host check (`on = "host"`, or `gpu = true`) runs only when named, and only for a project the machine
  trusts. `check` sends those to the host and the rest to the container, and says which ran where.
- `--with <project>=<ref or folder>` builds against another project's files at a ref or worktree. Its checks
  run first; the run with a stand-in records no pass.
- Checks can take many minutes. Give `cli` a `timeout` that fits (seconds), and `--timeout 30m` to the verb.

## A named project and commit

```
run(command="ci.run", params={"project": "<name>", "commit": "<sha or HEAD>"}, wait=0)
job(job="<the job>", wait=600)
```

`ci.run` clones the commit clean and runs the project's checks. Start it with `wait=0` and follow the job: a
build outlasts most waits. `describe("ci.run")` lists the rest: one check or more (`workflow`, `jobs`),
`keep_going`, `cold` (every check again, no earlier pass or cache).

`ci.status` with `project` and `commit` answers what that commit has passed and what it still needs, and runs
nothing.

## Reading the result

- `ok`: every check passed. The pass is recorded for the tree.
- `finding`: a check failed. The answer has a line per check and per step with its exit code, time and peak
  memory; `data.failed` names the failed checks. `logs(target="<job>")` has the output, and the outputs listed
  in the envelope hold each step's own log.
- `usage`: the project, commit or check name was wrong.
- `environment`: the machine lacks something the checks need; the reason names the fix.

Report a failed check with the step that failed and its decisive output lines. A second run without a change
answers the same.

## Before a push

Each repository's pre-push hook asks the gate: every pushed commit of a registered project must have passed
its gate list. The hook reads records and git only, so it answers at once.

- Run `check` at the commit to be pushed. Passed already means nothing runs.
- A refused push names the commit and what it lacks; `ci.status` says the same.
- Do not push with `--no-verify`, and do not change records to get a push through. If a check cannot pass,
  say so.

The same hooks scrub commits: the staged changes, the message, and the pushed range must carry no attribution
trailers, tooling files, local paths or personal data. A refusal names the line; fix it and commit again.
