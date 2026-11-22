"""drive.send: one request to a running client's drive agent (``mods/drive-agent``) through its inbox, and its answer.

The agent reads JSON requests from its inbox folder (``wb.driver.inbox``) on the game's own thread and writes each
answer to its outbox (``wb.driver.outbox``) under the request's id. A request is written under another name and
renamed in, so the agent never reads half of one. An answer the agent marks ``ok: false`` is a finding, and so is no
answer within ``timeout`` (the request is then taken back, so it cannot run later by surprise).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from wb_core.module import Context, ParamError

ACTIONS = ("status", "screenshot", "chat", "key", "click", "close", "quit")


def default_outbox(inbox: Path) -> Path:
    if "inbox" not in inbox.name:
        raise ParamError(f"outbox: name it; {inbox.name} does not say where the answers go")
    return inbox.with_name(inbox.name.replace("inbox", "outbox"))


def send(ctx: Context, inbox: str, action: str, args: dict, timeout: float, outbox: str | None = None):
    if action not in ACTIONS:
        raise ParamError(f"action: {action!r} is none of {', '.join(ACTIONS)}")
    if "action" in args:
        raise ParamError("args: the action is given by action=, not inside args")
    inbox_path = Path(inbox)
    outbox_path = Path(outbox) if outbox else default_outbox(inbox_path)
    inbox_path.mkdir(parents=True, exist_ok=True)
    rid = "wb-" + "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in ctx.job)
    part, request, answer = inbox_path / f"{rid}.part", inbox_path / f"{rid}.json", outbox_path / f"{rid}.json"
    answer.unlink(missing_ok=True)
    part.write_text(json.dumps({"action": action, **args}), encoding="utf-8")
    os.replace(part, request)
    ctx.progress("answer", msg=f"{action} sent to {ctx.host_path(inbox_path)}")
    deadline = time.monotonic() + timeout
    while not answer.is_file():
        ctx.check_cancel()
        if time.monotonic() >= deadline:
            request.unlink(missing_ok=True)
            ctx.finding(f"no answer to {action} within {timeout:g} s: is the client running with -Dwb.driver=true, "
                        f"and its outbox {ctx.host_path(outbox_path)}?")
            return {"answer": None}
        time.sleep(0.2)
    reply = json.loads(answer.read_text(encoding="utf-8"))
    answer.unlink(missing_ok=True)
    if not reply.get("ok"):
        ctx.finding(f"the agent refused {action}: {reply.get('error')}")
    else:
        ctx.summary(f"{action}: " + json.dumps(reply.get("result") or {})[:300])
    return {"answer": reply}
