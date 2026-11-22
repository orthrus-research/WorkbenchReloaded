"""runs: orun's run custody, as commands (D18), and the evidence that lets a comparison of runs count."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import statistics
import sys
import tomllib
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError, sha256_tree

from . import evidence


def at_commit(ctx: Context, repo: str, commit: str, steps: list, keep: bool):
    source = Path(repo)
    if not (source / ".git").exists():
        raise ParamError(f"{repo} is not a git checkout")
    tree = ctx.work / "tree"
    if tree.exists():
        shutil.rmtree(tree, ignore_errors=True)
    ctx.progress("clone", msg=f"{commit} from {ctx.host_path(source)}")
    # A shared clone reads the source's objects and writes nothing into it, so a read-only mount works.
    ctx.run(["git", "-c", f"safe.directory={source.as_posix()}", "clone", "--quiet", "--shared", "--no-checkout",
             str(source), str(tree)], check=True)
    resolved = ctx.run(["git", "-C", str(tree), "rev-parse", "--verify", f"{commit}^{{commit}}"], check=False)
    if resolved.returncode != 0:
        raise ParamError(f"no commit {commit} in {repo}")
    sha = resolved.stdout.strip().splitlines()[-1]
    ctx.run(["git", "-C", str(tree), "checkout", "--quiet", "--detach", sha], check=True)
    tree_hash = ctx.run(["git", "-C", str(tree), "rev-parse", f"{sha}^{{tree}}"], check=True).stdout.strip()
    ctx.log.info(f"clean tree at {sha} (tree {tree_hash})")
    results = []
    try:
        for index, step in enumerate(steps, start=1):
            ctx.check_cancel()
            ctx.progress(f"step {index}", msg=step)
            args = shlex.split(step, posix=sys.platform != "win32")
            if sys.platform == "win32":  # non-POSIX splitting keeps the quotes around a token
                args = [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "\"'" else a for a in args]
            done = ctx.run(args, cwd=tree)
            log = ctx.out / f"step-{index}.log"
            log.write_text(done.stdout, encoding="utf-8")
            ctx.output(f"step-{index}", log)
            results.append({"step": step, "exit": done.returncode})
            if done.returncode != 0:
                ctx.finding(f"step {index} ({step}) exited {done.returncode}")
                break
    finally:
        if not keep:
            shutil.rmtree(tree, ignore_errors=True)
    if ctx.outcome == "ok":
        ctx.summary(f"{len(results)} step(s) passed at {sha[:12]}")
    return {"commit": sha, "tree": tree_hash, "steps": results, "kept": ctx.host_path(tree) if keep else None}


def _metric(envelope: dict, metric: str) -> float | None:
    values = [m["value"] for m in envelope.get("measurements", []) if m.get("name") == metric
              and isinstance(m.get("value"), (int, float))]
    if not values:
        data = envelope.get("data") or {}
        value = data.get(metric)
        return float(value) if isinstance(value, (int, float)) else None
    return float(statistics.median(values))


def _run_once(ctx: Context, command: str, params: dict, metric: str, label: str) -> tuple[float, dict]:
    envelope = ctx.call(command, fresh=True, **params)
    if envelope.get("state") != "settled" or envelope.get("outcome") not in ("ok", "finding"):
        raise EnvironmentProblem(f"{label}: {command} ended {envelope.get('state')} "
                                 f"{envelope.get('outcome')}: {envelope.get('summary')}")
    value = _metric(envelope, metric)
    if value is None:
        raise ParamError(f"{command} reported no measurement or data field {metric!r}")
    return value, envelope


# -- evidence -------------------------------------------------------------------------------------
def _client():
    from wb.client import Client
    client = Client(os.environ.get("WB_URL"), os.environ.get("WB_TOKEN"))
    client.path_style = "posix"
    return client


def _envelope(job: str) -> dict:
    from wb.client import WbError
    try:
        return dict(_client().job(job).refresh())
    except WbError as error:
        raise (ParamError if error.status == 404 else EnvironmentProblem)(f"job {job}: {error}") from None


def _local(ctx: Context, name: str, value) -> Path:
    """A host path given inside an object parameter, as this process reads it."""
    from wb_core.paths import PathError, PathMap
    from wb_core.settings import Root
    if not isinstance(value, str) or not value:
        raise ParamError(f"files.{name}: expected a path")
    roots = [Root(**r) for r in ctx.spec.get("roots", [])]
    if not roots:
        return Path(value)
    try:
        return Path(PathMap(roots).resolve(value).container)
    except PathError as error:
        raise ParamError(f"files.{name}: {error}") from None


class _Recorder:
    """Makes the evidence record of each run: its envelope's facts, the facts declared, and the files declared,
    digested and kept (up to ``[modules.runs] keep_mb``) as ``snapshot`` finds them: before a run, for a run started
    here; when the record is made, for a run that finished before."""

    def __init__(self, ctx: Context, fields: dict, files: dict, data: list):
        self.fields, self.data = fields or {}, list(data or [])
        try:  # a declaration that cannot be recorded fails before any run
            evidence.declared_names(self.fields, files)
        except evidence.EvidenceError as error:
            raise ParamError(str(error)) from None
        self.files = {}
        for name, value in (files or {}).items():
            local = _local(ctx, name, value)
            if not local.exists():
                raise ParamError(f"files.{name} does not exist: {value}")
            self.files[name] = (value, local)
        self.keep = float(ctx.config.get("keep_mb", 64)) * 1_000_000
        self.schemas: dict[str, dict | None] = {}

    def snapshot(self, folder: Path) -> dict:
        return {name: self._file(name, host, local, folder) for name, (host, local) in self.files.items()}

    def _file(self, name: str, host: str, local: Path, folder: Path) -> dict:
        if not local.exists():
            raise ParamError(f"files.{name} no longer exists: {host}")
        if local.is_dir() and folder.resolve().is_relative_to(local.resolve()):
            raise ParamError(f"files.{name} holds the record itself ({host}); declare the files in it instead")
        digest, count = sha256_tree(local)
        size = local.stat().st_size if local.is_file() else \
            sum(p.stat().st_size for p in local.rglob("*") if p.is_file())
        info = {"path": host, "sha256": digest, "bytes": size, "files": count, "kept": None}
        if 0 < self.keep and size <= self.keep:
            target = folder / "files" / name
            if target.exists():  # the record of the same run, made again
                shutil.rmtree(target)
            if local.is_file():
                target.mkdir(parents=True)
                copy = target / local.name
                shutil.copy2(local, copy)
            else:
                shutil.copytree(local, target)
                copy = target
            if sha256_tree(copy)[0] != digest:
                raise EnvironmentProblem(f"files.{name} changed while it was copied: {host}")
            info["kept"] = copy.relative_to(folder).as_posix()
        return info

    def _schema(self, envelope: dict) -> dict | None:
        """The params schema the run's command has now, when that is still the version that ran (for defaults)."""
        from wb.client import WbError
        name = envelope.get("command")
        if name not in self.schemas:
            try:
                self.schemas[name] = _client().command(name)
            except WbError as error:
                if error.status != 404:
                    raise EnvironmentProblem(f"command {name}: {error}") from None
                self.schemas[name] = None  # a command no longer known: its params as submitted
        described = self.schemas[name]
        if described and str(described.get("version")) == str(envelope.get("command_version")):
            return described.get("params")
        return None

    def record(self, envelope: dict, folder: Path, files: dict | None = None) -> dict:
        """Writes the run's record into ``folder``; ``files`` is the snapshot taken before the run, if one was."""
        try:
            if files is None:
                evidence.require_measured(envelope)  # before copying anything for it
                files = self.snapshot(folder)
            record = evidence.build(envelope, self._schema(envelope), declared=self.fields, files=files,
                                    data=self.data)
        except evidence.EvidenceError as error:
            raise ParamError(str(error)) from None
        evidence.write(record, folder)
        return record


def _kinds(ctx: Context, path: str | None) -> evidence.Kinds:
    """The field kinds: ``[modules.runs] kinds``, then the kinds file's ``[kinds]`` table over it."""
    try:
        entries = evidence.kind_entries(ctx.config.get("kinds") or {})
    except evidence.EvidenceError as error:
        raise EnvironmentProblem(f"[modules.runs] kinds: {error}") from None
    if path:
        try:
            table = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ParamError(f"kinds: {error}") from None
        unknown = sorted(set(table) - {"kinds"})
        if unknown:
            raise ParamError(f"kinds: unknown keys {unknown}; field kinds go in a [kinds] table")
        try:
            entries += evidence.kind_entries(table.get("kinds") or {})
        except evidence.EvidenceError as error:
            raise ParamError(f"kinds file: {error}") from None
    return evidence.Kinds(entries)


def _records_folder(ctx: Context) -> Path:
    """Where a command keeps the records of the runs it makes; never over another command's records."""
    folder = ctx.out / "evidence"
    if folder.exists() and any(folder.iterdir()):
        raise ParamError(f"{ctx.host_path(folder)} already holds evidence records; give this run its own out folder")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _refuse(ctx: Context, refusals: list[str], lead: str):
    for refusal in refusals:
        ctx.finding(refusal)
    more = f" (and {len(refusals) - 1} more)" if len(refusals) > 1 else ""
    ctx.summary(f"{lead}: {refusals[0]}{more}")


def _matched(report: dict) -> str:
    sides = [label[0] for label in report["runs"]]
    runs = f"{sides.count('A')} A and {sides.count('B')} B run(s)"
    others = sum(d["role"] in ("ignored", "uncontrolled") for d in report["differences"])
    text = (f"{runs} differ only in {', '.join(report['treatment'])}" if report["treatment"]
            else f"{runs} match") + f": {report['controls']} control field(s) agree"
    return text + (f"; {others} ignored or uncontrolled field(s) differ" if others else "")


def _write_json(path: Path, value) -> Path:
    path.write_text(json.dumps(value, indent=1, default=str) + "\n", encoding="utf-8")
    return path


def _read_record(path: Path, where: str) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ParamError(f"{where}: {error}") from None
    try:
        return evidence.check_record(record, where)
    except evidence.EvidenceError as error:
        raise ParamError(str(error)) from None


def record_evidence(ctx: Context, job: str, fields: dict, files: dict, data: list):
    path = ctx.out / evidence.RECORD
    try:
        held = json.loads(path.read_text(encoding="utf-8")).get("job") if path.exists() else None
    except (OSError, ValueError, AttributeError):
        held = "something else"
    if held not in (None, job):
        raise ParamError(f"{ctx.host_path(path)} already holds the record of {held}; give this record its own "
                         "out folder")
    record = _Recorder(ctx, fields, files, data).record(_envelope(job), ctx.out)
    ctx.output("evidence", path, "application/json")
    kept = sum(1 for info in record["files"].values() if info["kept"])
    if kept:
        ctx.output("files", ctx.out / "files")
    ctx.summary(f"{len(record['fields'])} fields of {job} ({record['command']}) recorded, "
                f"{len(record['declared'])} of them declared"
                + (f"; {kept} of {len(record['files'])} declared file(s) kept" if record["files"] else ""))
    return {"record": ctx.host_path(path), "job": job, "fields": record["fields"], "declared": record["declared"],
            "files": record["files"]}


def compare_evidence(ctx: Context, a: list, b: list, a_jobs: list, b_jobs: list, treatment: list, control: list,
                     data: list, kinds=None):
    rules = _kinds(ctx, kinds)
    recorder = _Recorder(ctx, {}, {}, data)
    folder = _records_folder(ctx) if a_jobs or b_jobs else None
    sides: dict[str, list] = {}
    for side, paths, jobs in (("A", a, a_jobs), ("B", b, b_jobs)):
        records = []
        for value in paths:
            found = evidence.find(Path(value))
            if not found:
                raise ParamError(f"{ctx.host_path(value)} "
                                 f"{'holds no evidence record' if Path(value).exists() else 'does not exist'}")
            records += [_read_record(path, ctx.host_path(path)) for path in found]
        for job in jobs:  # recorded from its envelope, and kept with the comparison
            records.append(recorder.record(_envelope(job), folder / side / str(len(records) + 1)))
        if not records:
            raise ParamError(f"side {side} has no runs: give {side.lower()} (evidence records) or "
                             f"{side.lower()}_jobs (job ids)")
        sides[side] = records
    report = evidence.compare(sides["A"], sides["B"], treatment, control, rules)
    ctx.output("comparison", _write_json(ctx.out / "comparison.json", report), "application/json")
    if folder is not None:
        ctx.output("evidence", folder)
    if report["refused"]:
        _refuse(ctx, report["refusals"], "refused")
    else:
        ctx.summary(_matched(report))
    return report


def repeat(ctx: Context, command: str, params: dict, times: int, warmup: int, metric: str, fields: dict,
           files: dict, data: list):
    recorder = _Recorder(ctx, fields, files, data)
    folder = _records_folder(ctx)
    for index in range(warmup):
        ctx.progress("warm-up", index / max(1, warmup))
        _run_once(ctx, command, params, metric, f"warm-up {index + 1}")
    values, jobs = [], []
    for index in range(times):
        ctx.progress("measure", index / times, f"run {index + 1} of {times}")
        run = folder / str(index + 1)
        kept = recorder.snapshot(run)  # the files as this run starts from them
        value, envelope = _run_once(ctx, command, params, metric, f"run {index + 1}")
        recorder.record(envelope, run, kept)
        values.append(value)
        jobs.append(envelope["job"])
        ctx.measure(metric, value, run=index + 1)
    ctx.output("evidence", folder)
    median = statistics.median(values)
    spread = (max(values) - min(values)) / median * 100 if median else None
    ctx.summary(f"{metric}: median {median:g} over {times} runs, spread {spread:.1f}%" if spread is not None
                else f"{metric}: median {median:g}")
    return {"values": values, "median": median, "min": min(values), "max": max(values),
            "stdev": statistics.stdev(values) if len(values) > 1 else 0.0, "spread_percent": spread, "jobs": jobs,
            "evidence": {"records": ctx.host_path(folder)}}


def ab(ctx: Context, command: str, a: dict, b: dict, pairs: int, metric: str, treatment: list, control: list,
       fields: dict, files: dict, data: list, precondition: bool, fail_above=None, kinds=None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "src"))
    from orthrus_tools.stats import paired_interval
    rules = _kinds(ctx, kinds) if treatment else None  # a kinds file that cannot be read fails before any run
    checking = bool(treatment) and precondition
    recorder = _Recorder(ctx, fields, files, data)
    folder = _records_folder(ctx)
    records: dict[str, list] = {"A": [], "B": []}
    measured, jobs, report = [], [], None
    for index in range(pairs):
        ctx.progress("pairs", index / pairs, f"pair {index + 1} of {pairs}")
        first, second = ((a, "A"), (b, "B")) if index % 2 == 0 else ((b, "B"), (a, "A"))  # ABBA against drift
        results = {}
        for params, label in (first, second):
            run = folder / label / str(index + 1)
            kept = recorder.snapshot(run)  # the files as this run starts from them
            value, envelope = _run_once(ctx, command, params, metric, f"pair {index + 1} {label}")
            records[label].append(recorder.record(envelope, run, kept))
            results[label] = value
            jobs.append({"pair": index + 1, "side": label, "job": envelope["job"], "value": value})
        measured.append((results["A"], results["B"]))
        if checking:
            report = evidence.compare(records["A"], records["B"], treatment, control, rules)
            if report["refused"]:
                break  # a difference found stays found: more runs cannot take it back
    if treatment and report is None:
        report = evidence.compare(records["A"], records["B"], treatment, control, rules)
    if report is not None:
        _write_json(folder / "comparison.json", report)
    ctx.output("evidence", folder)
    proof = {"records": ctx.host_path(folder), "precondition": checking, **(report or {})}
    if checking and report["refused"]:
        when = f"stopped after pair {len(measured)} of {pairs}" if len(measured) < pairs else f"after {pairs} pairs"
        _refuse(ctx, report["refusals"], f"no verdict on {metric}, {when}")
        return {"median_percent_change": None, "interval": None, "pairs": measured, "jobs": jobs,
                "enough_for_review": None, "scope": None, "evidence": proof}
    try:
        stats = paired_interval(measured)
    except ValueError as error:
        raise ParamError(f"cannot bootstrap {metric}: {error}") from None
    change = stats["medianPercentChange"]
    interval = stats["bootstrap95PercentInterval"]
    ctx.measure("median_percent_change", change, unit="%")
    text = f"B vs A: {change:+.2f}% median change in {metric} (95% interval {interval[0]:+.2f}% to {interval[1]:+.2f}%)"
    if report is not None and report["refused"]:  # declared with the precondition off: said, not enforced
        text += f"; the evidence would refuse it: {report['refusals'][0]}"
    elif report is not None:
        text += f"; the runs differ only in {', '.join(report['treatment'])}"
    if fail_above is not None and change > fail_above:
        ctx.finding(text + f", above the {fail_above}% gate")
    else:
        ctx.summary(text)
    return {"median_percent_change": change, "interval": interval, "pairs": measured, "jobs": jobs,
            "enough_for_review": stats["enoughRepetitionsForReview"], "scope": stats["scope"], "evidence": proof}


def interleaved(ctx: Context, samples: str, baseline: str, treatments: list, metrics: list, window: int,
                index: str, condition: str, min_share: float, seed: int):
    """Treatments interleaved within one session (each sample's condition from a hash of its index), paired window
    by window: wb_runs.interleaved."""
    from . import interleaved as paired
    path = Path(samples)
    if not path.is_file():
        raise ParamError(f"samples: no such file: {samples}")
    rows = paired.read_samples(path)
    missing = [k for k in (index, condition, *metrics) if rows and k not in rows[0]]
    if not rows or missing:
        raise ParamError(f"samples: {'no rows' if not rows else 'no column ' + ', '.join(missing)}")
    report = paired.analyse(rows, index=index, condition=condition, metrics=list(metrics), baseline=baseline,
                            treatments=list(treatments), size=window, min_share=min_share, seed=seed)
    _write_json(ctx.out / "interleaved.json", report)
    ctx.output("interleaved", ctx.out / "interleaved.json", "application/json")
    if report["unbalanced"]:
        ctx.finding(f"{len(report['unbalanced'])} window(s) hold less than {min_share:.0%} of a condition and are "
                    "left out: the assignment is not the hashed one, or the windows are too short")
    lines = []
    for metric, results in report["metrics"].items():
        for treatment, result in results.items():
            ctx.measure(f"{metric}_difference", result["medianDifference"], treatment=treatment)
            low, high = result["interval95"]
            lines.append(f"{metric} {treatment} minus {baseline}: {result['medianDifference']:+.4g} "
                         f"(95% {low:+.4g} to {high:+.4g}), lower in {result['treatmentLower']} of "
                         f"{result['windows']} windows")
    if not lines:
        raise ParamError("no window holds the baseline and a treatment: check the condition labels")
    if ctx.outcome == "ok":
        ctx.summary("; ".join(lines))
    else:
        ctx.log.info("; ".join(lines))
    return {"metrics": report["metrics"], "windows": len(report["windows"]), "unbalanced": len(report["unbalanced"])}
