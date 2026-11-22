# Bundle templates

A rental runs a **bundle**: a `bundle.toml` saying what to upload, what the machine needs, which script to run and
what to fetch back (see `wb_vast/bundle.py`). Workbench builds no project's bundle in. A **template** in config says
how one is made, and three commands use it:

| Command | Does |
|---|---|
| `vast.lab-inputs` | builds the template's input archives from pinned commits, each with a receipt |
| `vast.bundle` | makes a bundle: the template's folder, a plan's game steps written as the template declares, the input archives pinned; checked, with a manifest of every file's SHA-256 |
| `vast.rent` | rents an offer and runs a bundle on it (after an explicit yes on its quote) |

`plan.run machine=rental` calls `vast.bundle` and then `vast.rent` itself. `vast.bundle-riven`, the former name of
`vast.bundle`, still resolves to it.

## Where a template lives

A template is `[modules.vast.templates.<name>]`. In a config folder the file `modules/vast/templates/<name>.toml`
fills it, so a shared config layer can carry a team's templates. A template's `folder` and its recipes' `repo` are
host paths: absolute, or relative to the machine's `[modules.vast] root` (the way a shared config names them).

`docs/example/` beside this file is a worked example: `tours.toml` (the template) and `tours/` (its folder, holding
`run.sh`).

## Keys

| Key | Default | What it is |
|---|---|---|
| `folder` | (required) | the folder of the template's files; all of them go into the bundle |
| `script` | `run.sh` | the entry script in the folder; the rental runs `bash /root/<its name> <run.args>` |
| `exclude` | `[]` | globs of folder files to leave out |
| `summary` | `""` | what the template runs |
| `purpose` | `measurement` | recorded with the rental |
| `needs` | `{}` | `jdk` (majors the machine gets; none: 26), `gpu` (VirtualGL on the GPU), `disk_gb` (32) |
| `run` | | how `vast.rent` watches the run: `args` (`["/root/bundle"]`), `progress` (a regular expression for lines worth showing, `^RUN `), `done` (the line that ends the run, `RUN done`), `failed` (lines that make it partial), `results` (what is fetched, `["/root/bundle/results.tar", "/root/run.log"]`), `stop` (what stops it when the cap comes first; default: the script), `partial` (how to pack what there is then), `unpack` (fetched archives a plan unpacks) |
| `inputs` | `{}` | the input archives, by file name (below) |
| `steps` | none | how a plan's game steps are written into the bundle (below) |
| `outputs` | `{}` | which fetched results become step outputs (below) |

Globs are over POSIX paths relative to their folder: `**` crosses folders, `*` and `?` stay within one, and a glob
that names a folder takes everything under it.

## Inputs

```toml
[inputs."app.tar"]
sha256 = "..."        # optional pin: other bytes are refused, by vast.lab-inputs and by vast.bundle
to = "/root/bundle/"  # where the rental puts it (the default)
repo = "App"          # a recipe: the checkout (only ever read) ...
commit = "9c1d4e7b..."  # ... the pinned commit (7 to 64 hex digits) ...
steps = ["{python} tools/build.py --release"]   # ... commands run in order in a clean tree of it ...
artifacts = ["tools", "build/libs/*.jar"]       # ... and the files the archive holds
base = ""             # the folder of the tree those paths are relative to (default: the top)
exclude = []          # globs to leave out
jdk = "26"            # JAVA_HOME for the steps (every JDK is JAVA_HOME_<major>_X64)
env = {}              # more environment for the steps
```

An input with none of the recipe keys is made by hand and only checked (its pin, when it has one). A recipe needs
`repo`, `commit` and `artifacts`, and makes a `.tar`. `vast.lab-inputs template=<name> --out <folder>` builds
every input with a recipe (or those named in `inputs=`): a shared clone of the repository at the commit (the
checkout is never written, as with `runs.at-commit` and `toolchain.rebuild`), the steps (`{python}` is Workbench's
interpreter, `{tree}` the clean tree; each gets `WB_PIN_CACHE`), then a tar of the matched files whose bytes depend
only on their paths, modes and contents and on the commit: sorted entries, no folder entries, the commit's time,
owner 0, and the mode git records (a file the steps made is 0755 when executable, else 0644). Links are refused.
`cached=false` builds again to check an archive still reproduces; `keep=true` keeps the trees.

Beside each archive go `<name>.sha256` and the receipt `<name>.receipt.json` (`wb.lab-input.v1`): the recipe and
its digest, the full commit and its tree, each step with its exit code, every file's size, mode and SHA-256, the
archive's SHA-256, and the pin it was checked against. An archive that is not its pin is not kept. A step that
fails, a recipe whose globs match nothing, and a commit the checkout lacks are findings.

**The pin cache.** `toolchain.rebuild` and `ci` share a folder of files named by their SHA-256: `WB_PIN_CACHE`, else
`[modules.toolchain] pin_cache`, else `<state>/cache/pins`. `vast.lab-inputs` reads it first and fills it after: an
archive it holds under the input's pin, or under the digest an earlier build of the same recipe made (receipts are
kept by recipe in `<state>/vast/lab-inputs`), is taken from it instead of being built again.

## Game steps

A plan's game steps are its steps whose command declares a `role` in its `module.toml` (`prepare`, `play`). The
`[steps]` table says how the template takes them:

```toml
[steps]
file = "plan.env"          # where they are written in the bundle
format = "env"             # env (shell variables; the default) or json (the default for a .json file)
roles = { prepare = 1, play = "1+" }   # how many of each it takes: 1, "1+", "1-4"; other roles are refused
same = ["name"]            # parameters every game step must give, alike
files = [{ role = "prepare", param = "args", match = "*.jar", into = "jars", write = "remote" },
         { role = "play", param = "tour" }]
```

A **file rule** carries the files a parameter names into the bundle: each value (or list item) whose file name
matches `match` (default `*`) must be a file; it is copied under `into` (default: the bundle's top) and written as
its path in the bundle (`write = "relative"`, the default) or on the rental (`"remote"`, under `/root/bundle`).
Values that do not match are written as they are.

An **env** file is written from `[steps.values]`, one variable each, quoted for the shell:

```toml
[steps.values]
NAME = "{prepare.name}"
TOURS = "{play.tour}"                 # a role's steps joined with spaces
LABELS = "{play.label|play.id}"
"OPTIONS_{n}" = "{play.options|}"     # {n} in a name: one variable per step of the role its value reads
WINDOW = "{values.window|1920x1080}"
```

A **json** file holds the steps themselves: `{"schema": "wb.bundle-steps.v1", "bundle", "template", "values",
"steps": [{"id", "command", "role", "n", "params"}]}`, with the file rules applied.

**Fields.** `{<role>.<param>}` is a game step's parameter (`id` and `command` name the step itself),
`{values.<key>}` a value the bundle was given (`plan.run` gives its `[rental]` table less `offer`, `max_dph`, `cap`,
`template` and `lab`), `{name}` the bundle's name and `{n}` a step's place among its role's steps, from 1.
`{a|b|c}` takes the first alternative with a value; an alternative that is no reference is text (`{prepare.seed|0}`;
`{play.options|}` is empty), and text in a field has no dot. `{{` and `}}` are braces. A field reads one role. A value
nothing gives is a refusal naming the step.

## Outputs

```toml
[outputs]
"{play.label|play.id}-captures" = "results/{play.label|play.id}-captures"
```

Each entry is written once per step of the role it reads (or once, reading none): an output name and its path in
the rental's out folder after the fetched `run.unpack` archives are unpacked there. `plan.run` registers the paths
that exist as outputs of the rental, so a later step reads one as `{steps.<id>.outputs.<name>}`, as on the host.

## The bundle

`vast.bundle template=<name> steps=[...] lab=<folder> --out <folder>` (or `plan=<folder>` already holding the steps
file and the files it names, as the former name took it) writes:

- `files/`: the template's folder, the steps file and the files the steps name; its children go to `/root/bundle`;
- `bundle.toml`: the input archives by path, each pinned by its SHA-256, then `files/`, the needs and the run;
- `manifest.json` (`wb.bundle.v1`): the SHA-256 of `bundle.toml` and of every file under `files/` (and of the set),
  each input with its pin and its receipt's commit and tree, the steps it carries, the outputs and what to unpack.

Before writing, every reason to refuse is a finding and nothing is written: a step of a role the template does not
take or a count it does not take, a parameter in `same` that differs, a named file that is missing, a field nothing
gives, an input missing from `lab`, not its pin or not its receipt's archive, two different files for one path.
`dry_run=true` checks all of it, inputs by their receipts and `.sha256` files, and writes nothing; `plan.check` does
this for a rental plan. Loading a bundle (`vast.bundle-check`, `vast.rent`) refuses one whose files changed or
gained a file since its manifest was written, and an input that no longer matches its pin.

## Plans on a rental

```toml
[rental]
offer = "29811734"
max_dph = 0.10
cap = "90m"
template = "tours"            # default: the only template configured
lab = '/srv/lab-inputs/tours' # the input archives, as vast.lab-inputs made them
window = "1280x720"           # any other key is {values.<key>} for the steps file
```

The game steps run first, together, in one bundle written before any step runs, so their parameters read plan
inputs, never a step's results; later host steps read the game steps' outputs.
