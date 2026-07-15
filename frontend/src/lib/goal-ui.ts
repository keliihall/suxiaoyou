import type { SessionGoal } from "@/types/goal";

export const OPEN_SESSION_GOAL_EVENT = "suxiaoyou:open-session-goal";

export interface OpenSessionGoalEventDetail {
  sessionId: string;
}

export type GoalPresentationState =
  | "active"
  | "running"
  | "pausing"
  | "waiting_user"
  | "needs_review"
  | "paused"
  | "blocked"
  | "usage_limited"
  | "budget_limited"
  | "complete";

/**
 * Resolve Goal status once so visible copy, icons, and tone cannot disagree.
 * A transient user/action boundary wins because it is what needs attention
 * now; lifecycle status supplies the stable fallback.
 */
export function resolveGoalPresentationState(goal: SessionGoal): GoalPresentationState {
  if (goal.run_state === "waiting_user") return "waiting_user";
  if (goal.run_state === "pausing") return "pausing";
  if (goal.run_state === "interrupted" || goal.needs_review) return "needs_review";
  switch (goal.status) {
    case "active":
      return goal.run_state === "running" || goal.run_state === "reserved"
        ? "running"
        : "active";
    case "paused": return "paused";
    case "blocked": return "blocked";
    case "usage_limited": return "usage_limited";
    case "budget_limited": return "budget_limited";
    case "complete": return "complete";
  }
}

/** Bridge a slash command to the desktop panel or mobile Goal sheet. */
export function requestOpenSessionGoal(sessionId: string): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent<OpenSessionGoalEventDetail>(
    OPEN_SESSION_GOAL_EVENT,
    { detail: { sessionId } },
  ));
}

/** Mirror the backend resume gate so a raised budget immediately unlocks Resume. */
export function hasRemainingGoalBudget(goal: SessionGoal): boolean {
  const checks: Array<readonly [number | null, number]> = [
    [goal.token_budget, goal.tokens_used],
    [goal.cost_budget_microusd, goal.cost_used_microusd],
    [goal.time_budget_seconds, goal.time_used_seconds],
    [goal.max_continuations, goal.continuation_count],
  ];
  return checks.every(([limit, used]) => limit == null || used < limit);
}

export function goalNeedsBudgetIncrease(goal: SessionGoal): boolean {
  return goal.status === "budget_limited" && !hasRemainingGoalBudget(goal);
}

/** Read a structured server ceiling without coupling UI helpers to ApiError. */
export function goalBudgetMaximumFromError(
  error: unknown,
  field = "token_budget",
): number | null {
  if (!error || typeof error !== "object" || !("body" in error)) return null;
  const body = (error as { body?: unknown }).body;
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const detail = (body as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return null;
  const record = detail as Record<string, unknown>;
  return record.code === "goal_budget_exceeds_maximum"
    && record.field === field
    && typeof record.maximum === "number"
    && Number.isSafeInteger(record.maximum)
    && record.maximum >= 0
    ? record.maximum
    : null;
}
