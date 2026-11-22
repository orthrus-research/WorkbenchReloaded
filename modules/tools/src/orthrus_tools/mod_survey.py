"""Survey how Minecraft 1.12.2 mods touch OpenGL state and the game's rendering classes.

Reads each JAR's class files and lists, from their constant pools, which methods they call
and which rendering classes they patch. Nothing is executed and no mod code is read as
source: the survey sees only the names a class refers to. JARs nested inside a mod (for
example under ``META-INF/jarjar``) are surveyed as part of that mod.

    orthrus-mod-survey MODS_DIR_OR_JAR [--out SURVEY.json] [--mod NAME ...] [--json]

``--out`` writes the full per-mod result; ``--json`` prints the summary as one JSON object.

A reference is not proof of use at runtime, and a call's arguments are not visible: a mod
that calls glDepthFunc is listed whether it passes the game's usual comparison or another.
"""

from __future__ import annotations

import argparse
import io
import json
import struct
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import classfile, cli
from .cli import OK, USAGE, Output, ToolError

GL = "org/lwjgl/opengl/"
STATE = "net/minecraft/client/renderer/GlStateManager"
HELPER = "net/minecraft/client/renderer/OpenGlHelper"

# category -> list of (owner class or prefix, method names). An owner ending in "/" is a prefix.
CALLS: dict[str, list[tuple[str, set[str]]]] = {
    "depth function, direct": [(GL, {"glDepthFunc"})],
    "depth function, through the game": [(STATE, {"func_179143_c", "depthFunc"})],
    "depth mask, direct": [(GL, {"glDepthMask"})],
    "depth range": [(GL, {"glDepthRange"})],
    "clear depth value": [(GL, {"glClearDepth"}), (STATE, {"func_179151_a", "clearDepth"})],
    "polygon offset, direct": [(GL, {"glPolygonOffset"})],
    "polygon offset, through the game": [(STATE, {"func_179136_a", "doPolygonOffset"})],
    "enable or disable, direct": [(GL, {"glEnable", "glDisable"})],
    "blend function, direct": [(GL, {"glBlendFunc", "glBlendFuncSeparate"})],
    "alpha function, direct": [(GL, {"glAlphaFunc"})],
    "attribute stack": [
        (GL, {"glPushAttrib", "glPopAttrib"}),
        (STATE, {"func_179123_a", "pushAttrib"}),
    ],
    "display lists": [(GL, {"glNewList", "glCallList", "glGenLists"})],
    "immediate mode": [(GL, {"glBegin"})],
    "read pixels": [(GL, {"glReadPixels"})],
    "copy framebuffer to texture": [(GL, {"glCopyTexImage2D", "glCopyTexSubImage2D"})],
    "stencil": [(GL, {"glStencilFunc", "glStencilOp", "glStencilMask"})],
    "own shader programs": [
        (GL, {"glUseProgram", "glUseProgramObjectARB"}),
        (HELPER, {"func_153161_d", "glUseProgram"}),
    ],
    "own framebuffers": [
        (GL, {"glBindFramebuffer", "glBindFramebufferEXT"}),
        (HELPER, {"func_153171_g", "glBindFramebuffer"}),
    ],
    "vertex array objects": [(GL, {"glBindVertexArray"})],
    "generic vertex attributes": [(GL, {"glEnableVertexAttribArray", "glVertexAttribPointer"})],
    "buffer objects, direct": [(GL, {"glBindBuffer", "glBindBufferARB"})],
    "matrices, direct": [
        (
            GL,
            {
                "glLoadMatrix",
                "glLoadMatrixf",
                "glMultMatrix",
                "glMultMatrixf",
                "glMatrixMode",
                "glOrtho",
                "glFrustum",
                "glLoadIdentity",
            },
        ),
        ("org/lwjgl/util/glu/", {"gluPerspective"}),
    ],
    "pixel store": [(GL, {"glPixelStorei"})],
    "scissor or viewport, direct": [(GL, {"glScissor", "glViewport"})],
}

# Rendering classes whose patching matters to a renderer, by internal name.
TARGETS = {
    "EntityRenderer": "net/minecraft/client/renderer/EntityRenderer",
    "RenderGlobal": "net/minecraft/client/renderer/RenderGlobal",
    "RenderChunk": "net/minecraft/client/renderer/chunk/RenderChunk",
    "BlockRendererDispatcher": "net/minecraft/client/renderer/BlockRendererDispatcher",
    "BlockModelRenderer": "net/minecraft/client/renderer/BlockModelRenderer",
    "BlockFluidRenderer": "net/minecraft/client/renderer/BlockFluidRenderer",
    "BufferBuilder": "net/minecraft/client/renderer/BufferBuilder",
    "GlStateManager": STATE,
    "TextureMap": "net/minecraft/client/renderer/texture/TextureMap",
    "ChunkCache": "net/minecraft/world/ChunkCache",
    "ForgeHooksClient": "net/minecraftforge/client/ForgeHooksClient",
}
MIXIN = "Lorg/spongepowered/asm/mixin/Mixin;"
TRANSFORMER = "net/minecraft/launchwrapper/IClassTransformer"

# Calls that can move depth out of the depth buffer, and the constants that say they do.
DEPTH_CALLS = {
    "reads depth pixels": {"glReadPixels"},
    "copies depth to a texture": {"glCopyTexImage2D", "glCopyTexSubImage2D"},
    "creates a depth texture": {"glTexImage2D", "glTexStorage2D"},
    "attaches a depth texture": {
        "glFramebufferTexture2D",
        "glFramebufferTexture2DEXT",
        "glFramebufferTexture",
    },
}
# DEPTH_COMPONENT, DEPTH_STENCIL, the sized depth formats and the depth attachment points.
DEPTH_CONSTANTS = {0x1902, 0x84F9, 0x81A5, 0x81A6, 0x81A7, 0x8CAC, 0x8CAD, 0x88F0, 0x8D00, 0x821A}
# invokevirtual, invokestatic, invokeinterface
_INVOKES = (b"\xb6", b"\xb8", b"\xb9")
# Nested JARs deeper than this are not opened: a guard against archive bombs.
MAX_NESTING = 3


def calls_of(parsed: classfile.ClassFile) -> set[str]:
    methods = parsed.methods
    found = set()
    for category, owners in CALLS.items():
        for owner, names in owners:
            prefix = owner.endswith("/")
            if any(
                (o == owner or (prefix and o.startswith(owner))) and n in names for o, n in methods
            ):
                found.add(category)
                break
    return found


def patches_of(parsed: classfile.ClassFile) -> dict[str, str]:
    """Rendering classes a Mixin or class transformer names, with which kind of patch it is."""
    strings, class_names = parsed.strings, parsed.class_names
    is_mixin = MIXIN in strings
    if not is_mixin and TRANSFORMER not in class_names:
        return {}
    kind = "Mixin" if is_mixin else "transformer"
    found = {}
    for label, internal in TARGETS.items():
        dotted = internal.replace("/", ".")
        if (
            internal in class_names
            or internal in strings
            or dotted in strings
            or ("L" + internal + ";") in strings
        ):
            found[label] = kind
    return found


def depth_reads(parsed: classfile.ClassFile) -> set[str]:
    """Categories of DEPTH_CALLS for which one method both makes the call and names a depth constant.

    A heuristic over raw bytecode: it looks for the call's and the constant's instruction bytes in
    the same method body without decoding instructions, so it can report a pair that is unrelated.
    """
    calls: dict[str, list[bytes]] = defaultdict(list)
    for pool_index in parsed.member_refs:
        owner, name = parsed.member(pool_index)
        if "/opengl/" not in owner and owner != HELPER:
            continue
        for category, names in DEPTH_CALLS.items():
            if name in names:
                operand = struct.pack(">H", pool_index)
                calls[category] += [invoke + operand for invoke in _INVOKES]
    if not calls:
        return set()
    # Instruction bytes that push a depth constant: sipush for small values, ldc for the rest.
    pushes = [b"\x11" + struct.pack(">h", c) for c in DEPTH_CONSTANTS if c <= 0x7FFF]
    for pool_index, value in parsed.integers.items():
        if value in DEPTH_CONSTANTS:
            pushes.append(b"\x13" + struct.pack(">H", pool_index))
            if pool_index < 256:
                pushes.append(b"\x12" + bytes([pool_index]))
    found = set()
    for _, body in parsed.method_bodies:
        if any(push in body for push in pushes):
            for category, patterns in calls.items():
                if any(pattern in body for pattern in patterns):
                    found.add(category)
    return found


def _survey_archive(
    archive: zipfile.ZipFile, result: dict[str, Any], prefix: str, depth: int
) -> None:
    for entry in archive.infolist():
        name = entry.filename
        if name.endswith(".class"):
            result["classes"] += 1
            parsed = classfile.parse(archive.read(entry))
            if not parsed.complete:
                result["unreadable"] += 1
            class_name = prefix + name[:-6]
            for category in calls_of(parsed):
                result["calls"][category] += 1
            for label, kind in patches_of(parsed).items():
                result["patches"][label].add(kind)
            for category in depth_reads(parsed):
                result["depth"][category].add(class_name)
        elif name.endswith(".jar") and depth < MAX_NESTING:
            try:
                with zipfile.ZipFile(io.BytesIO(archive.read(entry))) as inner:
                    result["nested"].append(prefix + name)
                    _survey_archive(inner, result, prefix + name + "!/", depth + 1)
            except zipfile.BadZipFile:
                continue


def survey_jar(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "classes": 0,
        "unreadable": 0,
        "calls": defaultdict(int),
        "patches": defaultdict(set),
        "depth": defaultdict(set),
        "nested": [],
    }
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as error:
        return {"calls": {}, "patches": {}, "classes": 0, "depth": {}, "error": str(error)}
    with archive:
        _survey_archive(archive, result, "", 0)
    return {
        "classes": result["classes"],
        "unreadable": result["unreadable"],
        "calls": dict(result["calls"]),
        "patches": {k: sorted(v) for k, v in result["patches"].items()},
        "depth": {k: sorted(v) for k, v in result["depth"].items()},
        "nested": result["nested"],
    }


def find_jars(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(target.rglob("*.jar"))


def summarize(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Which mods fall in each category; the compact form of a survey."""
    return {
        "jars": len(results),
        "classes": sum(r["classes"] for r in results.values()),
        "unreadable": sorted(name for name, r in results.items() if "error" in r),
        "calls": {
            category: sorted(name for name, r in results.items() if category in r["calls"])
            for category in CALLS
        },
        "patches": {
            label: sorted(name for name, r in results.items() if label in r["patches"])
            for label in TARGETS
        },
        "depth": {
            category: {
                name: sorted(c.rsplit("/", 1)[-1] for c in r["depth"][category])
                for name, r in sorted(results.items())
                if category in r["depth"]
            }
            for category in DEPTH_CALLS
        },
    }


def summary_lines(summary: dict[str, Any]) -> list[str]:
    lines = [f"{summary['jars']} JARs, {summary['classes']} classes"]
    if summary["unreadable"]:
        lines.append("Unreadable archives: " + ", ".join(summary["unreadable"]))
    lines.append("\nCalls (mods with at least one class that makes the call):")
    for category, mods in summary["calls"].items():
        lines.append(f"  {len(mods):4d}  {category}")
    lines.append(
        "\nPatches to rendering classes (mods with a Mixin or transformer naming the class):"
    )
    for label, mods in summary["patches"].items():
        lines.append(f"  {len(mods):4d}  {label}: " + ", ".join(mods))
    lines.append(
        "\nDepth leaving the depth buffer (a method makes the call and names a depth constant):"
    )
    for category, mods in summary["depth"].items():
        lines.append(f"  {category}:")
        for name, classes in mods.items():
            lines.append(f"    {name}: {', '.join(classes)}")
    return lines


def build_parser() -> cli.Parser:
    parser = cli.parser(
        "orthrus-mod-survey",
        (__doc__ or "").splitlines()[0],
    )
    parser.add_argument("mods", type=Path, help="a mods directory (searched recursively) or a JAR")
    parser.add_argument(
        "--out", type=Path, metavar="FILE", help="write the full per-mod result here"
    )
    parser.add_argument(
        "--mod",
        action="append",
        default=[],
        metavar="NAME",
        help="only JARs whose file name contains this",
    )
    return parser


def run(args: argparse.Namespace, out: Output) -> int:
    if not args.mods.exists():
        raise ToolError(USAGE, f"No such file or directory: {args.mods}")
    jars = [
        jar for jar in find_jars(args.mods) if not args.mod or any(m in jar.name for m in args.mod)
    ]
    if not jars:
        raise ToolError(USAGE, f"No JAR files under {args.mods}")
    results = {jar.name: survey_jar(jar) for jar in jars}
    summary = summarize(results)
    out.result = summary
    for line in summary_lines(summary):
        out.line(line)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
        summary["out"] = str(args.out)
    return OK


def main(argv: list[str] | None = None) -> int:
    return cli.run("mod-survey", build_parser, run, argv)


if __name__ == "__main__":
    sys.exit(main())
