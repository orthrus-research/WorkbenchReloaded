# Verbs for the `cli` tool

`cli(args=[verb, ...], cwd=<folder>)` runs a verb as a terminal would and answers with its exit code and what it
printed. Most verbs take `--json`. Give `cwd` whenever the verb reads the folder it stands in.

## The folder you stand in

| Verb | Does |
|---|---|
| `check [name ...]` | runs the checks of the worktree in `cwd`, its uncommitted changes included; no names: the gate list. `--with project=<ref or folder>` builds against another project's files (no pass is recorded); `--timeout 30m` |
| `work` | this folder: its project, branch, changes, and whether its tree passed. `--all`: every project's worktrees |
| `work start <branch> <project> ...` | a Work: one worktree per project on one branch, cut from each remote's default branch |
| `work add <project>`, `work adopt <path>`, `work sync`, `work finish` | another member; take in a worktree made by hand; move the folder's `.set` to the head; remove members that hold nothing unsaved |
| `land [project=<ref> ...]` | lands HEAD of the worktree in `cwd` (or the named refs) in the Set. `--dry` validates and publishes nothing |
| `store [project] [ref]` | what the Store holds for a project at a commit. `store path <project> <ref> <artifact>` gives a kept file's path |
| `play [project][@ref][:launch]` | resolves a launch, builds its instance and plays a session (see the `play` skill) |

## The machine

| Verb | Does |
|---|---|
| `gpu` | who holds the GPU lease and who waits. `gpu stuck` lists only what needs a person; `gpu leave <lane>` withdraws a lane |
| `status --doctor` | the daemon's health, then what still ties each project to the old routes; exit 1 when it finds anything |
| `config show [--origin]` | the merged config, each value with the layer and line it came from |
| `gc` | lists what the retention rules would remove, with a digest. `gc --yes <digest>` removes exactly that list |
| `setup --report` | scrubs unpushed commits and ref names and changes nothing |
| `backup [--to <folder>]` | copies the job database, the ledger and the scrub rules off this drive |

## Not run from here

`approve` is the person's. `up`, `down`, `restart`, `build`, `deploy`, `host`, `dev` and `hook` change or serve
the service itself and belong to a terminal, as does `gpu run -- <command>`, which gives any command a turn under
the GPU lease. `run`, `job`, `jobs`, `logs` and `cancel` have tools of their own, which answer in fewer words.
