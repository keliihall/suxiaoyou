import type { StepFinishPart, ToolPart } from "../types/message.ts";

export interface ActivityCompletionState {
  toolParts: ToolPart[];
  stepParts: Array<{ type: string } | StepFinishPart>;
  hasVisibleOutput?: boolean;
  isAwaitingConfirmation?: boolean;
  /** Authoritative lifecycle state supplied by history or the terminal event. */
  isTerminal?: boolean;
}

export function hasTerminalStepFinish(data: ActivityCompletionState): boolean {
  return data.stepParts.some(
    (part) => part.type === "step-finish" &&
      (part as StepFinishPart).reason !== "tool_use",
  );
}

/**
 * A completed activity is not necessarily a successful one. Keep this
 * separate from `isActivityComplete` so individual tool detours never inherit
 * task-level failure styling, while a genuinely failed response still does.
 */
export function isActivityFailed(data: ActivityCompletionState): boolean {
  for (let index = data.stepParts.length - 1; index >= 0; index -= 1) {
    const part = data.stepParts[index];
    if (part.type !== "step-finish") continue;
    const reason = (part as StepFinishPart).reason;
    if (reason === "tool_use") continue;
    return reason === "error";
  }
  return false;
}

export function hasRunningActivityTools(data: ActivityCompletionState): boolean {
  return data.toolParts.some(
    (tool) => tool.state.status === "running" || tool.state.status === "pending",
  );
}

/**
 * Terminal lifecycle evidence is authoritative.  A dropped tool-result event
 * must not leave a completed response displaying "Finalizing" forever.
 */
export function isActivityComplete(data: ActivityCompletionState): boolean {
  if (data.isAwaitingConfirmation) return false;
  if (data.isTerminal || hasTerminalStepFinish(data)) return true;
  return !!data.hasVisibleOutput && !hasRunningActivityTools(data);
}
