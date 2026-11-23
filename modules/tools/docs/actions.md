# actions

Run a project's build and test jobs locally, like a small CI, and gate pushes on them. Use it
when a project needs its checks run before a commit or push, within the machine's cores and
memory.

```sh
orthrus-tools actions list
orthrus-tools actions run ci [--if-needed] [--keep-going] [--jobs N] [--dry-run] [--verbose]
orthrus-tools actions run quick
orthrus-tools actions status [WORKFLOW]
orthrus-tools actions report [RUN]
orthrus-tools actions gate install [--repository NAME] [--force]
```

Every subcommand takes `--root DIR` and `--config FILE`, and `--json`.

## Configuration

Jobs live in `actions.toml` at the project root. To keep it elsewhere, name it in
`orthrus.toml`:

```toml
[actions]
config = "tools/actions.toml"
```

A small example:

```toml
[settings]
name = "myproject"        # the project's name in run records (default: project)
workflow = "ci"           # what `quick` narrows and the push gate requires (default: ci)
# runs = ".local/actions" # where runs are written
# share = 0.25            # default core share a job claims
# memory = 4              # default GiB a job claims
# memory_fraction = 0.75  # how much of the machine's memory jobs may claim together
identity_files = ["{env.JAVA_HOME}/release"]   # also part of every cache key
identity_env = ["JAVA_TOOL_OPTIONS"]

[repositories.library]    # another checkout the project builds against
path = "../library"
env = "LIBRARY_HOME"      # overrides path when set
pin = { file = "library.lock.json", key = "commit" }   # optional, see the push gate

[repositories.base]       # what the library pins, at the library's pin: named once, by the library
path = "../base"
pin = { repository = "library", file = "platform.json", key = "base.commit" }

[repositories.upstream]   # a checkout jobs only build pinned commits from
path = "../upstream"
tested = false            # its tree is not recorded, matched or gated (never with a pin)

[interpreters]            # the first that exists wins, relative to the root
lint_python = [".local/lint-venv/Scripts/python.exe", ".local/lint-venv/bin/python"]

[placeholders]
frozen = "{run_dir}/frozen"

[workflows]
ci = ["lint", "build", "golden"]

[jobs.lint]
description = "Formatting, lint and types"
steps = [{ name = "ruff", run = ["{lint_python}", "-m", "ruff", "check", "."] }]

[jobs.build]
description = "Build and test"
locks = ["gradle"]        # no two running jobs hold the same lock
memory = 6
artifacts = ["build/libs/*.jar"]   # kept with the run once the steps pass (see Run records)
steps = [
  { name = "gradle", run = ["./gradlew", "build", "--no-daemon"] },
  { name = "library", run = ["./gradlew", "build"], cwd = "{library}" },
]

[jobs.golden]
needs = ["build"]
share = 0.5
priority = 10             # higher starts first when several are ready
inputs = ["src/main/**/*.java", "golden/*.json"]   # a pure check: cached by these files
steps = [{ name = "digests", run = ["{python}", "tools/golden.py", "--out", "{frozen}"], env = { SEED = "1" } }]

[[quick.myproject]]       # changed paths -> jobs, first rule that matches wins
paths = ["docs/**", "*.md"]
jobs = []

[[quick.myproject]]
paths = ["tools/**"]
jobs = ["lint"]
```

A step's `run` is a list of arguments, never a shell string. Placeholders in braces are
replaced in `run`, `cwd`, `env`, `inputs`, `[placeholders]` and `identity_files`:

| Placeholder | Value |
| --- | --- |
| `{python}` | the interpreter running orthrus-tools |
| `{run_id}`, `{run_dir}` | the run's id and its folder |
| `{root}` and `{<name>}` | the project root; each repository by name, the project included |
| `{<interpreter>}` | each `[interpreters]` entry |
| `{env.NAME}` | the environment variable NAME, or empty |
| `{<placeholder>}` | each `[placeholders]` entry, which may use the ones above |

Write a literal brace as `{{` or `}}`.

## Running

Jobs run concurrently once the jobs they `needs` have passed. A job claims `share` of the cores,
`memory` GiB and its `locks`; jobs start while the running claims fit the machine. An
`exclusive = true` job runs alone. `--jobs 1` runs one at a time. After a failure no new job
starts (`--keep-going` continues), and a job whose need failed is skipped.

Each step's output goes to `.local/actions/<run>/<job>/<NN-step>.log`, and the failing step's
last lines are printed. A step may print lines `timing {json}`; the summary keeps them.

A job with `inputs` is a pure check. Its cache key is the SHA-256 of its steps, the files the
globs match (path and content) and the identity. When the key matches an earlier pass, that
pass stands and the job does not run (`--no-cache` runs it). A key that changed while the job
ran is not cached. List every file the job reads, or the cache will vouch for stale results.

`quick` stands for the jobs of the workflow that the changes since HEAD can affect: each
repository's working tree against its own HEAD, matched against `[[quick.<name>]]` rules
(`**` crosses folders, `*` does not). A path no rule matches takes every job.

## Run records

`summary.json` (`orthrus.actions.summary.v1`) holds `runId`, `targets`, `order`, `trees`,
`treesChanged`, `seconds` and `jobs`, where each job has `status` (`passed`, `failed`,
`skipped`), `steps` (`name`, `exitCode`, `seconds`, `log`, `timing`), `begun` and `ended` in
seconds into the run, `cached` or `inputKey` for pure checks, and `artifacts` for a job that
keeps them. `--json` returns it as `result`.

A job's `artifacts` are glob patterns (relative to the root, or absolute; `**` crosses folders)
for the files it builds. Once its steps pass, each match is copied to
`<run>/<job>/artifacts/<file name>`, and the summary records it by name with its `sha256`,
`bytes` and where it came `from`, so the run that vouches for a build also holds it. A pattern
that matches nothing, or two matches of one name, fails the job. A job that keeps artifacts
builds them every run, so it cannot also list `inputs`.

`trees` are the trees each repository's working tree would commit, computed through a scratch
copy of the index, so a run before a commit vouches for that commit. A run during which a tree
changed (`treesChanged`) vouches for nothing. The runs folder never counts as a change.
`run --if-needed` skips jobs that already passed for the current trees, and `status` exits 1
until every job of the workflow has.

`report` writes `timing.md` beside a run's summary: jobs, steps, and the timing records.

## The push gate

`gate install` writes a `pre-push` hook into the project and each repository (only those named
with `--repository`). It replaces another hook only with `--force`. The hook refuses a push
whose pushed commit's tree has not passed every job of the workflow in one run combination:

- a project commit's tree must have passed together with the tree of each repository commit it
  pins, read from `pin.file` at that commit as the JSON key `key`, or a dotted path to a nested
  one (`orthlib.commit`). A pin with `repository` is read from that repository's `pin.file` at
  the commit its own pin names (a pinned repository declared above it), so a dependency two
  projects share is pinned in one place: the project pins the library, the library pins the base.
  Repositories without a `pin` may be any tree tested with it;
- a repository commit's tree must have passed with any project tree.

To push: check out the commits with nothing uncommitted, run
`orthrus-tools actions run ci --if-needed`, and push again. `git push --no-verify` skips the
gate. Deleted refs are not checked.
