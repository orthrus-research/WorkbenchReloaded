"""One job in its own process (spec S6): ``python -m wb_core.worker``.

The daemon (or the host runner) writes the job spec as one JSON line on stdin and closes it. To cancel,
it creates a ``cancel`` file in the job folder; the worker sees it, interrupts the handler, and the
handler's ``finally`` blocks run (a rental is destroyed). No thread ever blocks reading stdin: on Windows,
synchronous I/O on a pipe handle is serialised, and a pending read there hangs NumPy's DLL start-up.
Events go out as JSON lines on the original stdout; fd 1 is pointed at stderr before the handler runs,
so anything the handler or its tools print lands in the job log instead of the event stream.

Modes: ``run`` (the handler), ``quote`` (a spends command's quote handler), ``startup`` (a module's
startup hook).
"""

from __future__ import annotations

import _thread
import importlib
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path


def _channel():
    out_fd = os.dup(1)
    os.dup2(2, 1)
    stream = os.fdopen(out_fd, "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr
    lock = threading.Lock()

    def emit(event: dict):
        line = json.dumps(event, separators=(",", ":"), default=str)
        with lock:
            stream.write(line + "\n")
            stream.flush()
    return emit


def _load(reference: str):
    module_name, _, attribute = reference.partition(":")
    module = importlib.import_module(module_name)
    target = module
    for part in attribute.split("."):
        target = getattr(target, part)
    return target


def main() -> int:
    first = sys.stdin.readline()
    if not first:
        return 2
    spec = json.loads(first)
    for path in spec.get("sys_path", []):
        if path not in sys.path:
            sys.path.insert(0, path)
    os.environ.update(spec.get("env", {}))
    emit = _channel()

    from wb_core import process as processes
    from wb_core.module import Cancelled, Context, EnvironmentProblem, ParamError, QuoteChanged, TimedOut

    ctx = Context(spec, emit)
    try:
        sys.stdin.close()  # nothing more comes this way; see the module docstring
    except OSError:
        pass
    cancel_file = Path(spec["work"]).parent / "cancel"

    def control():
        while not cancel_file.exists():
            time.sleep(0.25)
        ctx.cancelled.set()
        emit({"kind": "log", "ts": time.time(), "level": "WARNING", "msg": "cancel requested"})
        _thread.interrupt_main()
    threading.Thread(target=control, daemon=True).start()

    owner = processes.own_tree()  # everything the handler starts dies with this job
    emit({"kind": "log", "ts": time.time(), "level": "DEBUG", "msg": f"process ownership: {owner}"})
    try:
        return _handle(spec, ctx, emit, Cancelled, EnvironmentProblem, ParamError, QuoteChanged, TimedOut)
    finally:
        killed = processes.kill_descendants()
        if killed:
            emit({"kind": "log", "ts": time.time(), "level": "WARNING",
                  "msg": f"stopped {killed} process(es) the job left running"})


def _handle(spec, ctx, emit, Cancelled, EnvironmentProblem, ParamError, QuoteChanged, TimedOut) -> int:
    mode = spec.get("mode", "run")
    started = time.time()
    try:
        handler = _load(spec["handler"])
        if mode == "startup":
            result = handler(ctx)
        else:
            result = handler(ctx, **ctx.params)
        if mode == "quote":
            emit({"kind": "quote", "ts": time.time(), "quote": result})
            emit({"kind": "done", "ts": time.time(), "outcome": "ok", "summary": None, "data": {}})
            return 0
        if result is not None and not isinstance(result, dict):
            raise TypeError(f"handler returned {type(result).__name__}; it must return a dict or None")
        emit({"kind": "done", "ts": time.time(), "outcome": ctx.outcome, "summary": ctx.summary_text,
              "data": result or {}, "reasons": ctx.reasons, "seconds": round(time.time() - started, 3)})
        return 0
    except QuoteChanged as changed:
        emit({"kind": "requote", "ts": time.time(), "quote": changed.quote, "reason": changed.reason})
        return 0
    except (KeyboardInterrupt, Cancelled):
        emit({"kind": "done", "ts": time.time(), "outcome": "cancelled", "summary": "cancelled", "data": {}})
        return 0
    except TimedOut as error:
        emit({"kind": "done", "ts": time.time(), "outcome": "finding", "summary": str(error),
              "data": {"timed_out": True}, "reasons": [{"kind": "timeout", "name": spec.get("command", ""),
                                                        "detail": str(error), "fix": None}]})
        return 0
    except ParamError as error:
        emit({"kind": "done", "ts": time.time(), "outcome": "usage", "summary": str(error), "data": {}})
        return 0
    except EnvironmentProblem as error:
        emit({"kind": "done", "ts": time.time(), "outcome": "environment", "summary": str(error), "data": {},
              "reasons": ctx.reasons})
        return 0
    except BaseException as error:  # noqa: BLE001 - every failure becomes an envelope
        sys.stderr.write(traceback.format_exc())
        sys.stderr.flush()
        emit({"kind": "done", "ts": time.time(), "outcome": "environment",
              "summary": f"{type(error).__name__}: {error}", "data": {}, "traceback": True})
        return 0


if __name__ == "__main__":
    code = main()
    # Exit at once, without waiting on the control thread or interpreter shutdown; the event stream and
    # stderr are flushed first.
    for stream in (sys.stderr, sys.__stdout__):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)
