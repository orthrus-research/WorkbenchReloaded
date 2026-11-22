# The command contract

Every orthrus tool behaves the same way on the command line, so a script learns it once.
In Workbench each tool is a `tools.<name>` command (`wb help tools.pins`); this page describes
what every one of them returns.

## Calling a tool

```sh
orthrus-tools <tool> [arguments]        # such as: orthrus-tools pins verify
orthrus-<tool> [arguments]              # the standalone alias, such as: orthrus-pins verify
python -m orthrus_tools <tool> [arguments]
```

All three run the same code. `orthrus-tools --help` lists the tools; `<tool> --help` lists a
tool's arguments.

## Exit codes

| Code | Status | Meaning |
| --- | --- | --- |
| 0 | `ok` | the check passed, or the command did what it was asked |
| 1 | `finding` | the command ran and found a problem: a failed comparison, a missing pin, a scrub finding |
| 2 | `usage` | bad arguments, or the project is not set up for the tool (no lock file, no baseline) |
| 3 | `environment` | the machine lacks something: a JDK, Git, numpy, the network, a process backend |

A script can branch on the code alone: 1 means look at the result, 2 means fix the call or the
project, 3 means fix the machine.

## JSON output

`--json`, before or after a subcommand, makes a tool print exactly one JSON object on stdout and
nothing else there, whether it succeeds or fails:

```json
{
  "schema": "orthrus.result.v1",
  "tool": "pins",
  "command": "verify",
  "exitCode": 1,
  "status": "finding",
  "result": {"pins": [{"name": "a.jar", "state": "missing"}]},
  "messages": []
}
```

| Field | Holds |
| --- | --- |
| `schema` | always `orthrus.result.v1` |
| `tool`, `command` | what ran; `command` is `null` for a tool without subcommands |
| `exitCode`, `status` | the exit code and its name from the table above |
| `result` | the command's data, or `null` when it stopped before producing any; a report with its own schema names it in `result.schema` |
| `messages` | notes, warnings and errors, each `{"level": "info" \| "warn" \| "error", "text": ...}` |

Without `--json` the same data prints as short text lines, with warnings and errors on stderr.

## Stability

A schema name (`orthrus.result.v1`, `orthrus.catalog.v1`, `orthrus.java-workload-guard.v1`)
changes only when a field is removed or changes meaning. New fields can appear in any release,
so read the fields you need and ignore the rest.
