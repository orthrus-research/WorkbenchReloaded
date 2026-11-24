# Vocabulary

The words Workbench uses for what it does, one meaning each. 25 concepts replace the 59 the tools grew before
them; the last column says what each replaces. The help of every `wb` verb and option and every module's and
command's summary keep to these words: `python tools/vocabulary.py --check` (and `tests/test_vocabulary.py`, beside
the command tables' check) fails when one uses a word the tables below retire, or a word in a sense it does not
have here.

## Concepts

| Concept | Meaning | Replaces |
|---|---|---|
| Set | The repository of record. `set.json` holds one line per project (branch, commit, its own version, its artifacts by SHA-256), third entries, the platform and holds; `build-logic/` holds the shared Gradle plugins. Only `wb land` writes it. | Pins kept by consumers, lock files in many formats, the chained pin, the sibling folder layout as a version source |
| Set revision and `.set` | A commit of the Set. A worktree's revision is in its untracked `.set` file (a Work's file for its members); every tool reads it, and it moves only by `wb work sync` or a landing from that folder. Behind means it is not the head. | Follow and pinned modes, per-worktree pin copies, hand-kept status tables |
| Project and `project.toml` | A repository, known by its git common dir. Its one manifest, read at the commit in use, holds `[project]`, `[uses]`, `[artifacts]`, `[checks]`, `gate` and `[launch]`. | A main checkout versus a linked worktree as identity, `actions.toml` (workflow, job, step), identity files |
| Use | A `[uses]` entry: a project or third entry the project compiles against, with the artifacts it takes and a floor it owns. | Pins and their flags, build recipes on the consumer side, one project's file names hard-coded in another |
| Artifact | A named output of one producing check (`[artifacts.<name>]`: check, file glob, mod id), always keyed by tree. Consumers ask for `project:artifact` and never see a path. | Default artifacts, versioned JAR names in other projects, artifact flags |
| Third entry | A third-party file in `set.json`: a Maven coordinate or a URL, with SHA-256, licence, file name and mod id. It enters the key of every check that uses it. | Third-party hashes and download lines restated in consumers |
| Platform | The `set.json` section naming the Java major, the exact JDK builds per major with their archives, the Gradle distribution, Cleanroom, CleanroomGradle and the tree id of `build-logic/`. | Platform files, JDK discovery, the Cleanroom version restated per file, exact-JDK gates copied per project |
| Hold | A set entry `<project>[:<artifact>][@<consumer>]` that keeps one artifact, or one consumer's view of a project, on another commit, with a reason, a date and its own hashes. Shown with its age. | Stale pins as the way to lag, `[agree]` and `allow_disagreement`, hashes hard-coded in builds |
| Store | Workbench's byte store on the out root, written only by Workbench: files by SHA-256 under their real names, pass records, the convention's plugins, JDKs and landing records. | The pin cache as an unlabelled bag, refresh and verify tasks, rebuild, stage and freeze commands, pin folders as dependency state |
| Check and gate list | A named unit of verification in `[checks]`: argument lists with `{gradle}` and `{python}`, run in a clean clone in the container, or on the host with `on = "host"`. `gate = [...]` lists the checks a push and a landing need. | The actions job (a homonym of a Workbench job), the workflow, `ci.run` as the daily command, README prose for host checks |
| Key, pass and gate file | A check's key is a digest of its steps, its tree or input files, the hashes it uses, the platform, its tool paths and the runner. A pass is the record under a key; the gate file says a tree passed its whole gate list under one set revision. | Pass keys of sibling trees, the run record as the gate's source |
| Land | The one operation that changes the Set: validate at the head, take the lock `set`, validate again, publish (landing refs, the Set commit, the branches). | Pin-move commit sequences, refresh flags, a hash learned from a refusal |
| Work | A folder `<work>/<branch>/<project>` of worktrees on one branch name with one `.set` file. Members stand in for their set lines in every verb, in hand builds and in the IDE. | Several worktree folder conventions, junction provisioning, hand-kept registers of who works where |
| Stand-in (`--with`) | `name=<ref \| folder \| jar>` on check and play (`-Pset.with.<name>` in plain Gradle), or a Work member: it replaces one line for one command, is printed and recorded, and can never be landed. | Candidate artifact flags, named lab profiles that chose checkouts, checkout and pin-folder flags |
| Convention | The Set's Gradle plugins, `set.base` and `set.cleanroom`, versioned by the tree id of `build-logic/` and resolved by the block every `settings.gradle` begins with. | Per-repository plugin versions, JDK gates and lookups, copied verifiers |
| Launch and launch file | `[launch.<name>]` lists members by `project:artifact@side`, a base, params, overlays and one configure script; a member's `[launch.member]` adds its JVM properties and what to collect. `launch.json` is that launch resolved once, by SHA-256. | Launchers, instance profiles, launch sets, recipe files, a runtime lock as the launch's identity |
| Instance | One prepared template per launch file, with instances hard-linked from it and an `instance.json` receipt by hash. Worlds are kept by world key and writer build. | Instance, recipe, template, store and world snapshot as overlapping words; absolute paths in receipts; the lab home |
| Session | One run of an instance under the GPU lease: open, tour, measure or driver. Its inputs go into `session.json`, never into the template. A kept session (`lab.keep`) is what retention never touches. | Host run ids as the name of a game run, baselines inside disposable instances |
| GPU lease | One OS lock on the machine with one lane file per taker. `wb gpu run` gives any command a turn. | Lease, lane and turn token in three implementations; the lease as a general lock |
| Command, job and envelope | Commands declared in static `module.toml` manifests; one job per execution; one result shape (`wb/result/v1`, exit codes 0 to 6). A job has named locks, a reservation, one recorded wait reason while queued, a failure block and a caller label. | Engine budget, engine lock and run lock; receipt, record, evidence and summary as names of a result |
| Quote and approval | Spending or publishing waits for approval of one exact quote, and only the person's credential approves, never a job's. | Approval under the token every step holds |
| Config layers and places | Machine config over organisation config, each value with a file and line. Each shared folder is named once in its host form; the container's spelling is derived. `[lab] trusted` names the projects whose code may run on the host. | Root and container path pairs, one path in three keys, reload versus restart |
| Release and deploy | An immutable folder of Workbench at one commit that the daemon, the host runner, the steps and the CLI all run. Changed only by `wb deploy`, which drains first and can be overridden with a recorded reason. | Daemon, image and state volume as three things to refresh; a hand restart of the host runner |
| Retention (`wb gc`) | Each folder Workbench makes declares a keep rule. `wb gc` lists and prints a digest; `wb gc --yes <digest>` removes exactly that list; the nightly run removes only classes marked unattended. | Pruners per module and `wb prune` |
| Hooks and scrub | Three stubs per repository that call the released CLI on the host: pre-commit and commit-msg scrub with the machine's rules; pre-push scrubs the range and the ref names, then answers the gate from files. | Push gates installed per project, the scrub as hand-run jobs, the gate as a daemon job |

## Retired words

Help text says what the second column says instead. "Kept in" names the places that still use the old word,
because the thing it names is still there; each goes with the release that removes that thing.

| Retired | Say instead | Kept in |
|---|---|---|
| `pin chain`, `chained pin` | the Set: one line per project | |
| `pin cache` | the Store | |
| `pins` | the projects' lines in the Set, or a project's uses | `wb status` (`--doctor` reports the pins that remain), `ci.run` (clones pinned repositories until the last `actions.toml` is converted) |
| `workflow`, `workflows` | a check, or the gate list | `wb check` (an `actions.toml` still names its workflow) |
| `launch set` | a launch | |
| `allow_disagreement`, `[agree]` | a hold | |
| `lock file`, `lock files` | the project's line in the Set | |
| `follow mode`, `pinned mode` | the folder's `.set` revision | |
| `engine lock`, `run lock`, `engine budget` | a named lock, or the job's reservation | |
| `toolchain.rebuild`, `build.stage`, `build.freeze` | `store.verify` | |
| `wb restart`, `wb prune` | `wb deploy`, `wb gc` | |
| `instance profile`, `named profile`, `lab profile` | a launch | |
| `lab home` | the instance's folder, or the lab's out root | `lab` (until its old route is removed) |
| `actions job` | a check | |

## One meaning each

A word the vocabulary defines is used in that sense only. The second column lists phrases that would use it in
another.

| Word | Not in the sense of | Means here | Kept in |
|---|---|---|---|
| job | `project's jobs`, `build and test jobs`, `required job`, `jobs file` | one execution of a command, with one envelope; a project's units of verification are checks | `wb check` (an `actions.toml`'s jobs, until it is converted) |
| instance | `account's instances`, `destroy one instance`, `rented instance` | a prepared game folder, hard-linked from its launch file's template; a rented computer is a rented machine | |
| store | `world store`, `instance store`, `lab store`, `lab's store` | Workbench's byte store, written only by Workbench | |
