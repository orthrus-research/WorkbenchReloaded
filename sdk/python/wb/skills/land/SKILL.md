---
name: land
description: Work in Workbench worktrees and land commits in the Set - start a Work, see what a folder builds against, sync its set revision, validate a landing and publish it. Use it when asked to start work on a branch, to land or merge a project's commits, or when a verb says a folder is behind.
---

# Works and landing

The Set is the repository of record: `set.json` holds one line per project (branch, commit, its artifacts by
SHA-256), with third entries, the platform and holds. Projects build against a set revision, never against
each other's folders. Only `wb land` writes the Set.

All of these are verbs: `cli(args=[...], cwd="<the worktree>")`.

## Where a folder stands

`["work"]` answers for the folder: its project, worktree, branch, changes, and whether its tree passed its
checks. `["work", "--all"]` lists every registered project's worktrees.

Each worktree has an untracked `.set` file: the set revision it resolves against. Every verb says first when
that revision is not the head:

```
set 3f2a91c, 2 behind (cubature, orthlib moved): wb work sync
```

`["work", "sync"]` moves the folder to the head. Nobody else's landing moves it, so a folder that is behind
still builds what it built before.

## Starting and finishing work

| Args | Does |
|---|---|
| `["work", "start", "<branch>", "<project>", ...]` | a Work: `<work root>/<branch as a folder>/<project>`, one worktree per project, each on `<branch>`, cut from its remote's default branch |
| `["work", "add", "<project>"]` | another member in the Work of `cwd` |
| `["work", "adopt", "<path>"]` | moves a worktree made by hand into a Work |
| `["work", "finish"]` | removes the Work's members that hold nothing unsaved |

Branches are `<type>/<topic>`: `feat/...`, `fix/...`, `docs/...`. Members of one Work stand in for their set
lines in every verb, so a change across two projects is checked together before either lands. Make worktrees
with these verbs and not by hand: nothing here moves a main checkout or touches another worktree's files.

## Landing

Landing publishes: it pushes to remotes and moves set-named branches. Land only when the person asked for it.

1. Commit the work, and run its checks (`checks` skill).
2. Validate: `["land", "--dry"]` runs every step up to the publish and publishes nothing. It names what the
   landing would reach: every consumer whose inputs change runs its gate list against the would-be set.
3. Land: `["land"]` for HEAD of the worktree (or of every member of its Work), or `["land",
   "<project>=<ref>", ...]` for named refs. Give `cli` a long `timeout`: gate checks run first, and a pass
   already recorded stands.

A commit that does not descend from its project's line is refused with the git command that fixes it. A failure
before the Set moves leaves every branch as it was. A failure after it leaves "landed, branch behind", which
the next `land` or `work sync` finishes from its record: run that, and do not push the branch by hand.

Other forms, each for a request that names it: `--add <project>=<url>` (a project's first line), `--tag
<name>`, `--only <project>`, `--hold <project>:<artifact>[@<consumer>]` with `--file`, `--commit`, `--reason`
and `--source` (keep one artifact on another commit), `--drop-hold <name>`, `--third <name>` (a third-party
file).

## What CI built

`["store"]` lists what the Store holds for the project of `cwd` at HEAD; `["store", "<project>", "<ref>"]` for
another. `["store", "path", "<project>", "<ref>", "<artifact>"]` gives a kept file's path, hashed again
first. Take built files from there; consumers never read another project's build folder.
