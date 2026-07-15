import { preferNewestGoalSnapshot } from "./goal-state.ts";
import type { SessionGoal } from "../types/goal.ts";
import type { SSEEventData } from "../types/streaming.ts";

export type GoalSSECacheDecision =
  | { kind: "apply"; goal: SessionGoal }
  | { kind: "clear" }
  | { kind: "ignore" }
  | { kind: "refetch" };

export interface GoalSSEWatermark {
  goalId: string;
  revision: number;
  timeUpdated: string | null;
  cleared: boolean;
}

function isGoalSnapshot(value: unknown): value is SessionGoal {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return typeof record.id === "string"
    && typeof record.session_id === "string"
    && typeof record.objective === "string"
    && typeof record.status === "string"
    && typeof record.run_state === "string"
    && typeof record.revision === "number"
    && Number.isInteger(record.revision)
    && typeof record.time_created === "string"
    && typeof record.time_updated === "string";
}

function targetsSession(data: SSEEventData, sessionId: string): boolean {
  return data.session_id == null || data.session_id === sessionId;
}

/** Reconcile a full Goal snapshot carried by goal-updated/run lifecycle SSE. */
export function reconcileGoalSnapshotEvent(
  current: SessionGoal | null | undefined,
  data: SSEEventData,
  sessionId: string,
): GoalSSECacheDecision {
  if (!targetsSession(data, sessionId)) return { kind: "ignore" };
  if (!isGoalSnapshot(data.goal)) return { kind: "refetch" };
  if (data.goal.session_id !== sessionId) return { kind: "ignore" };
  if (data.goal_id && data.goal_id !== data.goal.id) return { kind: "ignore" };
  if (current && current.id !== data.goal.id) {
    const currentCreatedAt = Date.parse(current.time_created);
    const incomingCreatedAt = Date.parse(data.goal.time_created);
    if (
      Number.isFinite(currentCreatedAt)
      && Number.isFinite(incomingCreatedAt)
      && currentCreatedAt > incomingCreatedAt
    ) {
      return { kind: "ignore" };
    }
  }

  const goal = preferNewestGoalSnapshot(current, data.goal);
  return goal === current ? { kind: "ignore" } : { kind: "apply", goal };
}

export function goalSSEWatermarkForSnapshot(goal: SessionGoal): GoalSSEWatermark {
  return {
    goalId: goal.id,
    revision: goal.revision,
    timeUpdated: goal.time_updated,
    cleared: false,
  };
}

export function goalSSEWatermarkForClear(
  current: SessionGoal | null | undefined,
  data: SSEEventData,
  previous?: GoalSSEWatermark,
): GoalSSEWatermark | null {
  const goalId = data.goal_id ?? current?.id ?? previous?.goalId;
  if (!goalId) return null;
  const previousRevision = previous?.goalId === goalId ? previous.revision : 0;
  return {
    goalId,
    revision: Math.max(data.revision ?? current?.revision ?? 0, previousRevision),
    timeUpdated: null,
    cleared: true,
  };
}

/** A clear tombstone prevents replay from resurrecting the deleted identity. */
export function isGoalSnapshotBlockedByWatermark(
  goal: SessionGoal,
  watermark: GoalSSEWatermark | null | undefined,
): boolean {
  if (!watermark || watermark.goalId !== goal.id) return false;
  if (watermark.cleared) return true;
  if (watermark.revision > goal.revision) return true;
  if (watermark.revision < goal.revision || !watermark.timeUpdated) return false;

  const watermarkUpdatedAt = Date.parse(watermark.timeUpdated);
  const incomingUpdatedAt = Date.parse(goal.time_updated);
  return Number.isFinite(watermarkUpdatedAt)
    && Number.isFinite(incomingUpdatedAt)
    && watermarkUpdatedAt > incomingUpdatedAt;
}

/**
 * Clear only the Goal identity named by the event. A replay for an older Goal
 * must not erase a replacement Goal created later in the same conversation.
 */
export function reconcileGoalClearedEvent(
  current: SessionGoal | null | undefined,
  data: SSEEventData,
  sessionId: string,
): GoalSSECacheDecision {
  if (!targetsSession(data, sessionId)) return { kind: "ignore" };
  if (current && data.goal_id && data.goal_id !== current.id) {
    return { kind: "ignore" };
  }
  if (
    current
    && typeof data.revision === "number"
    && data.revision < current.revision
  ) {
    return { kind: "ignore" };
  }
  return { kind: "clear" };
}

/** Partial warning/wait events are refetch hints, never replacement snapshots. */
export function shouldRefetchGoalForPartialEvent(
  data: SSEEventData,
  sessionId: string,
): boolean {
  return targetsSession(data, sessionId);
}
