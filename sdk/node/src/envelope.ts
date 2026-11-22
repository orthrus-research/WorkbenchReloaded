// generated from schema/envelope.schema.json by schema/generate.py; do not edit

/** wb/result/v1: the one shape of every job, at every state. code equals the CLI exit code once settled. */
export interface Envelope {
  "schema": "wb/result/v1";
  "job": string;
  "command": string;
  "command_version"?: string | null;
  "module"?: string;
  "module_version"?: string | null;
  "params": Record<string, unknown>;
  "params_hash"?: string | null;
  "state": "new" | "queued" | "awaiting_approval" | "running" | "settled" | "cancelled" | "interrupted";
  "phase"?: string | null;
  "outcome"?: "ok" | "finding" | "usage" | "environment" | null;
  "code"?: 0 | 1 | 2 | 3 | null;
  "summary"?: string | null;
  "reasons"?: Array<{ "kind"?: string; "name"?: string; "detail"?: string; "fix"?: string; [key: string]: unknown }>;
  "inputs"?: Array<{ "name"?: string; "host_path"?: string; "sha256"?: string; "files"?: number; [key: string]: unknown }>;
  "outputs": Array<{ "name"?: string; "host_path"?: string; "sha256"?: string; "bytes"?: number; "media_type"?: string | null; "url"?: string; [key: string]: unknown }>;
  "data": Record<string, unknown>;
  "measurements": Array<{ "name"?: string; "value"?: unknown; "unit"?: string | null; "labels"?: Record<string, unknown> | null; [key: string]: unknown }>;
  "findings"?: Array<string>;
  "machine"?: { "id"?: string; [key: string]: unknown };
  "toolchain"?: Record<string, unknown>;
  "timings": { "submitted"?: string | null; "started"?: string | null; "settled"?: string | null; "phases"?: Record<string, unknown>; [key: string]: unknown };
  "cost"?: { "amount"?: number; "currency"?: string; [key: string]: unknown } | null;
  "cache"?: { "hit"?: boolean; "from"?: string | null; "key"?: string | null; [key: string]: unknown };
  "approval"?: { "quote"?: { "id"?: string; "expires_at"?: string; [key: string]: unknown }; "prompt"?: { "question"?: string; "options"?: Array<{ "label"?: string; "description"?: string; [key: string]: unknown }>; [key: string]: unknown }; "status"?: string; "approved"?: Record<string, unknown> | null; [key: string]: unknown } | null;
  "children"?: Array<string>;
  "parent"?: string | null;
  "logs"?: Record<string, unknown>;
}

export const SCHEMA_ID = "https://orthrus.cc/schemas/wb/result/v1";
