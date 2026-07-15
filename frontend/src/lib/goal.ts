import { api } from "@/lib/api";
import { API } from "@/lib/constants";
import type {
  GoalControlRequest,
  GoalCreateRequest,
  GoalStartRequest,
  GoalStartResponse,
  GoalTokenUsage,
  GoalUpdateRequest,
  SessionGoal,
} from "@/types/goal";
export { preferNewestGoalSnapshot } from "@/lib/goal-state";

export function createGoalClientRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `goal-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function getSessionGoal(sessionId: string, signal?: AbortSignal) {
  return api.get<SessionGoal | null>(API.SESSIONS.GOAL(sessionId), { signal });
}

export function getSessionGoalUsage(sessionId: string, signal?: AbortSignal) {
  return api.get<GoalTokenUsage>(API.SESSIONS.GOAL_USAGE(sessionId), { signal });
}

export function startGoal(request: GoalStartRequest) {
  return api.post<GoalStartResponse>(API.CHAT.GOAL, request, {
    retryNetworkErrors: true,
  });
}

export function createSessionGoal(sessionId: string, request: GoalCreateRequest) {
  return api.post<SessionGoal>(API.SESSIONS.GOAL(sessionId), request, {
    retryNetworkErrors: true,
  });
}

export function updateSessionGoal(sessionId: string, request: GoalUpdateRequest) {
  return api.patch<SessionGoal>(API.SESSIONS.GOAL(sessionId), request, {
    retryNetworkErrors: true,
  });
}

export function pauseSessionGoal(sessionId: string, request: GoalControlRequest) {
  return api.post<SessionGoal>(API.SESSIONS.GOAL_PAUSE(sessionId), request, {
    retryNetworkErrors: true,
  });
}

export function resumeSessionGoal(sessionId: string, request: GoalControlRequest) {
  return api.post<SessionGoal | GoalStartResponse>(API.SESSIONS.GOAL_RESUME(sessionId), request, {
    retryNetworkErrors: true,
  });
}

export function isGoalStartResponse(
  response: SessionGoal | GoalStartResponse,
): response is GoalStartResponse {
  return "goal" in response && "stream_id" in response && "session_id" in response;
}

export function clearSessionGoal(sessionId: string, request: GoalControlRequest) {
  const params = new URLSearchParams({
    client_request_id: request.client_request_id,
    expected_revision: String(request.expected_revision),
  });
  return api.delete<void>(`${API.SESSIONS.GOAL(sessionId)}?${params.toString()}`, {
    retryNetworkErrors: true,
  });
}
