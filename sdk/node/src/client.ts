// Workbench Reloaded client for Node (D24): token, submit, long-poll, envelope. No dependencies; Node runs this
// TypeScript directly (type stripping). The endpoint and token come from WB_URL and WB_TOKEN (or WB_TOKEN_FILE),
// else from the run folder `wb up` writes: ~/.config/wb/run/endpoint.json and ~/.config/wb/run/token.

import { randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { Envelope } from "./envelope.ts";

export type { Envelope } from "./envelope.ts";

const TERMINAL = new Set(["settled", "cancelled", "interrupted"]);

export class WbError extends Error {
  readonly status: number | undefined;
  readonly body: Record<string, unknown>;
  constructor(message: string, status?: number, body: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

/** A command that spends money waits for an explicit yes on one quote; show `prompt` to the user, then approve. */
export class ApprovalRequired extends Error {
  readonly envelope: Envelope;
  readonly quote: Record<string, unknown>;
  readonly prompt: { question?: string; options?: Array<{ label: string; description: string }> };
  constructor(envelope: Envelope) {
    const approval = (envelope.approval ?? {}) as Record<string, any>;
    super(approval.prompt?.question ?? `job ${envelope.job} is awaiting approval`);
    this.envelope = envelope;
    this.quote = approval.quote ?? {};
    this.prompt = approval.prompt ?? {};
  }
}

function runFolder(): string {
  return join(process.env.WB_CONFIG ?? join(homedir(), ".config", "wb"), "run");
}

export function discover(): { url: string; token?: string } {
  let url = process.env.WB_URL;
  let token = process.env.WB_TOKEN;
  if (!url) {
    const endpoint = join(runFolder(), "endpoint.json");
    if (existsSync(endpoint)) url = JSON.parse(readFileSync(endpoint, "utf8")).url;
  }
  if (!token) {
    const file = process.env.WB_TOKEN_FILE ?? join(runFolder(), "token");
    if (existsSync(file)) token = readFileSync(file, "ascii").trim();
  }
  return { url: (url ?? "http://127.0.0.1:7377").replace(/\/$/, ""), token };
}

export class Client {
  readonly url: string;
  readonly token?: string;
  pathStyle: "windows" | "posix" = process.platform === "win32" ? "windows" : "posix";

  constructor(url?: string, token?: string) {
    const found = discover();
    this.url = (url ?? found.url).replace(/\/$/, "");
    this.token = token ?? found.token;
  }

  async request<T = any>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
    const response = await fetch(this.url + path, {
      method,
      headers: {
        Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).catch((error) => {
      throw new WbError(`Workbench is not reachable at ${this.url}: ${error}`);
    });
    const text = await response.text();
    const payload = text ? JSON.parse(text) : {};
    if (!response.ok) throw new WbError(payload.error ?? `HTTP ${response.status}`, response.status, payload);
    return payload as T;
  }

  health() {
    return this.request("GET", "/v1/health");
  }

  async commands(): Promise<any[]> {
    return (await this.request("GET", "/v1/commands")).commands;
  }

  async submit(command: string, params: Record<string, unknown> = {}, options: { out?: string; fresh?: boolean; idempotencyKey?: string } = {}): Promise<Job> {
    const body: Record<string, unknown> = { command, params, fresh: Boolean(options.fresh), path_style: this.pathStyle };
    if (options.out) body.out = options.out;
    const envelope = await this.request<Envelope>("POST", "/v1/jobs", body, { "Idempotency-Key": options.idempotencyKey ?? randomUUID() });
    return new Job(this, envelope.job, envelope);
  }

  job(id: string): Job {
    return new Job(this, id);
  }
}

/** A promise of one result. */
export class Job {
  readonly client: Client;
  readonly id: string;
  envelope?: Envelope;

  constructor(client: Client, id: string, envelope?: Envelope) {
    this.client = client;
    this.id = id;
    this.envelope = envelope;
  }

  async refresh(wait = 0): Promise<Envelope> {
    this.envelope = await this.client.request<Envelope>("GET", `/v1/jobs/${this.id}${wait ? `?wait=${Math.ceil(wait)}` : ""}`);
    return this.envelope;
  }

  async wait(timeoutSeconds?: number): Promise<Envelope> {
    const deadline = timeoutSeconds === undefined ? Infinity : Date.now() + timeoutSeconds * 1000;
    let envelope = this.envelope ?? (await this.refresh());
    while (!TERMINAL.has(envelope.state) && envelope.state !== "awaiting_approval") {
      const remaining = Math.min(60, (deadline - Date.now()) / 1000);
      if (remaining <= 0) return envelope;
      envelope = await this.refresh(Math.max(1, remaining));
    }
    return envelope;
  }

  async result(timeoutSeconds?: number): Promise<Envelope> {
    const envelope = await this.wait(timeoutSeconds);
    if (envelope.state === "awaiting_approval") throw new ApprovalRequired(envelope);
    return envelope;
  }

  async approve(quote: string, via: "direct" | "relayed" = "direct"): Promise<Envelope> {
    this.envelope = await this.client.request<Envelope>("POST", `/v1/jobs/${this.id}/approve`, { quote, via });
    return this.envelope;
  }

  async cancel(): Promise<Envelope> {
    this.envelope = await this.client.request<Envelope>("POST", `/v1/jobs/${this.id}/cancel`, {});
    return this.envelope;
  }
}

export function exitCode(envelope: Envelope): number {
  if (envelope.state === "settled") return envelope.code ?? 3;
  if (envelope.state === "cancelled" || envelope.state === "interrupted") return 4;
  if (envelope.state === "awaiting_approval") return 6;
  return 5;
}

/** Submit `command` with `params` on the default client; returns the job (a promise). */
export function run(command: string, params: Record<string, unknown> = {}, options = {}): Promise<Job> {
  return new Client().submit(command, params, options);
}
