"""``wb mcp``: Workbench for MCP clients, over stdio.

    wb mcp              serve until the client closes stdin
    wb mcp --list       the tools and skills it serves, then exit
    wb mcp --config     the entry an MCP client's config takes for this machine's release, as JSON

Ten tools, each a thin form of a verb (``commands``, ``describe``, ``run``, ``job``, ``jobs``, ``logs``, ``cancel``,
``approve``, ``status``, ``cli``), and the skills under ``skills/`` that say how to use them. The skills are served
through MCP's Skills extension (``io.modelcontextprotocol/skills``): a client lists them by name and description
(``skills/list``) and reads a skill's files (``resources/read``) only when it loads that skill, each file checked
against the SHA-256 and size its entry names. Until then a client holds ten short tool descriptions and one line
per skill.

It speaks protocol revision 2026-07-28, in which every request names its version and the client's capabilities and
nothing is remembered between requests, and it answers the ``initialize`` handshake of the revisions before it, for
a client that still opens with one. stdout carries protocol messages only: whatever else this process prints goes
to stderr.

Approval stays the person's. ``approve`` asks the person through the client (an elicitation, which the client shows
and the person answers); a client that cannot ask is told the command the person runs. ``cli`` refuses ``approve``,
the verbs that change the service this process is part of, and ``gpu run``, which runs any command. These refusals
keep a client from approving or redeploying by mistake. They are no boundary against a program that can already run
commands as this user: the daemon's token is the person's, and so is everything it reaches.

Standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .client import TERMINAL, Client, Envelope, WbError

MODERN = ("2026-07-28",)                                            # served request by request
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")   # served after an initialize handshake
SKILLS_EXTENSION = "io.modelcontextprotocol/skills"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_SERVER = "io.modelcontextprotocol/serverInfo"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
UNSUPPORTED_VERSION = -32022

SERVER = {"name": "workbench", "title": "Workbench", "version": __version__}
SKILLS = Path(__file__).parent / "skills"
SKILL_FILE = "SKILL.md"
STATIC_TTL_MS = 3_600_000    # the tools and this server's identity change only with a release
SKILLS_TTL_MS = 60_000       # a deploy can change a skill's files under a running server
TEXT_LIMIT = 24_000          # the most characters one tool answer carries
POLL_S = 15                  # one long poll of a job, between looks at a cancellation
WAIT_S, WAIT_MOST_S = 120, 3600
CLI_S, CLI_MOST_S = 600, 14_400
CANCEL_S = 10                # how long cancel watches a job end
DRAIN_S = 1                  # how long answers under way may take once the client closed stdin
ASK_S = 600                  # how long a question put to the person may be answered
RESOURCE_NOT_FOUND = -32002  # what the revisions before 2026-07-28 call a resource that is not there

INSTRUCTIONS = (
    "Workbench (wb) runs Minecraft modding commands as jobs; each settles in one envelope with an outcome: ok, "
    "finding, usage or environment. Find a command with `commands` and `describe`, run it with `run`, follow it "
    "with `job`; `cli` runs the verbs that act on the folder you stand in (check, work, land, play, store, gpu). "
    "Before the first call, load the skill `start` (skill://start/SKILL.md); `checks`, `land`, `play`, `spend` and "
    "`diagnose` cover those tasks. Spending and publishing wait for the person's approval of one exact quote: "
    "show it to them, and never approve for them."
)


class Refusal(Exception):
    """A request this server answers with a JSON-RPC error."""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.data = code, data


# -- skills ---------------------------------------------------------------------------------------------------------
class SkillError(ValueError):
    """A skill folder that cannot be served as it is."""


_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*\Z")
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
_TYPED = {"null", "true", "false", "yes", "no", "on", "off", "y", "n"}   # words some YAML parser reads as no string
_URI = re.compile(r"skill://([^/?#]+)(?:/([^?#]*))?\Z")


def frontmatter(text: str) -> dict:
    """A SKILL.md's frontmatter as the JSON object every YAML parser reads from it.

    A client parses the frontmatter itself and refuses the skill when a field differs from what the skill's entry
    said, so this takes only the part of YAML that has one reading: one ``key: value`` per line, the value a plain
    string that begins with a letter, or a double-quoted one. A key or value another parser could read as a number,
    a flag, a date, a list or a table is refused here rather than guessed, and so is any character that parsers
    treat differently: a tab, a control character, a line break other than LF or CRLF, or space that is not a plain
    space at a value's ends."""
    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    if lines[0].rstrip(" ") != "---":
        raise SkillError("it must begin with a frontmatter block: --- on the first line")
    fields: dict = {}
    for number, line in enumerate(lines[1:], 2):
        if line.rstrip(" ") == "---":
            return fields
        if not line.strip(" "):
            continue
        if any(not _plain(character) for character in line):
            raise SkillError(f"frontmatter line {number} has a tab, a control character or an unusual line break")
        key, colon, value = line.partition(":")
        if not colon or not _KEY.match(key) or key.lower() in _TYPED or (value and not value.startswith(" ")):
            raise SkillError(f"frontmatter line {number} is not `key: value` at the left margin")
        if key in fields:
            raise SkillError(f"frontmatter line {number} repeats {key}")
        fields[key] = _scalar(value.strip(" "), number)
    raise SkillError("its frontmatter block is not closed with ---")


def _plain(character: str) -> bool:
    """Whether every YAML parser takes the character as itself: nothing that controls, breaks a line or cannot be
    encoded."""
    code = ord(character)
    return not (code < 0x20 or 0x7F <= code < 0xA0 or code in (0x2028, 0x2029, 0xFEFF) or 0xD800 <= code <= 0xDFFF)


def _scalar(value: str, number: int) -> str:
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if not isinstance(parsed, str) or any(not _plain(character) for character in parsed) or "\\u" in value:
            raise SkillError(f"frontmatter line {number}: the quoted value is not one string of plain characters "
                             "(write a character as itself, not as an escape)")
        return parsed
    if (not value or not ("a" <= value[0].lower() <= "z") or value != value.strip() or ": " in value or " #" in value
            or value.endswith(":") or value.lower() in _TYPED):
        raise SkillError(f"frontmatter line {number}: write the value as a plain string that begins with a letter, "
                         "or in double quotes")
    return value


class Skills:
    """The skills under one folder: each a folder holding SKILL.md, named as that file's frontmatter names it."""

    def __init__(self, root: Path = SKILLS):
        self.root = Path(root)

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir()
                      if _SKILL_NAME.match(p.name) and p.is_dir() and SKILL_FILE in os.listdir(p))  # by its exact name

    def files(self, name: str) -> dict[str, Path]:
        """Every file of a skill by its path below the skill's folder: SKILL.md first, the rest in path order."""
        top = self.root / name
        found = {}
        for path in sorted(top.rglob("*")):
            parts = path.relative_to(top).parts
            if not path.is_file() or any(part.startswith(".") or part == "__pycache__" for part in parts):
                continue  # folders, and what tools leave behind (__pycache__, dot files)
            if not all(_SEGMENT.match(part) for part in parts):
                raise SkillError(f"{name}: {'/'.join(parts)} has a name a skill URI cannot carry as it is")
            found["/".join(parts)] = path
        if SKILL_FILE not in found:
            raise SkillError(f"{name} holds no {SKILL_FILE}")
        return {SKILL_FILE: found.pop(SKILL_FILE), **found}

    def entry(self, name: str) -> dict:
        """A skill's entry: its URI, its frontmatter, and every file with the digest and size of the bytes served."""
        if name not in self.names():
            raise SkillError(f"no skill named {name}")
        content = {relative: path.read_bytes() for relative, path in self.files(name).items()}
        try:
            fields = frontmatter(content[SKILL_FILE].decode("utf-8"))
        except (SkillError, UnicodeDecodeError) as error:
            raise SkillError(f"{name}/{SKILL_FILE}: {error}") from None
        if fields.get("name") != name:
            raise SkillError(f"{name}/{SKILL_FILE} names itself {fields.get('name')!r}: a skill's folder carries its name")
        if not fields.get("description"):
            raise SkillError(f"{name}/{SKILL_FILE} has no description")
        return {"uri": self.uri(name), "frontmatter": fields,
                "resources": [{"uri": self.uri(name, relative), "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                               "size": len(data)} for relative, data in content.items()]}

    def entries(self) -> list[dict]:
        """Every skill that can be served; one that cannot is named on stderr and left out."""
        found = []
        for name in self.names():
            try:
                found.append(self.entry(name))
            except (SkillError, OSError) as error:
                print(f"wb mcp: skill left out: {error}", file=sys.stderr)
        return found

    @staticmethod
    def uri(name: str, relative: str = SKILL_FILE) -> str:
        return f"skill://{name}/{relative}" if relative else f"skill://{name}"

    def locate(self, uri) -> tuple[str, str] | None:
        """(skill, path below it) for a URI under a skill this serves; the path is empty for the skill's folder."""
        match = _URI.match(uri) if isinstance(uri, str) else None
        if not match or match.group(1) not in self.names():
            return None
        return match.group(1), (match.group(2) or "").rstrip("/")

    def read(self, uri) -> dict | None:
        """One file as resource contents: text when it is UTF-8, else base64. None when no skill has it."""
        where = self.locate(uri)
        if where is None:
            return None
        try:
            path = self.files(where[0]).get(where[1])
        except SkillError:
            return None
        if path is None:
            return None
        data = path.read_bytes()
        item = {"uri": uri, "mimeType": _mime(path)}
        try:
            item["text"] = data.decode("utf-8")  # the bytes exactly, line ends included: a digest covers them
        except UnicodeDecodeError:
            item["blob"] = base64.b64encode(data).decode("ascii")
        return item

    def children(self, uri) -> list[dict] | None:
        """The direct children of a skill's folder or of a folder in it. None when the URI names no such folder."""
        where = self.locate(uri)
        if where is None:
            return None
        name, folder = where
        try:
            files = self.files(name)
        except SkillError:
            return None
        prefix = folder + "/" if folder else ""
        listed: dict[str, dict] = {}
        for relative, path in files.items():
            if not relative.startswith(prefix):
                continue
            head, _, rest = relative[len(prefix):].partition("/")
            listed.setdefault(head, {"uri": self.uri(name, prefix + head), "name": head,
                                     "mimeType": "inode/directory" if rest else _mime(path)})
        if not listed:
            return None
        return [listed[key] for key in sorted(listed)]


def _mime(path: Path) -> str:
    known = {".md": "text/markdown", ".toml": "application/toml", ".json": "application/json", ".txt": "text/plain",
             ".py": "text/x-python", ".sh": "application/x-sh", ".ps1": "text/plain"}
    return known.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


# -- tools ----------------------------------------------------------------------------------------------------------
@dataclass
class Call:
    """One tool call in hand: what the client can do, what it answered, and how to tell it the call is alive."""
    modern: bool = True
    capabilities: dict = field(default_factory=dict)
    responses: dict = field(default_factory=dict)     # the client's answers to what an earlier attempt asked
    state: str | None = None                           # what an earlier attempt handed out with its question
    cancelled: threading.Event = field(default_factory=threading.Event)
    progress: object = None                            # callable(text), when the client asked for progress

    def tell(self, text: str):
        if self.progress is not None and text and not self.cancelled.is_set():
            self.progress(text)


@dataclass
class Answer:
    text: str
    error: bool = False


@dataclass
class Ask:
    """What a call needs from the person before it can go on, as the requests the client is to fulfil. ``state``
    comes back with the answers, and says which question they answer."""
    requests: dict
    state: str | None = None


def _tool(name: str, title: str, description: str, properties: dict, required=(), **hints) -> dict:
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": list(required),
                            "additionalProperties": False},
            "annotations": {"title": title, **hints}}


_READ = {"readOnlyHint": True, "openWorldHint": False}
_DETAIL = {"type": "string", "enum": ["summary", "full"],
           "description": "full: the whole envelope as JSON (default summary: the lines wb show prints)"}
_CWD = {"type": "string", "description": "the folder to stand in, absolute: the worktree a verb reads"}

TOOLS = [
    _tool("commands", "List commands",
          "Workbench's commands. With no argument: each module with its command names. `module` or `query` lists "
          "the matching commands with one line each, and why an inactive one is inactive.",
          {"module": {"type": "string", "description": "a module's name, such as ci or jar"},
           "query": {"type": "string", "description": "words to find in names and summaries"}}, **_READ),
    _tool("describe", "Describe a command",
          "One command: what it does, where it runs, whether it spends or is cached, every parameter with its type "
          "and default, and what keeps it inactive.",
          {"command": {"type": "string", "description": "module.command, such as ci.run"}}, ["command"], **_READ),
    _tool("run", "Run a command",
          "Run a command and wait for its envelope. Answers with the result once the job settles, or with the job "
          "id and what it waits for when `wait` seconds pass first (the job goes on: follow it with `job`). A "
          "command that spends stops at a quote.",
          {"command": {"type": "string", "description": "module.command"},
           "params": {"type": "object", "description": "the command's parameters by name, typed as `describe` "
                      "shows them; paths absolute, or relative to `cwd`"},
           "out": {"type": "string", "description": "a folder for the job's outputs"},
           "cwd": {"type": "string", "description": "the folder relative paths start from, absolute"},
           "fresh": {"type": "boolean", "description": "run again though a cached result exists"},
           "locks": {"type": "array", "items": {"type": "string"},
                     "description": "named locks to take beside the command's own, all or none"},
           "wait": {"type": "number", "description": f"seconds to wait (default {WAIT_S}); 0 answers with the job "
                    "id at once"},
           "detail": _DETAIL}, ["command"], readOnlyHint=False, openWorldHint=True),
    _tool("job", "Show or await a job",
          "A job's envelope: the one named, or this client's newest. With `wait`, waits that many seconds for it "
          "to settle first.",
          {"job": {"type": "string", "description": "a job id (j_...); default: this client's newest job"},
           "wait": {"type": "number", "description": "seconds to wait for it to settle (default 0)"},
           "detail": _DETAIL}, **_READ),
    _tool("jobs", "List jobs",
          "Recent jobs, newest first: id, state, outcome, command and summary.",
          {"state": {"type": "string", "description": "queued, running, awaiting_approval, settled, cancelled or "
                     "interrupted"},
           "command": {"type": "string", "description": "only this command's"},
           "limit": {"type": "integer", "description": "how many (default 15)"},
           "mine": {"type": "boolean", "description": "only the jobs this client submitted"}}, **_READ),
    _tool("logs", "Read log lines",
          "Log lines of one job or one module, the newest last.",
          {"target": {"type": "string", "description": "a job id (j_...) or a module's name; default: every module"},
           "limit": {"type": "integer", "description": "how many lines (default 60)"}}, **_READ),
    _tool("cancel", "Cancel a job",
          "Cancel a job that has not settled. What it started ends with it.",
          {"job": {"type": "string", "description": "a job id (j_...)"}}, ["job"],
          readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
    _tool("approve", "Ask the person to approve a quote",
          "Puts a waiting job's quote to the person through this client; on their yes the job runs. It is never a "
          "way around them: where the client cannot ask, it answers with the command the person runs.",
          {"job": {"type": "string", "description": "the job awaiting approval"},
           "quote": {"type": "string", "description": "the quote's id, as the job's envelope names it"},
           "wait": {"type": "number", "description": f"seconds to wait for the job once approved (default {WAIT_S})"}},
          ["job", "quote"], readOnlyHint=False, destructiveHint=True, openWorldHint=True),
    _tool("status", "Daemon health",
          "Whether the daemon answers, its commit, how many commands are active, running and stalled jobs, the "
          "host runner and who holds the GPU lease.", {}, **_READ),
    _tool("cli", "Run a wb verb on the host",
          "Run a wb verb as a terminal would, for the verbs that act on a folder or the machine: check, work, land, "
          "play, store, gpu, gc, config, status --doctor. Answers with the exit code and what the verb printed.",
          {"args": {"type": "array", "items": {"type": "string"},
                    "description": 'the verb and its arguments, such as ["check"] or ["work", "--all"]'},
           "cwd": _CWD,
           "timeout": {"type": "number", "description": f"seconds before the verb is stopped (default {CLI_S})"}},
          ["args"], readOnlyHint=False, destructiveHint=True, openWorldHint=True),
]

# Verbs `cli` does not run, each with why and what to do.
REFUSED = {
    "approve": "approval is the person's: they run `wb approve JOB --quote Q` themselves, or the approve tool asks them",
    "mcp": "it would start this server inside itself",
    "hook": "git calls it, with a hook's input",
    "host": "it manages the host runner's service; run it in a terminal",
    "up": "it starts the service this server talks to; run it in a terminal",
    "down": "it stops the service this server talks to; run it in a terminal",
    "restart": "it restarts the service this server talks to; run wb deploy in a terminal",
    "build": "it builds the image; run it in a terminal",
    "deploy": "it replaces the release this server runs from; run it in a terminal",
    "dev": "it runs a working tree's CLI in place of the release; run it in a terminal",
}
_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}


class Tools:
    """The tools' behaviour. ``client`` makes a Workbench client for one call (the daemon's token changes when it
    restarts, so none is kept), and ``runner`` runs the CLI for ``cli``."""

    def __init__(self, client=Client, runner=None):
        self._client, self._runner = client, runner or run_cli

    def call(self, name: str, arguments: dict, call: Call) -> Answer | Ask:
        tool = next((tool for tool in TOOLS if tool["name"] == name), None)
        if tool is None:
            raise Refusal(INVALID_PARAMS, f"no tool named {name}; there are: {', '.join(t['name'] for t in TOOLS)}")
        problem = _check(tool["inputSchema"], arguments)
        if problem:
            return Answer(f"{name}: {problem}", error=True)
        try:
            return getattr(self, "_" + name)(arguments, call)
        except WbError as error:
            reach = "" if error.status is not None else "  (start the daemon in a terminal: wb up)"
            return Answer(f"wb: {error}{reach}", error=True)

    # -- reading ---------------------------------------------------------------------------------------------------
    def _commands(self, args, call) -> Answer:
        commands = self._client().commands()
        module, query = args.get("module"), (args.get("query") or "").lower().split()
        if not module and not query:
            by_module: dict[str, list[str]] = {}
            for item in commands:
                short = item["name"].split(".", 1)[-1]
                by_module.setdefault(item["module"], []).append(short if item["active"] else f"({short})")
            active = sum(1 for item in commands if item["active"])
            lines = [f"{len(commands)} commands, {active} active; one in parentheses is inactive. "
                     "commands(module=...) lists a module's with a line each."]
            lines += [f"{name}: {', '.join(names)}" for name, names in sorted(by_module.items())]
            return Answer("\n".join(lines))
        chosen = [item for item in commands if (not module or item["module"] == module)
                  and all(word in (item["name"] + " " + item["summary"]).lower() for word in query)]
        if not chosen:
            modules = ", ".join(sorted({item["module"] for item in commands}))
            return Answer(f"no command matches; the modules are: {modules}")
        lines = []
        for item in chosen:
            flags = "".join(mark for mark, on in ((" $", item["spends"]), (" =", item["deterministic"])) if on)
            lines.append(f"{item['name']}{flags}{'' if item['active'] else '  INACTIVE'}  {item['summary']}")
            lines += [f"    - {reason['detail']}  (fix: {reason['fix']})" for reason in item.get("reasons", [])]
        lines.append("($ spends and waits for approval, = cached; describe(command) gives the parameters)")
        return Answer(_head("\n".join(lines)))

    def _describe(self, args, call) -> Answer:
        item = self._client().command(args["command"])
        lines = [f"{item['name']} {item['version']}: {item['summary']}",
                 f"  {'active' if item['active'] else 'INACTIVE'}; runs on {', '.join(item['runs_on'])}"
                 f"{'; spends (waits for approval)' if item['spends'] else ''}"
                 f"{'; deterministic (cached)' if item['deterministic'] else ''}"]
        properties = item["params"].get("properties", {})
        required = set(item["params"].get("required", []))
        if properties:
            lines.append("  params (* required):")
        for name, spec in properties.items():
            kind = spec.get("format") or spec.get("type") or "enum"
            if spec.get("enum"):
                kind = "|".join(map(str, spec["enum"]))
            default = f" = {json.dumps(spec['default'])}" if "default" in spec and name not in required else ""
            lines.append(f"    {name}{'*' if name in required else ''}: {kind}{default}"
                         f"{'  ' + spec['description'] if spec.get('description') else ''}")
        result = (item.get("result") or {}).get("properties") or {}
        if result:
            lines.append("  data: " + ", ".join(result))
        lines += [f"  ! {reason['detail']}  (fix: {reason['fix']})"
                  for reason in item.get("reasons", []) + item.get("warnings", [])]
        return Answer("\n".join(lines))

    def _job(self, args, call) -> Answer:
        client = self._client()
        named = args.get("job")
        if not named:
            newest = client.jobs(limit=1, caller=os.environ.get("WB_CALLER"))
            if not newest:
                return Answer("this client has submitted no job yet; jobs() lists everyone's")
            named = newest[0]["job"]
        envelope = self._wait(client.job(named), _seconds(args.get("wait"), 0, WAIT_MOST_S), call)
        return Answer(render(envelope, args.get("detail")))

    def _jobs(self, args, call) -> Answer:
        listed = self._client().jobs(state=args.get("state"), command=args.get("command"),
                                     limit=args.get("limit") or 15,
                                     caller=os.environ.get("WB_CALLER") if args.get("mine") else None)
        lines = []
        for envelope in listed:
            state = envelope["state"]
            if state == "queued" and envelope.get("phase"):
                state += f" ({envelope['phase']})"
            lines.append(f"{envelope['job']}  {state}  {envelope.get('outcome') or '-'}  {envelope['command']}  "
                         f"{envelope.get('summary') or ''}"[:200].rstrip())
        return Answer("\n".join(lines) or "no jobs match")

    def _logs(self, args, call) -> Answer:
        target = args.get("target")
        is_job = bool(target) and target.startswith("j_")
        lines = self._client().logs(module=None if is_job else target, job=target if is_job else None,
                                    limit=args.get("limit") or 60)
        shown = [line["line"] if "line" in line else
                 f"{_clock(line['ts'])} {line['level']:<5} [{line.get('module')}] {line.get('job') or '-'} {line['msg']}"
                 for line in lines]
        return Answer(_tail("\n".join(shown)) or "no log lines")

    def _status(self, args, call) -> Answer:
        health = self._client().health()
        host = health.get("host") or {}
        lease = host.get("lease") or {}
        lines = [f"up {health['uptime_s']} s, version {health['version']}, commit {str(health.get('commit'))[:12]}; "
                 f"{health['commands']['active']} of {health['commands']['total']} commands active; "
                 f"{health['running']} running{'; draining for a deploy' if health.get('draining') else ''}"]
        if not host:
            lines.append("  host runner: off")
        elif "error" in host:
            lines.append(f"  host runner: {host['error']}")
        else:
            behind = host.get("commit") and host["commit"] != health.get("commit")
            lines.append(f"  host runner: ok{', on another commit: wb deploy brings them together' if behind else ''}; "
                         f"GPU lease: {'held by ' + str(lease.get('by')) if lease.get('held') else 'free'}")
        lines += [f"  stalled: {item['job']}: {item['reason']}" for item in health.get("stalled") or []]
        lines += [f"  start-up hook of {module} failed: {hook.get('summary')}"
                  for module, hook in (health.get("hooks_failed") or {}).items()]
        warnings = health.get("config_warnings") or []
        if warnings:  # the same lines every time: counted here, listed by the verb
            lines.append(f'  config: {len(warnings)} warning(s); cli(args=["status"]) lists them')
        return Answer("\n".join(lines))

    # -- acting ----------------------------------------------------------------------------------------------------
    def _run(self, args, call) -> Answer:
        client = self._client()
        described = client.command(args["command"])
        problem, params = _params(args.get("params") or {}, described.get("params") or {}, args.get("cwd"))
        if problem:
            return Answer(f"run {args['command']}: {problem}", error=True)
        out = args.get("out")
        if out and not os.path.isabs(out):
            if not args.get("cwd"):
                return Answer("run: `out` is relative; give it absolute, or give `cwd`", error=True)
            out = str(Path(args["cwd"], out))
        wait = _seconds(args.get("wait"), WAIT_S, WAIT_MOST_S)  # every argument is read before anything starts
        job = client.submit(args["command"], params, out=out, fresh=bool(args.get("fresh")),
                            locks=list(args.get("locks") or []))
        return Answer(render(self._wait(job, wait, call), args.get("detail")))

    def _cancel(self, args, call) -> Answer:
        job = self._client().job(args["job"])
        job.cancel()
        envelope = self._wait(job, CANCEL_S, call, refresh=True)  # a running job ends once what it runs has stopped
        if envelope.get("state") in TERMINAL:
            return Answer(render(envelope, None))
        return Answer(f"cancel requested; {args['job']} ends once what it runs has stopped\n"
                      + "\n".join(render(envelope, None).splitlines()[:-1]))

    def _approve(self, args, call) -> Answer | Ask:
        client = self._client()
        job = client.job(args["job"])
        envelope = job.refresh()
        if envelope.get("state") != "awaiting_approval":
            return Answer(f"{args['job']} is not awaiting approval:\n" + render(envelope, None))
        quote = (envelope.get("approval") or {}).get("quote") or {}
        if quote.get("id") != args["quote"]:
            return Answer(f"quote {args['quote']} is no longer the job's quote (it expired or its price moved). The "
                          "current one, to show the person again:\n" + render(envelope, None), error=True)
        by_hand = f"wb approve {args['job']} --quote {args['quote']}"
        if not call.modern or not _can_ask(call.capabilities):
            return Answer("Approval is the person's, and this client has no way to put the quote to them here. "
                          f"Show them the quote; if they agree, they run in a terminal:\n  {by_hand}\n\n"
                          + render(envelope, None), error=True)
        wait = _seconds(args.get("wait"), WAIT_S, WAIT_MOST_S)
        reply = call.responses.get("approval")
        if not isinstance(reply, dict) or not _sealed(call.state, args["job"], args["quote"]):
            # No answer yet, or one that does not come with the question asked for this job and this quote.
            return Ask({"approval": {"method": "elicitation/create", "params": {
                "mode": "form", "message": _question(envelope),
                "requestedSchema": {"type": "object", "required": ["approve"], "properties": {"approve": {
                    "type": "boolean", "title": "Approve this quote",
                    "description": "Yes lets the job run and spend or publish as quoted."}}}}}},
                state=_seal(args["job"], args["quote"]))
        content = reply.get("content")
        if reply.get("action") != "accept" or not isinstance(content, dict) or content.get("approve") is not True:
            return Answer(f"Not approved: the person did not say yes. {args['job']} still waits; cancel(job) ends it.")
        job.approve(args["quote"], "relayed")
        return Answer("approved by the person.\n" + render(self._wait(job, wait, call, refresh=True), None))

    def _cli(self, args, call) -> Answer:
        words = [str(word) for word in args["args"]]
        verb = words[0] if words else ""
        if not verb or verb.startswith("-"):
            return Answer('cli: `args` begins with a verb, such as ["check"] or ["work", "--all"]', error=True)
        if any("\x00" in word for word in words):
            return Answer("cli: an argument holds a NUL character", error=True)
        if verb in REFUSED:
            return Answer(f"cli: wb {verb} is not run from here: {REFUSED[verb]}", error=True)
        if verb == "gpu" and "run" in words[1:]:
            return Answer("cli: wb gpu run is not run from here: it runs any command at all; run it in a terminal",
                          error=True)
        cwd = args.get("cwd")
        if cwd and not Path(cwd).is_dir():
            return Answer(f"cli: `cwd` {cwd} is not a folder", error=True)
        code, output = self._runner(words, cwd, _seconds(args.get("timeout"), CLI_S, CLI_MOST_S), call)
        if code is None:
            return Answer(f"wb {' '.join(words)}: stopped (its time ran out, or the call was cancelled)\n"
                          + _tail(output), error=True)
        return Answer(f"exit {code}  wb {' '.join(words)}\n" + _tail(output), error=code in (2, 3))

    # -- waiting ---------------------------------------------------------------------------------------------------
    @staticmethod
    def _wait(job, seconds: float, call: Call, refresh: bool = False) -> Envelope:
        """The job's envelope once it settles or stops at a quote, else as it stands when the time is up. A wait
        that ends leaves the job running; so does a cancelled call."""
        deadline = time.monotonic() + seconds
        envelope = job.refresh() if refresh or job.envelope is None else job.envelope
        while envelope.get("state") not in TERMINAL and envelope.get("state") != "awaiting_approval":
            left = deadline - time.monotonic()
            if left <= 0 or call.cancelled.is_set():
                break
            envelope = job.refresh(wait=max(1, min(POLL_S, left)))
            waiting = (envelope.get("waiting") or {}).get("reason")
            call.tell(f"{envelope.get('state')}{': ' + waiting if waiting else ''}")
        return envelope


def render(envelope: Envelope, detail: str | None) -> str:
    """An envelope as a tool answers with it: all of it as JSON, or the few lines ``wb show`` prints, with what to
    call next when the job has not settled."""
    if detail == "full":
        return json.dumps(envelope, indent=1, default=str)
    from .cli import envelope_lines
    job, state = envelope.get("job"), envelope.get("state")
    if state == "awaiting_approval":
        quote = (envelope.get("approval") or {}).get("quote") or {}
        lines = [f"awaiting approval  {envelope.get('command')}  {job}", "  " + _question(envelope).replace("\n", "\n  "),
                 f"  quote: {quote.get('id')}",
                 "  This spends or publishes. Show the person this quote; only on their yes, "
                 f'approve(job="{job}", quote="{quote.get("id")}").']
        return "\n".join(lines)
    lines = envelope_lines(Envelope(envelope), more='detail="full"')
    if state not in TERMINAL:
        lines.append(f'  not settled; the job goes on. job(job="{job}", wait=...) waits for it.')
    elif state == "settled" and envelope.get("outcome") not in ("ok", None):
        lines.append(f'  logs(target="{job}") has its log lines.')
    return _head("\n".join(lines))


def _question(envelope: Envelope) -> str:
    approval = envelope.get("approval") or {}
    quote, prompt = approval.get("quote") or {}, approval.get("prompt") or {}
    lines = [prompt.get("question") or f"Approve {envelope.get('command')} ({envelope.get('job')})?"]
    lines += [f"{key}: {quote[key]}" for key in ("card", "driver", "host", "dph", "cap_minutes", "max_cost", "currency",
                                                 "expires_at") if key in quote]
    return "\n".join(lines)


def _can_ask(capabilities: dict) -> bool:
    """Whether the client declared that it can show the person a form and return their answer."""
    elicitation = capabilities.get("elicitation")
    return isinstance(elicitation, dict) and (not elicitation or "form" in elicitation)


def _check(schema: dict, arguments) -> str | None:
    """What is wrong with a tool's arguments, in words that say how to put it right; None when nothing is."""
    if not isinstance(arguments, dict):
        return "the arguments are an object"
    properties = schema.get("properties", {})
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        return f"no argument named {', '.join(unknown)}; it takes: {', '.join(properties) or 'none'}"
    missing = [name for name in schema.get("required", []) if arguments.get(name) in (None, "")]
    if missing:
        return f"it needs {', '.join(missing)}"
    for name, value in arguments.items():
        kind = properties[name].get("type")
        wrong = value is not None and (not isinstance(value, _TYPES[kind]) or (isinstance(value, bool) and kind in
                                                                                ("number", "integer")))
        if wrong:
            return f"`{name}` is {'an' if kind[0] in 'aio' else 'a'} {kind}"
        if value is not None and "enum" in properties[name] and value not in properties[name]["enum"]:
            return f"`{name}` is one of: {', '.join(properties[name]['enum'])}"
    return None


def _params(params: dict, schema: dict, cwd: str | None) -> tuple[str | None, dict]:
    """A command's parameters as the daemon takes them: a relative path made absolute from ``cwd`` (as the CLI does
    from the folder it runs in), one value for a list parameter made a list. (problem, params)."""
    properties = schema.get("properties", {})
    ready = {}
    for name, value in params.items():
        spec = properties.get(name, {})
        if spec.get("type") == "array" and not isinstance(value, list):
            value = [value]
        formats = (spec.get("format"), (spec.get("items") or {}).get("format"))
        if "path.in" in formats or "path.out" in formats:
            values = value if isinstance(value, list) else [value]
            for index, item in enumerate(values):
                if isinstance(item, str) and item and not os.path.isabs(item) and not item.startswith("/"):
                    if not cwd:
                        return f"`{name}` is a relative path ({item}); give it absolute, or give `cwd`", {}
                    values[index] = str(Path(cwd, item))
            value = values if isinstance(value, list) else values[0]
        ready[name] = value
    return None, ready


def _clock(stamp) -> str:
    """A log line's time of day; a stamp that is not seconds since the epoch is shown as it is."""
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(stamp)))
    except (TypeError, ValueError, OverflowError, OSError):
        return str(stamp)


def _seconds(value, default: float, most: float) -> float:
    if value is None:
        return default
    try:
        seconds = float(value)
    except (OverflowError, ValueError):
        return most
    return max(0.0, min(seconds, most)) if seconds == seconds else 0.0  # not a number: no wait


_SEAL = os.urandom(32)  # this process's own: a question asked by another process is asked again


def _seal(job: str, quote: str) -> str:
    """What goes out with the question about one job's quote, so an answer is known to be to that question."""
    body = json.dumps({"job": job, "quote": quote, "until": int(time.time()) + ASK_S}).encode()
    return base64.urlsafe_b64encode(body).decode("ascii") + "." + hmac.new(_SEAL, body, "sha256").hexdigest()


def _sealed(state, job: str, quote: str) -> bool:
    try:
        text, _, mark = state.partition(".")
        body = base64.urlsafe_b64decode(text.encode("ascii"))
        asked = json.loads(body)
        genuine = hmac.compare_digest(mark, hmac.new(_SEAL, body, "sha256").hexdigest())
        return genuine and asked["job"] == job and asked["quote"] == quote and asked["until"] >= time.time()
    except Exception:  # noqa: BLE001 - whatever is wrong with it, it is not this server's question
        return False


def _head(text: str, limit: int = TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... ({len(text) - limit} more characters not shown)"


def _tail(text: str, limit: int = TEXT_LIMIT) -> str:
    return text if len(text) <= limit else f"... ({len(text) - limit} earlier characters not shown)\n" + text[-limit:]


def run_cli(words: list[str], cwd: str | None, seconds: float, call: Call,
            program: list[str] | None = None) -> tuple[int | None, str]:
    """Runs ``wb <words>`` with the code this server runs, in ``cwd``, with no terminal: (exit code, what it
    printed), the code None when it was stopped at its time or because the call was cancelled. ``program`` is what
    runs in place of this interpreter's ``-m wb``."""
    package = Path(__file__).resolve().parents[1]                     # the folder that holds the wb package
    beside = [package.parents[1] / "core", package.parents[1] / "host"]  # a checkout or a release: the core and host
    paths = [str(package)] + [str(path) for path in beside if path.is_dir()]
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": os.pathsep.join(
        paths + [path for path in [os.environ.get("PYTHONPATH")] if path])}
    own_group = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} \
        if os.name == "nt" else {"start_new_session": True}
    # -P: the folder the verb stands in is not searched for modules, so a wb package there cannot stand in for this one
    process = subprocess.Popen([*(program or [sys.executable, "-P", "-m", "wb"]), *words], cwd=cwd or None,
                               env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, **own_group)
    chunks: list[bytes] = []

    def read():
        for chunk in iter(lambda: process.stdout.read1(65536), b""):
            chunks.append(chunk)

    reader = threading.Thread(target=read, name="verb output", daemon=True)
    reader.start()
    deadline = time.monotonic() + seconds
    stopped = False
    while process.poll() is None:
        if time.monotonic() > deadline or call.cancelled.is_set():
            stopped = True
            _stop(process)
            break
        call.cancelled.wait(0.1)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    reader.join(2)  # a process the verb left running may hold the pipe open: what was printed so far is the answer
    output = b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n").strip()
    return (None if stopped else process.returncode), output


def _stop(process: subprocess.Popen):
    """Ends a verb and everything it started."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()


# -- the protocol ---------------------------------------------------------------------------------------------------
class Server:
    """Answers MCP requests one by one: ``handle`` takes a decoded message and returns the response, or None for
    what takes none. Nothing is kept between requests but the calls still running, so one can be cancelled."""

    def __init__(self, skills: Skills | None = None, tools: Tools | None = None):
        self.skills = skills or Skills()
        self.tools = tools or Tools()
        self._send = lambda message: None
        self._running: dict = {}
        self._withdrawn: dict = {}   # requests cancelled before their call began, by when
        self._opened = False         # whether a client opened with initialize: the revisions before 2026-07-28
        self._lock = threading.Lock()
        self._calls = threading.Lock()

    # -- the transport ---------------------------------------------------------------------------------------------
    def serve(self, source, sink) -> int:
        """Reads one message per line from ``source`` and writes one per line to ``sink`` (both binary) until the
        source ends. Requests are answered each on a thread of its own, so a long wait holds nothing else up."""
        gone = threading.Event()

        def send(message: dict):
            try:  # ASCII only: no character can break the line or fail to encode, whatever a client sent to echo
                data = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"
            except ValueError as error:
                if "id" not in message:
                    return
                data = json.dumps(_failure(message["id"], INTERNAL_ERROR, f"the answer cannot be written: {error}"),
                                  separators=(",", ":")).encode("ascii") + b"\n"
            with self._lock:
                try:
                    sink.write(data)
                    sink.flush()
                except (OSError, ValueError):
                    gone.set()  # the client closed its end: nothing is left to answer

        def answer(message):
            try:
                response = self.handle(message)
            except Exception as error:  # noqa: BLE001 - no message, however made, ends the server
                traceback.print_exc(file=sys.stderr)
                ident = message.get("id") if isinstance(message, dict) else None
                response = None if not isinstance(ident, (str, int)) or isinstance(ident, bool) else \
                    _failure(ident, INTERNAL_ERROR, f"{type(error).__name__}: {error}")
            if response is not None:
                send(response)

        self._send = send
        threads: list[threading.Thread] = []
        for line in iter(source.readline, b""):
            if gone.is_set():
                break
            if not line.strip():
                continue
            try:
                message = json.loads(line, parse_constant=_not_json)
            except (ValueError, RecursionError):
                send(_failure(None, PARSE_ERROR, "the line is not JSON"))
                continue
            if isinstance(message, dict) and "id" in message and "method" in message:
                threads = [thread for thread in threads if thread.is_alive()]
                threads.append(threading.Thread(target=answer, args=(message,), name="request", daemon=True))
                threads[-1].start()
            else:
                answer(message)
        # The input ended. Answers nearly done are still written; then every call still waiting is cancelled, so a
        # verb it runs is stopped with it. Jobs are the daemon's and go on.
        deadline = time.monotonic() + DRAIN_S
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        for call in list(self._running.values()):
            call.cancelled.set()
        deadline = time.monotonic() + 2
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return 0

    # -- one message -----------------------------------------------------------------------------------------------
    def handle(self, message) -> dict | None:
        if not isinstance(message, dict):
            return _failure(None, INVALID_REQUEST, "a message is one JSON object; batches are not taken")
        if "method" not in message:
            return None  # a response: this server sends no requests, so there is none to match
        method, params = message["method"], message.get("params")
        params = params if isinstance(params, dict) else {}
        if "id" not in message:
            ident = params.get("requestId")
            if method == "notifications/cancelled" and isinstance(ident, (str, int)) and not isinstance(ident, bool):
                with self._calls:
                    call = self._running.get(_key(ident))
                    if call is not None:
                        call.cancelled.set()
                    else:  # its call has not begun yet, or has ended: remembered for a moment, in case it begins
                        if len(self._withdrawn) > 256:
                            self._withdrawn.clear()
                        self._withdrawn[_key(ident)] = time.monotonic()
            return None
        ident = message["id"]
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str) or isinstance(ident, bool) \
                or not isinstance(ident, (str, int)):
            return _failure(ident if isinstance(ident, (str, int)) else None, INVALID_REQUEST,
                            "not a JSON-RPC 2.0 request with a string or integer id")
        call = Call()
        try:
            result = self._request(ident, method, params, call)
        except Refusal as refusal:
            return _failure(ident, refusal.code, str(refusal), refusal.data)
        except Exception as error:  # noqa: BLE001 - one request's failure must not end the server
            traceback.print_exc(file=sys.stderr)
            return _failure(ident, INTERNAL_ERROR, f"{type(error).__name__}: {error}")
        if call.cancelled.is_set():
            return None  # the client withdrew the request: nothing more is sent for it
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def _request(self, ident, method: str, params: dict, call: Call) -> dict:
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        version = meta.get(META_VERSION)
        call.modern = version is not None
        if call.modern:
            if version not in MODERN:
                raise Refusal(UNSUPPORTED_VERSION, "Unsupported protocol version",
                              {"supported": list(MODERN), "requested": version})
            if not isinstance(meta.get(META_CAPABILITIES), dict):
                raise Refusal(INVALID_PARAMS, f"the request's _meta has no {META_CAPABILITIES}")
            call.capabilities = meta[META_CAPABILITIES]
            _name_caller(meta.get(META_CLIENT))
        elif method == "server/discover" or (method not in ("initialize", "ping") and not self._opened):
            raise Refusal(INVALID_PARAMS, f"the request names no version: put {META_VERSION} ({', '.join(MODERN)}) "
                                          f"and {META_CAPABILITIES} in params._meta, or open with the initialize "
                                          f"handshake of {', '.join(LEGACY)}")

        def done(payload: dict, ttl_ms: int | None = None) -> dict:
            if not call.modern:
                return payload
            cached = {} if ttl_ms is None else {"ttlMs": ttl_ms, "cacheScope": "public"}
            return {"resultType": "complete", **payload, **cached, "_meta": {META_SERVER: SERVER}}

        if method == "server/discover":
            return done({"supportedVersions": list(MODERN), "capabilities": self.capabilities(True),
                         "instructions": INSTRUCTIONS}, STATIC_TTL_MS)
        if method == "initialize":
            asked = params.get("protocolVersion")
            self._opened = True
            _name_caller(params.get("clientInfo"))
            return {"protocolVersion": asked if asked in LEGACY else LEGACY[0],
                    "capabilities": self.capabilities(False), "serverInfo": SERVER, "instructions": INSTRUCTIONS}
        if method == "ping":
            return done({})
        if method == "tools/list":
            return done({"tools": TOOLS}, STATIC_TTL_MS)
        if method == "tools/call":
            return self._call(ident, params, meta, call, done)
        if method == "resources/list":
            return done({"resources": self._resources()}, SKILLS_TTL_MS)
        if method == "resources/templates/list":
            return done({"resourceTemplates": []}, STATIC_TTL_MS)
        if method == "resources/read":
            item = self.skills.read(params.get("uri"))
            if item is None:
                raise Refusal(INVALID_PARAMS if call.modern else RESOURCE_NOT_FOUND, "Resource not found",
                              {"uri": params.get("uri")})
            return done({"contents": [item]}, SKILLS_TTL_MS)
        if method == "resources/directory/read":
            children = self.skills.children(params.get("uri"))
            if children is None:
                raise Refusal(INVALID_PARAMS, f"{params.get('uri')} is not a directory resource")
            return done({"resources": children}, SKILLS_TTL_MS)
        if method == "skills/list":
            return done({"skills": self.skills.entries()}, SKILLS_TTL_MS)
        if method == "skills/get":
            where = self.skills.locate(params.get("uri"))
            if where is None or where[1] != SKILL_FILE:
                raise Refusal(INVALID_PARAMS, f"No skill is served at {params.get('uri')}")
            try:
                return done({"skill": self.skills.entry(where[0])}, SKILLS_TTL_MS)
            except (SkillError, OSError) as error:  # a folder that is no skill as it stands: none is served there
                raise Refusal(INVALID_PARAMS, f"No skill is served at {params.get('uri')}: {error}") from None
        raise Refusal(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _call(self, ident, params: dict, meta: dict, call: Call, done) -> dict:
        arguments = params.get("arguments")
        responses, state = params.get("inputResponses"), params.get("requestState")
        call.responses = responses if isinstance(responses, dict) else {}
        call.state = state if isinstance(state, str) else None
        token = meta.get("progressToken")
        if token is not None:
            beats = iter(range(1, 1 << 30))
            call.progress = lambda text: self._send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {
                "progressToken": token, "progress": next(beats), "message": text}})
        with self._calls:
            self._running[_key(ident)] = call
            if time.monotonic() - self._withdrawn.pop(_key(ident), float("-inf")) < 2:
                call.cancelled.set()
        try:
            outcome = self.tools.call(params.get("name"), {} if arguments is None else arguments, call)
        finally:
            with self._calls:
                self._running.pop(_key(ident), None)
        if isinstance(outcome, Ask):
            return {"resultType": "input_required", "inputRequests": outcome.requests,
                    **({"requestState": outcome.state} if outcome.state else {}), "_meta": {META_SERVER: SERVER}}
        return done({"content": [{"type": "text", "text": outcome.text}], "isError": outcome.error})

    def capabilities(self, modern: bool) -> dict:
        declared: dict = {"tools": {}, "resources": {}}
        if modern:  # extensions are named in capabilities from 2026-07-28 on
            declared["extensions"] = {SKILLS_EXTENSION: {"directoryRead": True}}
        return declared

    def _resources(self) -> list[dict]:
        """Every skill file as a resource, so that a client without the Skills extension can still read them."""
        listed = []
        for entry in self.skills.entries():
            name = entry["frontmatter"]["name"]
            for item in entry["resources"]:
                relative = item["uri"][len(self.skills.uri(name, "")) + 1:]
                first = relative == SKILL_FILE
                listed.append({"uri": item["uri"], "name": name if first else f"{name}/{relative}",
                               "mimeType": _mime(Path(relative)), "size": item["size"],
                               **({"description": entry["frontmatter"]["description"]} if first else {})})
        return listed


def _key(ident):
    return (type(ident).__name__, ident)


def _not_json(name: str):
    raise ValueError(f"{name} is not JSON")


def _failure(ident, code: int, message: str, data=None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": ident, "error": error}


def _name_caller(client):
    """Jobs this server submits carry the client's own name as their caller, unless the caller was set outside."""
    name = client.get("name") if isinstance(client, dict) else None
    if isinstance(name, str) and os.environ.get("WB_CALLER", "mcp") == "mcp":
        label = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:40]
        if label:
            os.environ["WB_CALLER"] = "mcp:" + label


# -- the verb -------------------------------------------------------------------------------------------------------
def client_config(config) -> dict:
    """The entry an MCP client's config takes to start this server from the release in service: the link, so the
    entry stays right through every deploy."""
    from . import releases
    if os.name == "nt":
        entry = {"command": "cmd.exe", "args": ["/c", str(releases.current(config) / "bin" / "wb.cmd"), "mcp"]}
    else:
        entry = {"command": str(releases.current(config) / "bin" / "wb"), "args": ["mcp"]}
    return {"mcpServers": {"wb": entry}}


def main(args) -> int:
    if getattr(args, "config", False):
        from . import config as wbconfig
        print(json.dumps(client_config(wbconfig.load()), indent=1))
        return 0
    if getattr(args, "list", False):
        skills = Skills().entries()
        if args.json:
            print(json.dumps({"tools": TOOLS, "skills": skills}, indent=1))
            return 0
        print("tools:")
        for tool in TOOLS:
            print(f"  {tool['name']:<9} {tool['description']}")
        print("skills:")
        for entry in skills:
            size = sum(item["size"] for item in entry["resources"])
            print(f"  {entry['frontmatter']['name']:<9} {len(entry['resources'])} file(s), {size} bytes  "
                  f"{entry['frontmatter']['description']}")
        return 0
    source, sink = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # stdout is the protocol's: a stray print must not land in it
    os.environ.setdefault("WB_CALLER", "mcp")
    return Server().serve(source, sink)
