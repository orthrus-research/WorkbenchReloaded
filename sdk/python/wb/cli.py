"""``wb``: the terse front end. Workbench's own options are flags; a command's parameters are key=value.

    wb up | down | restart | status | build
    wb ls [module]                       commands, active or inactive, with reasons
    wb help vast.rent                    parameters, requirements, result schema
    wb run frames.compare ref=a/ cand=b/ profile=near [--out DIR] [--json] [--detach] [--fresh]
    wb wait JOB [--timeout 10m] | wb show JOB | wb jobs [--state S] | wb logs [MODULE|JOB] [-n N]
    wb approve JOB --quote Q [--via relayed] | wb cancel JOB
    wb config show [--origin] | wb schema
    wb gc [--only CLASS] | wb gc --yes DIGEST | wb gc --rules | wb gc --nightly
    wb host serve | install | status
    wb gpu [run [--note T] [--limit D] [--detach] [--quiet] -- CMD | leave LANE | stuck]
    wb play [PROJECT][@REF|@set][:LAUNCH] [--from F] [--name N] [--tour F] [--measure] [--debug S] ...
    wb deploy [--commit SHA] [--wait 10m | --now] [--override REASON]

Exit codes: 0 ok, 1 finding, 2 usage, 3 environment, 4 cancelled or interrupted, 5 still pending,
6 awaiting approval (the quote and a ready-made prompt are printed; with --json, as JSON).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import compose
from . import config as wbconfig
from .client import EXIT_AWAITING_APPROVAL, Client, Envelope, WbError

MARK = {"ok": "ok", "finding": "FINDING", "usage": "USAGE", "environment": "ENVIRONMENT"}


def _duration(text: str | None) -> float | None:
    if text is None:
        return None
    units = {"s": 1, "m": 60, "h": 3600}
    text = text.strip()
    if text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text)


def parse_params(pairs: list[str], schema: dict | None) -> dict:
    params = {}
    properties = (schema or {}).get("properties", {})
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"wb: parameters are key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        spec = properties.get(key, {})
        structured = spec.get("type") in ("array", "object")
        if value[:1] in "[{":
            try:
                value = json.loads(value)
            except ValueError as error:
                if structured:
                    raise SystemExit(f"wb: {key}={value} is not JSON ({error}); for a list, repeat the key "
                                     f"({key}=a {key}=b), or quote the JSON so the shell keeps its quotes") from None
        item_format = (spec.get("items") or {}).get("format") if spec.get("type") == "array" else None
        if (spec.get("format") in ("path.in", "path.out") or item_format in ("path.in", "path.out")) \
                and isinstance(value, str) and value and not os.path.isabs(value) and not value.startswith("/"):
            value = str(Path(value).resolve())
        if spec.get("type") == "array" and not isinstance(value, list):
            value = [value]  # a list parameter: repeat the key (runs=A runs=B) or pass JSON
        if key in params and isinstance(params[key], list):
            params[key] = params[key] + (value if isinstance(value, list) else [value])
        else:
            params[key] = value
    return params


def print_envelope(env: Envelope, as_json: bool):
    if as_json:
        print(json.dumps(env, indent=1, default=str))
        return
    state = env.get("state")
    if state == "awaiting_approval":
        approval = env.get("approval") or {}
        quote, prompt = approval.get("quote") or {}, approval.get("prompt") or {}
        print(f"awaiting approval  {env['command']}  {env['job']}")
        print(f"  {prompt.get('question', '')}")
        for key in ("card", "driver", "host", "dph", "cap_minutes", "max_cost", "currency", "expires_at"):
            if key in quote:
                print(f"  {key}: {quote[key]}")
        print(f"  approve: wb approve {env['job']} --quote {quote.get('id')}")
        return
    if state != "settled":
        print(f"{state}  {env['command']}  {env['job']}  {env.get('summary') or ''}".rstrip())
        if (env.get("waiting") or {}).get("reason"):
            print(f"  waiting: {env['waiting']['reason']}")
        return
    timings = env.get("timings") or {}
    print(f"{MARK.get(env.get('outcome'), env.get('outcome'))}  {env['command']}  {env['job']}"
          f"{'  (cached)' if (env.get('cache') or {}).get('hit') else ''}  {env.get('summary') or ''}".rstrip())
    for reason in env.get("reasons") or []:
        print(f"  - {reason.get('detail')}" + (f"  (fix: {reason['fix']})" if reason.get("fix") else ""))
    data = env.get("data") or {}
    if data:
        text = json.dumps(data, default=str)
        print("  data: " + (text if len(text) < 400 else text[:400] + " ...  (all of it: --json)"))
    for item in env.get("measurements") or []:
        print(f"  {item['name']} = {item['value']}{' ' + item['unit'] if item.get('unit') else ''}")
    for item in env.get("outputs") or []:
        print(f"  -> {item['name']}: {item['host_path']}")
    if env.get("cost"):
        cost = env["cost"]
        print(f"  cost: {cost.get('amount')} {cost.get('currency', 'USD')}")
    if timings.get("phases"):
        print("  phases: " + ", ".join(f"{k} {v:.1f}s" for k, v in timings["phases"].items()))


def _client() -> Client:
    return Client()


def cmd_run(args) -> int:
    client = _client()
    try:
        described = client.command(args.command)
    except WbError as error:
        print(f"wb: {error}", file=sys.stderr)
        return 2
    params = parse_params(args.params, described.get("params"))
    out = str(Path(args.out).resolve()) if args.out and not os.path.isabs(args.out) else args.out
    job = client.submit(args.command, params, out=out, fresh=args.fresh)
    if args.detach:
        print(job.id if not args.json else json.dumps(job.envelope, indent=1))
        return 0
    if not args.quiet:
        print(f"wb: {job.id}  {args.command}  (wb show {job.id})", file=sys.stderr)
        _follow(client, job.id)
    env = job.wait(_duration(args.timeout))
    print_envelope(env, args.json)
    return env.exit_code


def _follow(client: Client, job: str):
    """Streams what a job waits for and its notices to stderr while it runs; stdout stays the one envelope."""
    import threading

    def stream():
        try:
            for event in client.events(job):
                kind, data = event.get("kind"), event.get("data") or {}
                if kind == "waiting":
                    print(f"wb: waiting: {data.get('reason')}", file=sys.stderr)
                elif kind == "state" and data.get("state") in ("running",):
                    print("wb: running", file=sys.stderr)
                elif kind == "log" and data.get("level") in ("WARNING", "ERROR"):
                    print(f"wb: {data.get('msg')}", file=sys.stderr)
                elif kind == "child":
                    print(f"wb: started {data.get('command')} {data.get('job')}", file=sys.stderr)
        except Exception:  # noqa: BLE001 - following is a courtesy; the wait below decides the result
            return

    threading.Thread(target=stream, name="follow", daemon=True).start()


def cmd_wait(args) -> int:
    env = _client().job(args.job).wait(_duration(args.timeout))
    print_envelope(env, args.json)
    return env.exit_code


def cmd_show(args) -> int:
    env = _client().job(args.job).refresh()
    print_envelope(env, args.json)
    return env.exit_code


def cmd_jobs(args) -> int:
    listed = _client().jobs(state=args.state, command=args.command, limit=args.limit,
                            caller=os.environ.get("WB_CALLER") if args.mine else None)
    if args.json:
        print(json.dumps(listed, indent=1, default=str))
        return 0
    for env in listed:
        state = env["state"] + (f" ({env['phase']})" if env.get("phase") and env["state"] == "queued" else "")
        print(f"{env['job']}  {state:<21} {str(env.get('outcome') or ''):<11} {env['command']:<24} "
              f"{env.get('summary') or ''}"[:160])
    return 0


def cmd_ls(args) -> int:
    commands = _client().commands()
    if args.json:
        print(json.dumps(commands, indent=1))
        return 0
    for item in commands:
        if args.module and item["module"] != args.module:
            continue
        flags = "".join(f for f, on in (("$", item["spends"]), ("=", item["deterministic"])) if on)
        state = "active  " if item["active"] else "inactive"
        print(f"{state} {item['name']:<28} {flags:<2} {item['summary']}")
        for reason in item.get("reasons", []):
            print(f"           - {reason['detail']}  (fix: {reason['fix']})")
    return 0


def cmd_help(args) -> int:
    item = _client().command(args.command)
    if args.json:
        print(json.dumps(item, indent=1))
        return 0
    print(f"{item['name']} {item['version']}: {item['summary']}")
    print(f"  {'active' if item['active'] else 'inactive'}; runs on {', '.join(item['runs_on'])}"
          f"{'; spends money (needs approval)' if item['spends'] else ''}"
          f"{'; deterministic (cached)' if item['deterministic'] else ''}")
    props = item["params"].get("properties", {})
    required = set(item["params"].get("required", []))
    for name, spec in props.items():
        kind = spec.get("format") or spec.get("type") or ("enum " + "|".join(map(str, spec.get("enum", []))))
        extra = "" if name in required else f" = {json.dumps(spec.get('default'))}" if "default" in spec else " (optional)"
        print(f"  {name}: {kind}{extra}{'  ' + spec['description'] if spec.get('description') else ''}")
    for reason in item.get("reasons", []) + item.get("warnings", []):
        print(f"  ! {reason['detail']}  (fix: {reason['fix']})")
    return 0


def cmd_approve(args) -> int:
    try:
        env = _client().job(args.job).approve(args.quote, args.via)
    except WbError as error:
        print(f"wb: {error}", file=sys.stderr)
        envelope = (error.body or {}).get("envelope")
        if envelope:
            print_envelope(Envelope(envelope), args.json)
        return EXIT_AWAITING_APPROVAL if error.status == 409 else 3
    if args.no_wait:
        print_envelope(env, args.json)
        return 0
    env = _client().job(args.job).wait()
    print_envelope(env, args.json)
    return env.exit_code


def cmd_cancel(args) -> int:
    env = _client().job(args.job).cancel()
    print_envelope(env, args.json)
    return 0


def cmd_logs(args) -> int:
    client = _client()
    target = args.target
    job = target if target and target.startswith("j_") else None
    module = None if job else target
    lines = client.logs(module=module, job=job, limit=args.n)
    if args.json:
        print(json.dumps(lines, indent=1, default=str))
        return 0
    for line in lines:
        if "line" in line:
            print(line["line"])
        else:
            print(f"{line['ts']} {line['level']:<5} [{line.get('module')}] {line.get('job') or '-'} {line['msg']}")
    return 0


def cmd_hook(args) -> int:
    from . import hooks
    return hooks.run(args.name, args.rest)


def cmd_setup(args) -> int:
    from . import setup
    return setup.main(args)


def cmd_deploy(args) -> int:
    from . import deploy
    return deploy.main(args)

def cmd_work(args) -> int:
    from . import work
    return work.main(args)

def cmd_backup(args) -> int:
    from . import backup
    return backup.main(args)

def cmd_store(args) -> int:
    from . import store
    return store.main(args)


def cmd_land(args) -> int:
    from . import land
    return land.main(args)


def cmd_gpu(args) -> int:
    from . import gpu
    return gpu.main(args)


def cmd_play(args) -> int:
    from . import play
    return play.main(args)

def _stand_ins(workspace, given: list, args) -> dict:
    """Each --with name=<ref|folder>: that project's checks run first on the commit (a folder: its would-be tree, as
    wb check takes one), so the Store holds its files; returns {name: tree} for the run that stands them in."""
    from .workspace import WorkspaceError, git
    client = _client()
    wanted = {}
    for item in given:
        name, sep, where = item.partition("=")
        project = workspace.projects().get(name)
        if not sep or project is None:
            raise WorkspaceError(f"--with {item!r}: name a registered project as <project>=<ref or folder>")
        folder = Path(where)
        commit = workspace.would_be(folder)["commit"] if folder.is_dir() else \
            git(project.repo, "rev-parse", "--verify", f"{where}^{{commit}}")
        print(f"wb: {name} stands in at {commit[:12]}: its checks first", file=sys.stderr)
        envelope = client.submit("ci.run", {"project": name, "commit": commit}).wait(_duration(args.timeout))
        if envelope.get("outcome") != "ok":
            raise WorkspaceError(f"{name} at {commit[:12]} did not pass its checks, so it cannot stand in: "
                                 f"{envelope.get('summary')}")
        wanted[name] = git(project.repo, "rev-parse", f"{commit}^{{tree}}")
    return wanted


def cmd_check(args) -> int:
    """ci.run for the worktree you stand in: its project from the repository, the commit from HEAD, or the tree
    the host's git would commit when the worktree has changes. No commit= or checkout= to type. The host checks among
    the names (``on = "host"``, ``gpu = true``) go to ci.host-run instead, which runs them natively."""
    from . import check
    from .workspace import Workspace, WorkspaceError
    try:
        workspace = Workspace()
        project, top = workspace.project_for(Path.cwd())
        from . import setwork
        revision = setwork.resolve(workspace, Path.cwd())
        tree = workspace.would_be(top)
    except WorkspaceError as error:
        print(f"wb: {error}", file=sys.stderr)
        return 2
    answer = check.predates(workspace, project, tree["commit"])
    if answer:
        print(f"wb: {answer['message']}", file=sys.stderr)
        if not answer["runs"]:
            return 2
    common = {"project": project.name, "commit": tree["commit"], **({"set": revision} if revision else {})}
    if args.stand_in:
        try:
            common["stand_ins"] = _stand_ins(workspace, args.stand_in, args)
        except WorkspaceError as error:
            print(f"wb: {error}", file=sys.stderr)
            return 2
    where = check.split(workspace, project, tree["commit"], args.names)
    runs = []  # (where, command, params, names)
    if where["container"] or not where["host"]:
        params = dict(common)
        if where["container"]:
            params["workflow"] = where["container"][0]
            params["jobs"] = where["container"][1:]
        runs.append(("in the container", "ci.run", params, where["container"]))
    if where["host"]:
        runs.append(("on the host", "ci.host-run", {**common, "checks": where["host"]}, where["host"]))
    what = (f"the working tree of {top} (tree {tree['tree'][:12]}, on {tree['head'][:12]})" if tree["dirty"]
            else f"{top} at {tree['commit'][:12]}")
    client = _client()
    jobs = []
    for place, command, params, names in runs:
        try:
            job = client.submit(command, params)
        except WbError as error:
            print(f"wb: {command} for {', '.join(names) or 'the gate'} {place}: {error}", file=sys.stderr)
            return 2
        jobs.append((place, names, job))
        if not args.quiet:
            listed = f" {place}: {', '.join(names)}" if where["host"] else ""
            print(f"wb: {job.id}  check {project.name}: {what}{listed}  (wb show {job.id})", file=sys.stderr)
            _follow(client, job.id)
    envelopes = [(place, names, job.wait(_duration(args.timeout))) for place, names, job in jobs]
    if args.json and len(envelopes) > 1:
        print(json.dumps([env for _, _, env in envelopes], indent=1, default=str))
    else:
        for _, _, env in envelopes:
            print_envelope(env, args.json)
    if len(envelopes) > 1 or where["host"]:
        print("wb: ran " + "; ".join(f"{place}: {', '.join(names)} ({env.get('outcome') or env.get('state')})"
                                     for place, names, env in envelopes), file=sys.stderr)
    return next((env.exit_code for _, _, env in envelopes if env.exit_code), 0)


def cmd_job(args) -> int:
    """A job's envelope: the one named, or the newest."""
    client = _client()
    if args.job:
        env = client.job(args.job).refresh()
    else:
        listed = client.jobs(limit=1, caller=os.environ.get("WB_CALLER"))  # this caller's newest, when it names itself
        if not listed:
            print("wb: no jobs yet", file=sys.stderr)
            return 2
        env = listed[0]
    print_envelope(env, args.json)
    return env.exit_code if hasattr(env, "exit_code") else 0


def cmd_config(args) -> int:
    if args.action == "reload":
        body = _client().reload_config()
        print(json.dumps(body, indent=1) if args.json else
              f"config reloaded: {body['active']} of {body['commands']} commands active")
        return 0
    if args.local:
        config = wbconfig.load()
        values, origins = config.values, config.origins
        for warning in wbconfig.deprecations(config):  # old keys, read for one more release
            print(f"wb: config: {warning}", file=sys.stderr)
    else:
        body = _client().config(origin=args.origin)
        values, origins = body["values"], body.get("origins", {})
    if args.json:
        print(json.dumps({"values": values, "origins": origins if args.origin else None}, indent=1))
        return 0
    for key, value in sorted(wbconfig.flatten(values).items()):
        origin = f"   # {origins.get(key)}" if args.origin and origins.get(key) else ""
        print(f"{key} = {json.dumps(value, default=str)}{origin}")
    return 0


def cmd_gc(args) -> int:
    from . import gc
    return gc.main(args)


def cmd_prune(args) -> int:
    print("wb: prune is now gc: `wb gc` lists what would go and prints its digest, `wb gc --yes <digest>` removes "
          "exactly that list (this alias goes in the next release)", file=sys.stderr)
    return 2


def cmd_schema(args) -> int:
    print(json.dumps(_client().schema(), indent=1))
    return 0


def cmd_up(args) -> int:
    try:
        health = compose.up(wbconfig.load())
    except (compose.ComposeError, wbconfig.ConfigError) as error:
        print(f"wb: {error}", file=sys.stderr)
        return 3
    print(f"Workbench {health['version']} is up: {health['commands']['active']} of "
          f"{health['commands']['total']} commands active")
    return 0


def cmd_down(args) -> int:
    compose.down(wbconfig.load())
    return 0


def cmd_restart(args) -> int:
    print("wb: restart stops running jobs; wb deploy drains them first and restarts the host runner too",
          file=sys.stderr)
    cmd_down(args)
    return cmd_up(args)


def cmd_status(args) -> int:
    if getattr(args, "doctor", False):
        return _doctor(args)
    try:
        health = _client().health()
    except WbError as error:
        print(f"wb: {error}", file=sys.stderr)
        return 3
    print(json.dumps(health, indent=1) if args.json else
          f"up {health['uptime_s']}s, version {health['version']}, {health['commands']['active']}/"
          f"{health['commands']['total']} commands active, {health['running']} running, host: "
          f"{'ok' if health.get('host') and 'error' not in health['host'] else (health.get('host') or {}).get('error', 'off')}")
    if not args.json:
        from wb_core.identity import short
        host = health.get("host") or {}
        line = f"  commit {short(health.get('commit'))}"
        if host.get("commit") and host.get("commit") != health.get("commit"):
            line += f"; the host runner runs {short(host['commit'])}: run wb deploy"
        if health.get("draining"):
            line += "; draining for a deploy"
        lines = [line]
        for item in health.get("stalled") or []:
            lines.append(f"  stalled: {item['job']}: {item['reason']}")
        for module, hook in (health.get("hooks_failed") or {}).items():
            lines.append(f"  start-up hook of {module} failed: {hook.get('summary')}")
        for warning in health.get("config_warnings") or []:
            lines.append(f"  config: {warning}")
        print("\n".join(lines))
    return 0


def _doctor(args) -> int:
    """``wb status --doctor``: the daemon's health when it answers, then the machine and every project's branch
    read from git and config (no daemon needed). Exit 1 when anything is found."""
    from . import doctor
    try:
        health = _client().health()
        if not args.json:
            print(f"up {health['uptime_s']}s, version {health['version']}")
    except WbError as error:
        print(f"wb: the daemon did not answer ({error}); the doctor reads git and config without it", file=sys.stderr)
    return doctor.main(args)


def cmd_build(args) -> int:
    try:
        tools = wbconfig.load().get("core.image.tools", []) or []
        return compose.build(args.tag, [str(t) for t in tools])
    except (compose.ComposeError, wbconfig.ConfigError) as error:
        print(f"wb: {error}", file=sys.stderr)
        return 3


def cmd_host(args) -> int:
    try:
        from wb_host import cli as host_cli
    except ImportError:
        print("wb: the host runner is not installed here (install the Workbench checkout on Windows)", file=sys.stderr)
        return 3
    return host_cli.main([args.action] + args.rest)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wb", description="Workbench Reloaded.")
    sub = parser.add_subparsers(dest="verb", required=True)

    def add(name, function, help_text):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(function=function)
        p.add_argument("--json", action="store_true")
        return p

    p = add("run", cmd_run, "run a command and wait for its result")
    p.add_argument("command")
    p.add_argument("params", nargs="*", metavar="key=value")
    p.add_argument("--out")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--detach", action="store_true")
    p.add_argument("--quiet", action="store_true", help="print nothing but the result: no job id, no progress")
    p.add_argument("--timeout")
    p = add("wait", cmd_wait, "wait for a job")
    p.add_argument("job")
    p.add_argument("--timeout")
    p = add("show", cmd_show, "show a job's envelope")
    p.add_argument("job")
    p = add("jobs", cmd_jobs, "list jobs")
    p.add_argument("--state")
    p.add_argument("--command")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--mine", action="store_true", help="only jobs submitted with this WB_CALLER")
    p = add("ls", cmd_ls, "list commands")
    p.add_argument("module", nargs="?")
    p = add("help", cmd_help, "describe a command")
    p.add_argument("command")
    p = add("approve", cmd_approve, "approve a job's current quote")
    p.add_argument("job")
    p.add_argument("--quote", required=True)
    p.add_argument("--via", choices=["direct", "relayed"], default="direct")
    p.add_argument("--no-wait", action="store_true")
    p = add("cancel", cmd_cancel, "cancel a job")
    p.add_argument("job")
    p = add("logs", cmd_logs, "show log lines")
    p.add_argument("target", nargs="?")
    p.add_argument("-n", type=int, default=100)
    p = add("config", cmd_config, "show the merged config, or have the daemon reload it")
    p.add_argument("action", choices=["show", "reload"])
    p.add_argument("--origin", action="store_true")
    p.add_argument("--local", action="store_true", help="read the config folder here, not the daemon's")
    p = add("gc", cmd_gc, "list what the retention rules would remove, with a digest; --yes <digest> removes it")
    p.add_argument("--yes", metavar="DIGEST", help="remove exactly the list with this digest")
    p.add_argument("--only", action="append", metavar="CLASS", help="only this class (repeatable)")
    p.add_argument("--rules", action="store_true", help="every class, its rule and whether it runs unattended")
    p.add_argument("--nightly", action="store_true", help="remove the unattended classes (for the scheduler)")
    p.add_argument("--schedule", action="store_true", help="print the line that runs --nightly each night")
    p = sub.add_parser("prune", help=argparse.SUPPRESS)  # the old verb, for one release: names wb gc
    p.set_defaults(function=cmd_prune)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--json", action="store_true")
    add("schema", cmd_schema, "print the API schema")
    add("up", cmd_up, "start the container")
    add("down", cmd_down, "stop the container")
    add("restart", cmd_restart, "restart the container (new token)")
    p = add("status", cmd_status, "health of the daemon")
    p.add_argument("--doctor", action="store_true",
                   help="also report what still ties a project to the old routes: pins, refresh tasks, the lease")
    p = add("build", cmd_build, "build the image from this checkout")
    p.add_argument("--tag", default="wb-core:dev")
    p = add("check", cmd_check, "run CI's checks on the worktree you stand in (its changes included); named host "
                                "checks run on the host")
    p.add_argument("names", nargs="*", help="a workflow or check, then more (default: the jobs file's workflow); "
                                            "host checks among them (on = \"host\") run on the host, for a trusted project")
    p.add_argument("--with", dest="stand_in", action="append", metavar="PROJECT=REF|FOLDER",
                   help="build against this project's files at a ref or worktree instead of its line (no pass recorded)")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--timeout")
    p = add("deploy", cmd_deploy, "gate, release, drain, and restart the daemon and the host runner on this "
                                  "checkout's commit (the image is built only when its inputs changed)")
    p.add_argument("--commit", metavar="SHA", help="deploy this commit instead of HEAD, from a clean export (the "
                                                   "undo: --commit <the previous release>)")
    p.add_argument("--wait", default="10m", help="how long running jobs may take to end (default 10m)")
    p.add_argument("--now", action="store_true", help="cancel what still runs after the wait, listing it first")
    p.add_argument("--override", metavar="REASON", help="skip the gate (the container pass and the host suite); "
                                                         "the reason is recorded in RELEASE.json")
    p = add("work", cmd_work, "worktrees with a lifecycle: the view, start, add, adopt, finish")
    p.add_argument("action", nargs="?", choices=["view", "start", "add", "adopt", "finish", "sync"])
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true", help="every registered project's worktrees")
    p = add("land", cmd_land, "move projects' lines in the Set: the only route to a set-named branch")
    p.add_argument("targets", nargs="*", metavar="PROJECT=REF", help="default: HEAD of the worktree you stand in")
    p.add_argument("--add", action="append", metavar="PROJECT=URL", help="a project's first line, with its public url")
    p.add_argument("--branch", help="the set-named branch (default: the line's, else the remote's default)")
    p.add_argument("--dry", action="store_true", help="validate and compose only; publish nothing")
    p.add_argument("--hold", metavar="PROJECT:ARTIFACT[@CONSUMER]", help="keep one artifact on another commit, "
                   "with seeded bytes (--file, --commit, --reason, --source)")
    p.add_argument("--file", help="--hold: the bytes")
    p.add_argument("--commit", help="--hold: the commit they were built from")
    p.add_argument("--reason", help="--hold: why they are held")
    p.add_argument("--source", help="--hold, --third without --module: where the bytes came from (a URL or a backup)")
    p.add_argument("--drop-hold", action="append", metavar="NAME", help="remove a hold")
    p.add_argument("--third", metavar="NAME", help="land a third-party entry (--module, --repository, --third-file; without --module: the one JAR in <out>/third/<name>/, with --source and --licence)")
    p.add_argument("--module", help="--third: group:artifact:version")
    p.add_argument("--repository", help="--third: the Maven repository's URL")
    p.add_argument("--third-file", action="append", metavar="KIND[:CLASSIFIER]=SHA256",
                   help="--third: a file and the SHA-256 it must have (runtime=..., dev:dev=...)")
    p.add_argument("--licence", help="--third: its licence (SPDX id)")
    p.add_argument("--mod-id", help="--third: the mod id it loads as")
    p = add("backup", cmd_backup, "copy the job database, the ledger and the scrub rules off this drive")
    p.add_argument("--to", help="the folder to back up into (default [paths] backup, else ~/wb-backup)")
    p = add("store", cmd_store, "what CI built: by project and commit, a kept file's path, or bytes to seed")
    p.add_argument("action", nargs="?", default=None)
    p.add_argument("names", nargs="*")
    p.add_argument("--as", dest="label", help="add: what the bytes are, as project:artifact@commit")
    p.add_argument("--source", help="add: where the bytes came from")
    p.add_argument("--reason", help="add: why they are seeded rather than built")
    p = add("job", cmd_job, "show a job's envelope, or the newest job's")
    p.add_argument("job", nargs="?")
    p = add("hook", cmd_hook, "a git hook, answered on the host (the hook stubs call this)")
    p.add_argument("name", choices=["pre-commit", "commit-msg", "pre-push"])
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p = add("setup", cmd_setup, "install Workbench's hooks in this machine's repositories (a plan unless --yes)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--report", action="store_true", help="scrub unpushed commits and ref names; change nothing")
    p.add_argument("--undo", action="store_true", help="remove Workbench's hooks, restoring those kept at install")
    p.add_argument("--repo", action="append", default=[], help="another repository to cover")
    p = add("gpu", cmd_gpu, "the GPU lease: who holds it and who waits; a turn for any command (wb gpu run -- ...)")
    p.add_argument("action", nargs="?", choices=["status", "run", "leave", "stuck"])
    p.add_argument("--lane", help="run: the lane's name (default: one named after this folder and the command)")
    p.add_argument("--note", help="run: what the turn is for")
    p.add_argument("--limit", help="run: the longest turn (1h by default); past it the command is stopped")
    p.add_argument("--detach", action="store_true", help="run: as a process of its own that outlives the shell")
    p.add_argument("--quiet", action="store_true", help="run: a measurement; recorded in the lane and the lease")
    p.add_argument("rest", nargs=argparse.REMAINDER, metavar="-- COMMAND | LANE")
    p = add("play", cmd_play, "resolve a launch, build its instance, and play a session of it on this GPU")
    p.add_argument("target", nargs="?", metavar="[PROJECT][@REF|@set|@head][:LAUNCH]")
    p.add_argument("--from", dest="from_file", metavar="LAUNCH.JSON", help="replay a launch file; resolve nothing")
    p.add_argument("--name", help="the instance's name (default <project>-<launch>)")
    p.add_argument("--tour", help="a tour file: play it and file its captures")
    p.add_argument("--measure", action="store_true", help="the tour is evidence: quiet, on a fresh copy")
    p.add_argument("--driver", help="run the launch's declared driver of this name")
    p.add_argument("--debug", choices=["client", "server", "both"], help="JDWP on loopback; not evidence")
    world = p.add_mutually_exclusive_group()
    world.add_argument("--fresh-world", action="store_true", help="remove the instance's world first")
    world.add_argument("--keep-world", action="store_true", help="play on the world the instance has (the default)")
    p.add_argument("--param", action="append", metavar="K=V", help="a launch param over the launch's (part of the "
                   "template's key)")
    p.add_argument("--configure", metavar="FILE", help="the product's configure hook, until its launch names it "
                   "(runs only for a project in [lab] trusted)")
    p.add_argument("--jvm", action="append", metavar="OPT", help="a session JVM option (-X, client:-X, server:-X)")
    p.add_argument("--view", action="append", metavar="K=V", help="a view distance (horizontal, vertical, "
                   "client_render)")
    p.add_argument("--window", metavar="WxH", help="the client's window")
    p.add_argument("--edit", action="append", metavar="SIDE/FILE:KEY=VALUE", help="an instance file changed for this "
                   "session only")
    p.add_argument("--no-rebuild", action="store_true", help="build no missing member: refuse, naming the check")
    p.add_argument("--desktop", choices=["hidden"], help="hidden: the client on a hidden Windows desktop that never "
                   "takes the input")
    p = add("host", cmd_host, "the Windows host runner")
    p.add_argument("action", choices=["serve", "install", "uninstall", "status"])
    p.add_argument("rest", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.function(args)
    except WbError as error:
        print(f"wb: {error}", file=sys.stderr)
        return 2 if error.status in (400, 404) else 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
