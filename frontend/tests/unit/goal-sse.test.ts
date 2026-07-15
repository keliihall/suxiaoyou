import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  goalSSEWatermarkForClear,
  goalSSEWatermarkForSnapshot,
  isGoalSnapshotBlockedByWatermark,
  reconcileGoalClearedEvent,
  reconcileGoalSnapshotEvent,
  shouldRefetchGoalForPartialEvent,
} from "../../src/lib/goal-sse.ts";
import type { SessionGoal } from "../../src/types/goal.ts";

function goal(
  revision: number,
  sessionId = "session-a",
  id = "goal-a",
  timeUpdated = `2026-07-15T00:00:0${Math.min(revision, 9)}Z`,
): SessionGoal {
  return {
    id,
    session_id: sessionId,
    objective: "Ship a verified result",
    definition_of_done: null,
    status: "active",
    run_state: "running",
    revision,
    token_budget: null,
    tokens_used: revision,
    cost_budget_microusd: null,
    cost_used_microusd: 0,
    time_budget_seconds: null,
    time_used_seconds: 0,
    max_continuations: null,
    continuation_count: 0,
    no_progress_count: 0,
    blocker_streak: 0,
    consecutive_error_count: 0,
    blocker_code: null,
    blocker_message: null,
    needs_review: false,
    next_retry_at: null,
    completion_summary: null,
    completion_evidence: null,
    model_id: null,
    provider_id: null,
    agent: "build",
    reasoning: null,
    language: "en",
    last_run_id: null,
    last_stream_id: null,
    time_started: null,
    time_completed: null,
    time_created: "2026-07-15T00:00:00Z",
    time_updated: timeUpdated,
  };
}

test("full Goal SSE snapshots stay session-scoped and revision monotonic", () => {
  const current = goal(4);

  assert.deepEqual(
    reconcileGoalSnapshotEvent(current, { goal: goal(3) }, "session-a"),
    { kind: "ignore" },
  );

  const newer = goal(5);
  assert.deepEqual(
    reconcileGoalSnapshotEvent(current, { goal: newer }, "session-a"),
    { kind: "apply", goal: newer },
  );

  assert.deepEqual(
    reconcileGoalSnapshotEvent(current, { goal: goal(5, "session-b") }, "session-a"),
    { kind: "ignore" },
  );
  assert.deepEqual(
    reconcileGoalSnapshotEvent(current, { goal_id: "goal-a" }, "session-a"),
    { kind: "refetch" },
  );
});

test("partial Goal SSE events only refetch their stream session", () => {
  assert.equal(
    shouldRefetchGoalForPartialEvent({ goal_id: "goal-a" }, "session-a"),
    true,
  );
  assert.equal(
    shouldRefetchGoalForPartialEvent({ goal_id: "goal-old" }, "session-a"),
    true,
  );
  assert.equal(
    shouldRefetchGoalForPartialEvent(
      { session_id: "session-b", goal_id: "goal-a" },
      "session-a",
    ),
    false,
  );
  assert.equal(
    shouldRefetchGoalForPartialEvent({ goal_id: "goal-a" }, "session-a"),
    true,
  );
});

test("goal-cleared cannot erase a newer revision or replacement Goal", () => {
  const current = goal(5);
  assert.deepEqual(
    reconcileGoalClearedEvent(
      current,
      { goal_id: "goal-a", revision: 4 },
      "session-a",
    ),
    { kind: "ignore" },
  );
  assert.deepEqual(
    reconcileGoalClearedEvent(
      current,
      { goal_id: "goal-old", revision: 9 },
      "session-a",
    ),
    { kind: "ignore" },
  );
  assert.deepEqual(
    reconcileGoalClearedEvent(
      current,
      { goal_id: "goal-a", revision: 5 },
      "session-a",
    ),
    { kind: "clear" },
  );

  const liveWatermark = goalSSEWatermarkForSnapshot(current);
  assert.equal(isGoalSnapshotBlockedByWatermark(goal(4), liveWatermark), true);
  const clearWatermark = goalSSEWatermarkForClear(
    current,
    { goal_id: "goal-a", revision: 5 },
    liveWatermark,
  );
  assert.equal(isGoalSnapshotBlockedByWatermark(goal(6), clearWatermark), true);
  assert.equal(
    isGoalSnapshotBlockedByWatermark(
      goal(1, "session-a", "goal-replacement"),
      clearWatermark,
    ),
    false,
  );
});

test("stream registry handles every Goal event and refreshes recovery boundaries", () => {
  const types = readFileSync("src/types/streaming.ts", "utf8");
  const registry = readFileSync("src/lib/session-stream-registry.ts", "utf8");

  for (const constant of [
    "GOAL_UPDATED",
    "GOAL_CLEARED",
    "GOAL_RUN_STARTED",
    "GOAL_RUN_FINISHED",
    "GOAL_BUDGET_WARNING",
    "GOAL_NEEDS_USER",
  ]) {
    assert.match(types, new RegExp(`${constant}:`));
    assert.match(registry, new RegExp(`onCurrent\\(SSE_EVENTS\\.${constant},`));
  }

  assert.match(registry, /const goalKey = queryKeys\.sessions\.goal\(sessionId\)/);
  assert.match(registry, /goalWatermarkKey/);
  assert.match(registry, /SSE_EVENTS\.DESYNC[\s\S]*?invalidateGoalCache\(\)/);
  assert.match(registry, /SSE_EVENTS\.DONE[\s\S]*?invalidateGoalCache\(\)/);
  assert.match(registry, /handleAgentError[\s\S]*?invalidateGoalCache\(\)/);
});
