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

const GOAL_BLOCKER_MESSAGE_KEYS: Readonly<Record<string, string>> = {
  loop_detected: "goalBlockerLoopDetected",
  generation_error: "goalBlockerGenerationError",
  no_progress: "goalBlockerNoProgress",
  provider_usage_limited: "goalBlockerProviderUsageLimited",
  manual_goal_turn_complete: "goalBlockerManualTurnComplete",
  session_archived: "goalBlockerSessionArchived",
  security_emergency_stop: "goalBlockerSecurityEmergencyStop",
  application_shutdown: "goalBlockerApplicationShutdown",
  restart_uncertain: "goalBlockerRestartUncertain",
  permission_required: "goalBlockerPermissionRequired",
  permission_denied: "goalBlockerPermissionDenied",
  permission_timeout: "goalBlockerPermissionTimeout",
  immediate_stop: "goalBlockerImmediateStop",
  worker_cancelled: "goalBlockerWorkerCancelled",
  controller_error: "goalBlockerControllerError",
  invocation_source_denied: "goalBlockerInvocationSourceDenied",
  security_audit_unavailable: "goalBlockerSecurityAuditUnavailable",
  MODEL_DOES_NOT_SUPPORT_IMAGES: "goalBlockerImagesUnsupported",
  goal_edited: "goalBlockerGoalEdited",
  user_pause: "goalBlockerUserPause",
  blocked: "goalBlockerGenericBlocked",
  failed: "goalBlockerGenericFailed",
  interrupted: "goalBlockerGenericInterrupted",
};

const LEGACY_FORCED_LOOP_STOP = /^\[FORCED STOP\]\s+Repeated tool calls exceeded/i;

/**
 * Resolve Goal status once so visible copy, icons, and tone cannot disagree.
 * A transient user/action boundary wins because it is what needs attention
 * now; lifecycle status supplies the stable fallback.
 */
export function resolveGoalPresentationState(goal: SessionGoal): GoalPresentationState {
  if (goal.run_state === "waiting_user") return "waiting_user";
  if (goal.run_state === "pausing") return "pausing";
  if (goal.run_state === "interrupted") return "needs_review";
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

/**
 * Resolve server-owned blocker codes to UI-localized copy.
 *
 * Arbitrary model-authored blockers deliberately fall back to their original
 * text. The legacy message check upgrades Goals saved before loop_detected was
 * preserved as the durable blocker code.
 */
export function goalBlockerMessageKey(goal: SessionGoal): string | null {
  if (
    goal.blocker_code === "generation_error"
    && LEGACY_FORCED_LOOP_STOP.test(goal.blocker_message || "")
  ) {
    return "goalBlockerLoopDetected";
  }
  return goal.blocker_code
    ? GOAL_BLOCKER_MESSAGE_KEYS[goal.blocker_code] || null
    : null;
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
