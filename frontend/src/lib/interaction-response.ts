/** Shared state machine for question, permission and plan acknowledgements. */

export type InteractionResponseState =
  | "idle"
  | "submitting"
  | "resolved"
  | "recovering"
  | "recovery_needed"
  | "continuing";

const STATE_RANK: Record<InteractionResponseState, number> = {
  idle: 0,
  submitting: 1,
  resolved: 2,
  recovering: 3,
  recovery_needed: 4,
  continuing: 5,
};

export type InteractionPromptType = "permission" | "question" | "plan";

export interface InteractionRecoveryTarget {
  sessionId: string;
  streamId: string;
  callId: string;
  promptType: InteractionPromptType;
  streamGeneration: number;
}

export const INTERACTION_CONTINUATION_GRACE_MS = 10_000;
export const INTERACTION_RECOVERY_VERIFY_MS = 12_000;

const CONTINUATION_EVENTS = new Set([
  "model-loading",
  "text-delta",
  "reasoning-delta",
  "tool-call",
  "tool-result",
  "tool-error",
  "step-start",
  "step-finish",
  "task-batch-start",
  "task-batch-update",
  "task-batch-finish",
  "compaction-start",
  "compaction-phase",
  "compaction-progress",
  "compacted",
]);

export function canSubmitInteraction(state?: InteractionResponseState): boolean {
  return !state || state === "idle";
}

export function isInteractionAwaitingResolution(
  state?: InteractionResponseState,
): boolean {
  return !state || state === "idle" || state === "submitting";
}

export function isInteractionPendingContinuation(
  state?: InteractionResponseState,
): boolean {
  return state === "resolved"
    || state === "recovering"
    || state === "recovery_needed";
}

export function isInteractionRecoveryActionable(
  state?: InteractionResponseState,
): boolean {
  return state === "recovery_needed";
}

export function canMarkInteractionContinuing(
  state?: InteractionResponseState,
): boolean {
  return state === "submitting" || isInteractionPendingContinuation(state);
}

export function isInteractionContinuationEvent(eventType: string): boolean {
  return CONTINUATION_EVENTS.has(eventType);
}

export function matchesInteractionRecoveryTarget(
  expected: InteractionRecoveryTarget,
  current: InteractionRecoveryTarget,
): boolean {
  return expected.sessionId === current.sessionId
    && expected.streamId === current.streamId
    && expected.callId === current.callId
    && expected.promptType === current.promptType
    && expected.streamGeneration === current.streamGeneration;
}

export function canResetInteractionAfterFailure(
  state?: InteractionResponseState,
): boolean {
  return state === "submitting";
}

/**
 * Explicit user retry is the sole safe backwards-looking transition. It only
 * re-enters recovery from the actionable terminal recovery state; late HTTP or
 * SSE updates still go through the monotonic state machine below.
 */
export function advanceInteractionRecoveryRetryState(
  current: InteractionResponseState | undefined,
): InteractionResponseState | undefined {
  return current === "recovery_needed" ? "recovering" : current;
}

/**
 * Keep acknowledgement transitions monotonic.
 *
 * The resolved SSE event and POST response race each other.  A late HTTP
 * response must not move a card backwards from "continuing" to "resolved".
 */
export function advanceInteractionResponseState(
  current: InteractionResponseState | undefined,
  next: InteractionResponseState,
): InteractionResponseState {
  const currentState = current ?? "idle";
  return STATE_RANK[next] >= STATE_RANK[currentState] ? next : currentState;
}
