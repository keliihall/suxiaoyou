import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  isActiveGoalStream,
  preferNewestGoalSnapshot,
} from "../../src/lib/goal-state.ts";
import {
  goalBudgetMaximumFromError,
  goalNeedsBudgetIncrease,
  resolveGoalPresentationState,
} from "../../src/lib/goal-ui.ts";
import type { SessionGoal } from "../../src/types/goal.ts";

function goal(
  revision: number,
  id = "goal-1",
  timeUpdated = "2026-07-15T00:00:00Z",
): SessionGoal {
  return {
    id,
    session_id: "session-1",
    objective: "Ship a verified result",
    definition_of_done: null,
    status: "active",
    run_state: "idle",
    revision,
    token_budget: null,
    tokens_used: 0,
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

test("goal snapshots are monotonic within one goal identity", () => {
  const current = goal(4);
  assert.equal(preferNewestGoalSnapshot(current, goal(3)), current);
  assert.equal(preferNewestGoalSnapshot(current, goal(4)).revision, 4);
  assert.equal(preferNewestGoalSnapshot(current, goal(5)).revision, 5);
  assert.equal(preferNewestGoalSnapshot(current, goal(1, "goal-2")).id, "goal-2");

  const currentTransient = goal(5, "goal-1", "2026-07-15T00:00:05Z");
  assert.equal(
    preferNewestGoalSnapshot(
      currentTransient,
      goal(5, "goal-1", "2026-07-15T00:00:04Z"),
    ),
    currentTransient,
  );
  assert.equal(
    preferNewestGoalSnapshot(
      currentTransient,
      goal(5, "goal-1", "2026-07-15T00:00:06Z"),
    ).time_updated,
    "2026-07-15T00:00:06Z",
  );
});

test("only the active Goal's own stream receives safe-pause semantics", () => {
  const active = {
    ...goal(3),
    status: "active" as const,
    run_state: "running" as const,
    last_stream_id: "goal-stream",
  };
  assert.equal(isActiveGoalStream(active, "goal-stream"), true);
  assert.equal(isActiveGoalStream(active, "ordinary-stream"), false);
  assert.equal(isActiveGoalStream({ ...active, status: "paused" }, "goal-stream"), false);
  assert.equal(isActiveGoalStream(null, "goal-stream"), false);
});

test("a budget-limited Goal unlocks resume after every exhausted budget is raised", () => {
  const exhausted = {
    ...goal(5),
    status: "budget_limited" as const,
    token_budget: 250_000,
    tokens_used: 250_231,
    blocker_code: "token_budget",
  };
  assert.equal(goalNeedsBudgetIncrease(exhausted), true);
  assert.equal(
    goalNeedsBudgetIncrease({ ...exhausted, token_budget: 500_000 }),
    false,
  );
  assert.equal(
    goalNeedsBudgetIncrease({
      ...exhausted,
      token_budget: 500_000,
      cost_budget_microusd: 10,
      cost_used_microusd: 10,
    }),
    true,
  );
});

test("structured Goal budget errors expose the server maximum", () => {
  assert.equal(goalBudgetMaximumFromError({
    body: {
      detail: {
        code: "goal_budget_exceeds_maximum",
        field: "token_budget",
        maximum: 2_000_000,
      },
    },
  }), 2_000_000);
  assert.equal(goalBudgetMaximumFromError({ body: { detail: "bad request" } }), null);
});

test("Goal presentation resolves one actionable state for copy, icon, and tone", () => {
  const running = {
    ...goal(5),
    status: "active" as const,
    run_state: "running" as const,
  };
  assert.equal(resolveGoalPresentationState(running), "running");
  assert.equal(
    resolveGoalPresentationState({ ...running, run_state: "waiting_user" }),
    "waiting_user",
  );
  assert.equal(
    resolveGoalPresentationState({ ...running, run_state: "pausing" }),
    "pausing",
  );
  assert.equal(
    resolveGoalPresentationState({ ...running, run_state: "interrupted" }),
    "needs_review",
  );
});

test("goal controls are session-keyed and fail closed behind the release gate", () => {
  const hook = readFileSync("src/hooks/use-session-goal.ts", "utf8");
  const api = readFileSync("src/lib/goal.ts", "utf8");
  const workspace = readFileSync("src/stores/workspace-store.ts", "utf8");
  const panel = readFileSync("src/components/workspace/workspace-panel.tsx", "utf8");

  assert.match(hook, /queryKeys\.sessions\.goal\(sessionId\)/);
  assert.match(hook, /queryKeys\.sessions\.goalUsage/);
  assert.match(hook, /release_gates\.goals === true/);
  assert.match(hook, /release_gates\.autonomous_goals === true/);
  assert.match(hook, /preferNewestGoalSnapshot/);
  assert.match(hook, /cancelQueries\(\{ queryKey: goalKey \}\)/);
  assert.match(api, /post<SessionGoal>\(API\.SESSIONS\.GOAL\(sessionId\)/);
  assert.match(api, /getSessionGoalUsage/);
  assert.match(api, /post<GoalStartResponse>\(API\.CHAT\.GOAL/);
  assert.match(hook, /isGoalStartResponse\(response\)/);
  assert.match(hook, /startGeneration\(response\.session_id, response\.stream_id\)/);
  assert.match(hook, /startStream\(response\.session_id, response\.stream_id\)/);
  assert.doesNotMatch(workspace, /\bgoal:\s*(null|\{)/);
  assert.match(panel, /focusedSessionId/);
  assert.match(panel, /<GoalCard sessionId=\{focusedSessionId\}/);
});

test("Goal card hides cumulative token details and keeps configured budget progress", () => {
  const card = readFileSync("src/components/goal/goal-card.tsx", "utf8");
  const activity = readFileSync("src/components/activity/activity-panel.tsx", "utf8");
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));
  const en = JSON.parse(readFileSync("src/i18n/locales/en/chat.json", "utf8"));

  assert.match(card, /tokenUsage\?\.total_tokens \?\? goal\.tokens_used/);
  assert.match(card, /goalBudgetUsage/);
  assert.match(card, /goalTokenUsageWithBudget/);
  assert.match(card, /goal\?\.token_budget != null/);
  assert.doesNotMatch(card, /goalCumulativeTokenUsage/);
  assert.doesNotMatch(card, /goal-token-breakdown/);
  assert.doesNotMatch(card, /goalTokenBreakdown/);

  assert.match(activity, /totalTokens\.input/);
  assert.match(activity, /totalTokens\.output/);
  assert.match(activity, /totalTokens\.reasoning/);
  assert.match(activity, /totalTokens\.cacheRead/);
  assert.match(activity, /currentResponseMetricsScope/);

  for (const key of [
    "currentResponseMetrics",
    "currentResponseMetricsScope",
    "totalContextTokens",
    "cacheRead",
    "goalBudgetUsage",
  ]) {
    assert.equal(typeof zh[key], "string", `missing zh key ${key}`);
    assert.equal(typeof en[key], "string", `missing en key ${key}`);
  }
  for (const key of [
    "goalCumulativeTokenUsage",
    "goalTokenUsage",
    "goalTokenBreakdown",
    "goalUncachedInputTokens",
    "goalOutputTokens",
    "goalReasoningTokens",
    "goalCacheReadTokens",
    "goalUnattributedTokens",
    "goalTokenAccountingNote",
  ]) {
    assert.equal(key in zh, false, `stale zh key ${key}`);
    assert.equal(key in en, false, `stale en key ${key}`);
  }
  assert.match(zh.goalBudgetUsage, /\u7f13\u5b58\u8bfb\u53d6/);
  assert.match(en.goalBudgetUsage, /cache reads/i);
});

test("goal controls expose localized actionable states", () => {
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));
  const en = JSON.parse(readFileSync("src/i18n/locales/en/chat.json", "utf8"));

  for (const key of [
    "goalStatusRunning",
    "goalStatusWaitingUser",
    "goalStatusBlocked",
    "goalStatusBudgetLimited",
    "goalBudgetLimitedDescription",
    "goalRevisionConflict",
    "goalTokenBudgetMaximumError",
    "goalTokenBudgetHint",
    "goalPauseAction",
    "goalResumeAction",
    "goalClearPauseFirst",
    "goalClearConfirmTitle",
  ]) {
    assert.equal(typeof zh[key], "string", `missing zh key ${key}`);
    assert.equal(typeof en[key], "string", `missing en key ${key}`);
  }
});
