# The envelope (`wb/result/v1`)

Every job answers with these fields, whatever its command. `detail="full"` on `run` or `job` returns all of
them as JSON.

## Identity

| Field | Holds |
|---|---|
| `job` | the job's id, `j_...` |
| `command`, `module` | what ran, with `command_version` and `module_version` |
| `params`, `params_hash` | the parameters as taken, and their digest |
| `caller` | the label the submitter gave itself; jobs from this server carry `mcp:<client>` |
| `parent`, `children` | the job that submitted this one, and the jobs it submitted |

## Progress

| Field | Holds |
|---|---|
| `state` | `queued`, `running`, `awaiting_approval`, `settled`, `cancelled` or `interrupted` |
| `phase` | `quoting` while a spending command's quote is being made |
| `waiting` | why a queued job has not started (`reason`, `since`): the budget, `behind <job>`, the GPU lease, `lock <name> held by <job>`, a quiet machine, or approval |
| `locks` | the named locks the submit asked for beside the command's own |
| `timings` | `submitted`, `started`, `settled`, and seconds per phase in `phases` |

## Result (a settled job)

| Field | Holds |
|---|---|
| `outcome`, `code` | `ok` 0, `finding` 1, `usage` 2, `environment` 3 |
| `summary` | one line that says what happened |
| `reasons` | why it is not `ok`: each with `kind`, `name`, `detail` and a `fix` |
| `data` | the command's own result, checked against the schema `describe` shows under `data` |
| `findings` | what a `finding` found, item by item, where the command lists them |
| `measurements` | named values with units |
| `inputs`, `outputs` | files read and written: `name`, `host_path`, `sha256`; an output also has `bytes` and `media_type` |
| `cost` | `amount` and `currency`, for a command that spent |
| `cache` | `hit` true when the answer came from an earlier job with the same inputs (`from`) |
| `approval` | for a spending command: the `quote`, the ready-made `prompt`, the `status`, and who approved |
| `machine`, `toolchain` | where it ran and with which Workbench commit |
| `logs` | where the job's log lines are; the `logs` tool reads them |

## Exit codes

A verb run through `cli` exits with the envelope's code: `0` ok, `1` finding, `2` usage, `3` environment,
`4` cancelled or interrupted, `5` still pending, `6` awaiting approval.
