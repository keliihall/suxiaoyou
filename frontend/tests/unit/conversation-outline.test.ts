import assert from "node:assert/strict";
import test from "node:test";

import {
  conversationHistoryWindowOffsets,
  conversationOutlineKeyTarget,
} from "../../src/lib/conversation-outline.ts";

test("directed history windows contain turns 1, 100, and 200 without loading 400 messages", () => {
  assert.deepEqual(conversationHistoryWindowOffsets(0, 400, 50), [0, 50]);
  assert.deepEqual(
    conversationHistoryWindowOffsets(198, 400, 50),
    [100, 150, 200],
  );
  assert.deepEqual(
    conversationHistoryWindowOffsets(398, 400, 50),
    [300, 350],
  );
});

test("directed history windows clamp stale offsets and reject empty input", () => {
  assert.deepEqual(conversationHistoryWindowOffsets(-20, 120, 50), [0, 50]);
  assert.deepEqual(conversationHistoryWindowOffsets(999, 120, 50), [50, 100]);
  assert.deepEqual(conversationHistoryWindowOffsets(0, 0, 50), []);
});

test("outline keyboard state moves predictably and stops at boundaries", () => {
  assert.equal(conversationOutlineKeyTarget("ArrowDown", 0, 4), 1);
  assert.equal(conversationOutlineKeyTarget("ArrowUp", 2, 4), 1);
  assert.equal(conversationOutlineKeyTarget("Home", 3, 4), 0);
  assert.equal(conversationOutlineKeyTarget("End", 0, 4), 3);
  assert.equal(conversationOutlineKeyTarget("ArrowUp", 0, 4), null);
  assert.equal(conversationOutlineKeyTarget("ArrowDown", 3, 4), null);
  assert.equal(conversationOutlineKeyTarget("Enter", 1, 4), null);
});
