"""``wb``: the terse front end. Workbench's own options are flags; a command's parameters are key=value.

    wb up | down | restart | status | build
    wb ls [module]                       commands, active or inactive, with reasons
    wb help vast.rent                    parameters, requirements, result schema
    wb run frames.compare ref=a/ cand=b/ profile=near [--out DIR] [--json] [--detach] [--fresh]
    wb wait JOB [--timeout 10m] | wb show JOB | wb jobs [--state S] | wb logs [MODULE|JOB] [-n N]
    wb approve JOB --quote Q [--via relayed] | wb cancel JOB
    wb config show [--origin] | wb prune [--yes] | wb schema
    wb host serve | install | status

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

def cmd_check(args) -> int:
    """ci.run for the worktree you stand in: its project from the repository, the commit from HEAD, or the tree
    the host's git would commit when the worktree has changes. No commit= or checkout= to type."""
    from .workspace import Workspace, WorkspaceError
    try:
        workspace = Workspace()
        project, top = workspace.project_for(Path.cwd())
        from . import setwork
        setwork.resolve(workspace, Path.cwd())
        tree = workspace.would_be(top)
    except WorkspaceError as error:
        print(f"wb: {error}", file=sys.stderr)
        return 2
    params = {"project": project.name, "commit": tree["commit"]}
    if args.names:
        params["workflow"] = args.names[0]
        params["jobs"] = args.names[1:]
    what = (f"the working tree of {top} (tree {tree['tree'][:12]}, on {tree['head'][:12]})" if tree["dirty"]
            else f"{top} at {tree['commit'][:12]}")
    client = _client()
    job = client.submit("ci.run", params)
    if not args.quiet:
        print(f"wb: {job.id}  check {project.name}: {what}  (wb show {job.id})", file=sys.stderr)
        _follow(client, job.id)
    env = job.wait(_duration(args.timeout))
    print_envelope(env, args.json)
    return env.exit_code


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


def cmd_prune(args) -> int:
    result = _client().request("POST", "/v1/prune", {"dry_run": not args.yes})
    print(json.dumps(result, indent=1) if args.json else
          f"{'would remove' if result['dry_run'] else 'removed'}: {len(result['job_dirs'])} job folders, "
          f"{len(result['store'])} stored artifacts, {len(result['logs'])} log archives (older than {result['days']} days)")
    return 0


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
        print("\n".join(lines))
    return 0


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
    p = add("prune", cmd_prune, "prune old logs, job folders and artifacts (dry run unless --yes)")
    p.add_argument("--yes", action="store_true")
    add("schema", cmd_schema, "print the API schema")
    add("up", cmd_up, "start the container")
    add("down", cmd_down, "stop the container")
    add("restart", cmd_restart, "restart the container (new token)")
    add("status", cmd_status, "health of the daemon")
    p = add("build", cmd_build, "build the image from this checkout")
    p.add_argument("--tag", default="wb-core:dev")
    p = add("check", cmd_check, "run CI's checks on the worktree you stand in (its changes included)")
    p.add_argument("names", nargs="*", help="a workflow or check, then more (default: the jobs file's workflow)")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--timeout")
    p = add("deploy", cmd_deploy, "test, build, drain, and restart the daemon and the host runner on this checkout")
    p.add_argument("--wait", default="10m", help="how long running jobs may take to end (default 10m)")
    p.add_argument("--now", action="store_true", help="cancel what still runs after the wait, listing it first")
    p.add_argument("--override", metavar="REASON", help="skip the test gate; the reason is recorded")
    p = add("work", cmd_work, "worktrees with a lifecycle: the view, start, add, adopt, finish")
    p.add_argument("action", nargs="?", choices=["view", "start", "add", "adopt", "finish", "sync"])
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true", help="every registered project's worktrees")
    p = add("land", cmd_land, "move projects' lines in the Set: the only route to a set-named branch")
    p.add_argument("targets", nargs="*", metavar="PROJECT=REF", help="default: HEAD of the worktree you stand in")
    p.add_argument("--add", action="append", metavar="PROJECT=URL", help="a project's first line, with its public url")
    p.add_argument("--branch", help="the set-named branch (default: the line's, else the remote's default)")
    p.add_argument("--dry", action="store_true", help="validate and compose only; publish nothing")
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
