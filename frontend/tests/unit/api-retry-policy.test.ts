import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { networkRetryLimit } from "../../src/lib/api-retry-policy.ts";

test("network retries are enabled by default only for read-only methods", () => {
  assert.equal(networkRetryLimit("GET"), 3);
  assert.equal(networkRetryLimit("HEAD"), 3);
  assert.equal(networkRetryLimit("POST"), 0);
  assert.equal(networkRetryLimit("PUT"), 0);
  assert.equal(networkRetryLimit("PATCH"), 0);
  assert.equal(networkRetryLimit("DELETE"), 0);
});

test("callers can explicitly opt an idempotent mutation into retries", () => {
  assert.equal(networkRetryLimit("POST", true), 3);
  assert.equal(networkRetryLimit("GET", false), 0);
});

test("Stop retries its idempotent abort but preserves running UI without acknowledgement", () => {
  const hook = readFileSync("src/hooks/use-chat.ts", "utf8");
  assert.match(
    hook,
    /API\.CHAT\.ABORT[\s\S]*retryNetworkErrors: true/,
  );
  assert.match(
    hook,
    /if the backend did not acknowledge the abort[\s\S]*return false/,
  );
  assert.match(hook, /result\.status !== "aborted"[\s\S]*"not_found"/);
});
