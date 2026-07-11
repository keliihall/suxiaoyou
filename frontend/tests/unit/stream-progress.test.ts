import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  hasProgressStalled,
  isBusinessProgressEvent,
  isWaitingForUserInteraction,
  PROGRESS_STALLED_AFTER_MS,
} from "../../src/lib/stream-progress.ts";

test("heartbeats keep the connection alive without pretending the task advanced", () => {
  assert.equal(isBusinessProgressEvent("heartbeat"), false);
  assert.equal(isBusinessProgressEvent("text-delta"), true);
  assert.equal(isBusinessProgressEvent("tool-result"), true);

  assert.equal(
    hasProgressStalled(
      PROGRESS_STALLED_AFTER_MS,
      0,
      true,
    ),
    true,
  );
  assert.equal(hasProgressStalled(PROGRESS_STALLED_AFTER_MS, 0, false), false);
});

test("an explicit interaction wait is not reported as a stalled task", () => {
  assert.equal(isWaitingForUserInteraction(null, null, null), false);
  assert.equal(
    isWaitingForUserInteraction({ responseState: "idle" }, null, null),
    true,
  );
  assert.equal(
    isWaitingForUserInteraction(null, { responseState: "submitting" }, null),
    true,
  );
  assert.equal(
    isWaitingForUserInteraction(null, null, { responseState: "resolved" }),
    false,
  );
  assert.equal(
    hasProgressStalled(PROGRESS_STALLED_AFTER_MS, 0, true, true),
    false,
  );
});

test("the stalled state is surfaced beside controls that remain usable", () => {
  const registry = readFileSync("src/lib/session-stream-registry.ts", "utf8");
  const form = readFileSync("src/components/chat/chat-form.tsx", "utf8");
  const en = JSON.parse(readFileSync("src/i18n/locales/en/chat.json", "utf8"));
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));

  assert.match(registry, /isBusinessProgressEvent\(eventType\)/);
  assert.match(registry, /lastBusinessProgressAt/);
  assert.match(registry, /isWaitingForUserInteraction/);
  assert.match(registry, /reconnectStream/);
  assert.match(registry, /const isCurrentGeneration/);
  assert.match(registry, /an old DONE[\s\S]*newer stream/);
  assert.match(form, /data-testid="progress-stalled-notice"/);
  assert.match(form, /taskNoProgressFor/);
  assert.match(form, /taskReconnect/);
  assert.match(en.taskMayBeStalledHint, /queuing follow-ups or stop/);
  assert.match(zh.taskMayBeStalledHint, /继续排队输入，或停止当前任务/);
});
