"""glslangValidator as render.validate runs it: one stage compiled, or one program's stages linked."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem

TOOL = "glslangValidator"
MESSAGE = re.compile(r"^(ERROR|WARNING):\s*(?:.*?:(\d+):\s+)?(.*?)\s*$")
NOISE = re.compile(r"^(?:\d+ compilation errors?\.(?:\s*No code generated\.?)?|''\s*:\s*compilation terminated)$")


def executable() -> str:
    found = shutil.which(TOOL)
    if not found:
        raise EnvironmentProblem(f"{TOOL} is not on PATH (it ships with glslang-tools or the Vulkan SDK)")
    return found


def version(ctx: Context, tool: str) -> str:
    done = ctx.run([tool, "--version"], log_output=False, timeout=60)
    return done.stdout.splitlines()[0].strip() if done.stdout.strip() else "unknown"


def messages(log: str) -> list[dict]:
    """The errors and warnings of a glslangValidator log: level, line (in the file it read, when given), text, and
    the indented lines that follow it (a link error's detail)."""
    out: list[dict] = []
    for raw in log.splitlines():
        match = MESSAGE.match(raw.strip())
        if not match:
            if out and raw[:1].isspace() and raw.strip():
                out[-1]["detail"] = (out[-1].get("detail", "") + " " + raw.strip()).strip()
            continue
        text = match[3]
        if NOISE.match(text):
            continue
        out.append({"level": match[1].lower(), "line": int(match[2]) if match[2] else None, "text": text})
    return out


def compile_stage(ctx: Context, tool: str, path: Path) -> tuple[bool, str]:
    done = ctx.run([tool, str(path)], log_output=False, timeout=300)
    return done.returncode == 0, done.stdout


def link(ctx: Context, tool: str, paths: list[Path]) -> tuple[bool, str]:
    done = ctx.run([tool, "-l", *[str(p) for p in paths]], log_output=False, timeout=300)
    return done.returncode == 0, done.stdout
