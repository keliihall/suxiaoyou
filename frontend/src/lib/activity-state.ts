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
