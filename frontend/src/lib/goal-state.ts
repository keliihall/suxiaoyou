import type { SessionGoal } from "../types/goal.ts";

/** Never let an older replay or slower GET overwrite a newer goal revision. */
export function preferNewestGoalSnapshot(
  current: SessionGoal | null | undefined,
  incoming: SessionGoal,
): SessionGoal {
  if (current && current.id === incoming.id) {
    if (current.revision > incoming.revision) return current;
    if (current.revision === incoming.revision) {
      const currentUpdatedAt = Date.parse(current.time_updated);
      const incomingUpdatedAt = Date.parse(incoming.time_updated);
      if (
        Number.isFinite(currentUpdatedAt)
        && Number.isFinite(incomingUpdatedAt)
        && currentUpdatedAt > incomingUpdatedAt
      ) {
        return current;
      }
    }
  }
  return incoming;
}

/**
 * A session can have an active persistent Goal while an unrelated ordinary
 * prompt is running. Match both the Goal status and its durable stream id so
 * Stop only changes semantics for the Goal run that owns the current stream.
 */
export function isActiveGoalStream(
  goal: SessionGoal | null | undefined,
  streamId: string | null | undefined,
): goal is SessionGoal {
  return Boolean(
    goal
    && streamId
    && goal.status === "active"
    && goal.last_stream_id === streamId,
  );
}
