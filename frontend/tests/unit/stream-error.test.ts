import assert from "node:assert/strict";
import test from "node:test";

import {
  streamErrorToastId,
  streamErrorTranslationKey,
} from "../../src/lib/stream-error.ts";

test("low-level truncated stream errors are mapped to a localized category", () => {
  const payload = {
    error_message:
      "peer closed connection without sending complete message body (incomplete chunked read)",
  };
  const key = streamErrorTranslationKey(payload);

  assert.equal(key, "streamErrorConnectionInterrupted");
  assert.equal(
    streamErrorToastId("session-1", payload, key),
    "stream-error:session-1:streamErrorConnectionInterrupted",
  );
});

test("machine-readable model error codes select stable localized messages", () => {
  const payload = {
    code: "model_rate_limited",
    error_message: "provider-specific diagnostic",
  };

  assert.equal(streamErrorTranslationKey(payload), "streamErrorRateLimited");
  assert.equal(
    streamErrorToastId(
      "session-1",
      payload,
      streamErrorTranslationKey(payload),
    ),
    "stream-error:session-1:model_rate_limited",
  );
});

test("shared backend error codes stay on localized UI categories", () => {
  const cases = [
    ["server_busy", "streamErrorServerBusy"],
    ["session_busy", "streamErrorServerBusy"],
    ["hook_policy_failed", "streamErrorPolicyBlocked"],
    ["security_emergency_stop", "streamErrorPolicyBlocked"],
    ["model_not_found", "streamErrorModelNotFound"],
    ["MODEL_DOES_NOT_SUPPORT_IMAGES", "visionModelRequired"],
    ["task_batch_workspace_rejected", "taskBatchStartFailed"],
    ["task_batch_failed", "taskBatchExecutionFailed"],
    ["compaction_failed", "contextCompactError"],
  ] as const;

  for (const [code, expectedKey] of cases) {
    assert.equal(
      streamErrorTranslationKey({
        code,
        error_message: "provider-specific English diagnostic",
      }),
      expectedKey,
    );
  }
});

test("unknown backend diagnostics are not treated as display text", () => {
  assert.equal(
    streamErrorTranslationKey({
      code: "internal_error",
      error_message: "secret raw exception details",
    }),
    null,
  );
});
