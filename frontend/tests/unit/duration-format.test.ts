import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  formatElapsedDuration,
  formatElapsedMilliseconds,
} from "../../src/lib/duration.ts";

test("formats Chinese elapsed durations with readable units", () => {
  assert.equal(formatElapsedDuration(0, "zh-CN"), "0秒");
  assert.equal(formatElapsedDuration(42, "zh-CN"), "42秒");
  assert.equal(formatElapsedDuration(2862, "zh-CN"), "47分钟42秒");
  assert.equal(formatElapsedDuration(3723, "zh-CN"), "1小时02分钟03秒");
});

test("formats non-Chinese elapsed durations with compact units", () => {
  assert.equal(formatElapsedDuration(42, "en-US"), "42s");
  assert.equal(formatElapsedDuration(2862, "en-US"), "47m 42s");
  assert.equal(formatElapsedDuration(3723, "en-US"), "1h 02m 03s");
});

test("keeps sub-second tool runtimes localized and clamps invalid input", () => {
  assert.equal(formatElapsedMilliseconds(450, "zh-CN"), "450毫秒");
  assert.equal(formatElapsedMilliseconds(1450, "zh-CN"), "1秒");
  assert.equal(formatElapsedDuration(-5, "zh-CN"), "0秒");
  assert.equal(formatElapsedDuration(Number.NaN, "en"), "0s");
});

test("live generation timestamps are retained only for the active run", () => {
  const store = readFileSync("src/stores/chat-store.ts", "utf8");
  assert.match(
    store,
    /base\.isGenerating && base\.generationStartedAt !== null/,
  );
  assert.match(
    store,
    /base\.isGenerating && base\.lastBusinessProgressAt !== null/,
  );
  assert.match(store, /finishGeneration:[\s\S]*generationStartedAt: null/);
});
