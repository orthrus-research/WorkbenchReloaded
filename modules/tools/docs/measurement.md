# Measurement

Three building blocks for timing and memory measurements of native runs on a workstation:
a guard that excludes runs overlapping other Java work, an observer that reads an owned
process's memory at two endpoints, and run-level statistics. All are read-only: none of them
stops, suspends, signals or reprioritizes any process.

## Java workload guard

`orthrus_tools.workload_guard.JavaWorkloadGuard` polls the machine's Java processes every five
seconds while a run is live. The run is excluded when:

| Issue | Meaning |
| --- | --- |
| `foreign-java` | a Java process the run did not start overlapped it |
| `active-resident-daemon` | a daemon that was idle before the run (by default a Gradle daemon) used more than 10% of one core over an interval |
| `observation-failed`, `observer-unavailable` | the machine could not be read; an unreadable machine is not an idle one |
| `owned-registration-failed` | the run's own process could not be bound to its identity |

```python
guard = JavaWorkloadGuard()
guard.start()  # refuses when the machine is not clean now
with guard.launch():  # no poll can run between Popen and register
    process = subprocess.Popen(command)
    guard.register(process)
...
guard.close()
passed = guard.report()["passed"]
```

Processes are keyed by PID and creation identity, so a reused PID is never mistaken for an
owned or resident process. `orthrus-tools workload-guard` prints the Java processes running now
and exits 1 when any is not a known resident daemon, or 3 when the machine cannot be read: a
quick check before starting a run.

Limits: a foreign process shorter than one interval can be missed; non-Java work and GPU
contention are not excluded.

## Process lifecycle

`orthrus_tools.process_lifecycle.ProcessLifecycle` binds to an owned `Popen` right after launch
and reads its memory only at `ready` (when a given marker appears in its log) and
`endBeforeQuit`. Each endpoint is read once; a failure is recorded with its reason and no
memory value is ever invented.

| Field | Windows | Linux |
| --- | --- | --- |
| `workingSetBytes` | working set | `VmRSS` |
| `peakWorkingSetBytes` | peak working set | `VmHWM` |
| `privateCommitBytes` | private usage (commit) | private resident plus swapped, from `smaps_rollup` |
| `peakPrivateCommitBytes` | peak pagefile usage | `None`: Linux keeps no such peak |

## Statistics

`orthrus_tools.stats.paired_interval(pairs)` takes matched `(baseline, candidate)` costs, one
pair per independent run, and returns the median paired percent change with a 95% percentile
bootstrap interval. Ticks or frames inside one run are correlated: never pool them as if they
were independent runs. Five pairs is the minimum for review. `window_quality(count, dropped)`
says whether a sampling window holds enough observations to talk about its p99 tail.

```sh
orthrus-tools stats pairs.json          # [[before, after], ...]: the median and its interval
orthrus-tools stats pairs.json --json   # the full result
```

## Platforms

Windows uses documented Win32 calls through `ctypes` with query-only access rights. Linux reads
`/proc` and holds a pidfd where the kernel offers one. Other platforms report the observer as
unavailable, which excludes the run.
