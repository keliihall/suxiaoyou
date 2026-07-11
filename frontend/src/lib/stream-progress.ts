/**
 * Heartbeats prove that the SSE connection is alive, but they do not prove
 * that the agent has made user-visible progress. Keep those two signals
 * separate so a connected-but-stalled task can be explained without
 * incorrectly presenting it as disconnected.
 */
import { isInteractionAwaitingResolution, type InteractionResponseState } from "./interaction-response.ts";

export const PROGRESS_STALLED_AFTER_MS = 60_000;

type PendingInteraction = {
  responseState?: InteractionResponseState;
} | null | undefined;

export function isWaitingForUserInteraction(
  ...prompts: PendingInteraction[]
): boolean {
  return prompts.some(
    (prompt) =>
      prompt != null
      && isInteractionAwaitingResolution(prompt.responseState),
  );
}

export function isBusinessProgressEvent(eventType: string): boolean {
  return eventType !== "heartbeat";
}

export function hasProgressStalled(
  now: number,
  lastProgressAt: number,
  isGenerating: boolean,
  isWaitingForUser = false,
  thresholdMs = PROGRESS_STALLED_AFTER_MS,
): boolean {
  if (
    !isGenerating
    || isWaitingForUser
    || !Number.isFinite(now)
    || !Number.isFinite(lastProgressAt)
  ) {
    return false;
  }
  return now - lastProgressAt >= Math.max(0, thresholdMs);
}
