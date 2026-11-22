// The shared conformance checks against a live daemon (tests/test_sdks.py starts one and sets WB_URL and WB_TOKEN).
import assert from "node:assert/strict";
import { test } from "node:test";
import { ApprovalRequired, Client, exitCode } from "../src/index.ts";

const client = new Client();

test("echo settles ok with typed data", async () => {
  const result = await (await client.submit("selftest.echo", { text: "node", times: 2 })).result(60);
  assert.equal(result.schema, "wb/result/v1");
  assert.equal(result.outcome, "ok");
  assert.equal((result.data as any).text, "nodenode");
  assert.equal(exitCode(result), 0);
});

test("outcome codes", async () => {
  const result = await (await client.submit("selftest.fail", { outcome: "finding" })).result(60);
  assert.equal(result.code, 1);
  const usage = await (await client.submit("selftest.echo", { times: 0 })).result(60);
  assert.equal(usage.outcome, "usage");
});

test("approval flow", async () => {
  const job = await client.submit("selftest.spend", { price: 0 });
  await assert.rejects(job.result(60), (error: unknown) => {
    assert.ok(error instanceof ApprovalRequired);
    assert.ok(error.prompt.question);
    return true;
  });
  const quote = (job.envelope!.approval as any).quote.id;
  await job.approve(quote, "relayed");
  const settled = await job.result(60);
  assert.equal(settled.outcome, "ok");
});
