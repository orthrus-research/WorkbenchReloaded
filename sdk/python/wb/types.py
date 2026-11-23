"""The wb/result/v1 envelope as TypedDicts (generated from schema/envelope.schema.json by schema/generate.py; do not edit)."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class EnvelopeDict(TypedDict, total=False):
    """wb/result/v1: the one shape of every job, at every state. code equals the CLI exit code once settled."""
    schema: str
    job: str
    command: str
    command_version: str | None
    module: str
    module_version: str | None
    params: dict
    params_hash: str | None
    state: Literal["new", "queued", "awaiting_approval", "running", "settled", "cancelled", "interrupted"]
    phase: str | None
    waiting: dict | None
    outcome: Literal["ok", "finding", "usage", "environment"] | None
    code: int | None
    summary: str | None
    reasons: list
    inputs: list
    outputs: list
    data: dict
    measurements: list
    findings: list[str]
    machine: dict
    toolchain: dict
    timings: dict
    cost: dict | None
    cache: dict
    approval: dict | None
    children: list[str]
    parent: str | None
    logs: dict


SCHEMA_ID = "https://orthrus.cc/schemas/wb/result/v1"
FIELDS = ['approval', 'cache', 'children', 'code', 'command', 'command_version', 'cost', 'data', 'findings', 'inputs', 'job', 'logs', 'machine', 'measurements', 'module', 'module_version', 'outcome', 'outputs', 'params', 'params_hash', 'parent', 'phase', 'reasons', 'schema', 'state', 'summary', 'timings', 'toolchain', 'waiting']
