"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/constants";
import {
  clearSessionGoal,
  createSessionGoal,
  createGoalClientRequestId,
  getSessionGoal,
  getSessionGoalUsage,
  isGoalStartResponse,
  pauseSessionGoal,
  preferNewestGoalSnapshot,
  resumeSessionGoal,
  updateSessionGoal,
} from "@/lib/goal";
import { startStream } from "@/lib/session-stream-registry";
import { useChatStore } from "@/stores/chat-store";
import { useSecurityOverview } from "@/hooks/use-security";
import type { GoalCreateRequest, GoalUpdateRequest, SessionGoal } from "@/types/goal";

export function useGoalsReleased(): boolean {
  const { data } = useSecurityOverview();
  return data?.release_gates.goals === true;
}

export function useAutonomousGoalsReleased(): boolean {
  const { data } = useSecurityOverview();
  return data?.release_gates.autonomous_goals === true;
}

export function useGoalTokenBudgetLimits() {
  const { data } = useSecurityOverview();
  return data?.goal_limits ?? null;
}

export function useSessionGoalUsage(
  sessionId: string | null | undefined,
  goal: SessionGoal | null | undefined,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: sessionId && goal
      ? queryKeys.sessions.goalUsage(sessionId, goal.id, goal.revision)
      : ["sessions", "none", "goal", "usage"] as const,
    queryFn: ({ signal }) => {
      if (!sessionId || !goal) {
        throw new Error("Goal usage is unavailable");
      }
      return getSessionGoalUsage(sessionId, signal);
    },
    enabled: options.enabled !== false && Boolean(sessionId && goal),
    staleTime: 2_000,
    // Source rows are durable before tool execution. Poll only while a visible
    // Goal card has an in-flight run; the revision-keyed query performs one
    // exact final read when the run reaches its safe boundary.
    refetchInterval: goal?.status === "active"
      && !["idle", "interrupted"].includes(goal.run_state)
      ? 2_000
      : false,
    refetchOnWindowFocus: true,
    retry: false,
  });
}

function isGoalRevisionConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

export function useSessionGoal(
  sessionId: string | null | undefined,
  options: { enabled?: boolean } = {},
) {
  const queryClient = useQueryClient();
  const enabled = options.enabled !== false && Boolean(sessionId);
  const goalKey = sessionId ? queryKeys.sessions.goal(sessionId) : ["sessions", "none", "goal"] as const;

  const goalQuery = useQuery({
    queryKey: goalKey,
    queryFn: async ({ signal }) => {
      if (!sessionId) return null;
      const incoming = await getSessionGoal(sessionId, signal);
      if (!incoming) return null;
      const current = queryClient.getQueryData<SessionGoal | null>(goalKey);
      return preferNewestGoalSnapshot(current, incoming);
    },
    enabled,
    staleTime: 15_000,
    refetchOnWindowFocus: true,
  });

  const writeSnapshot = (incoming: SessionGoal) => {
    queryClient.setQueryData<SessionGoal | null>(goalKey, (current) =>
      preferNewestGoalSnapshot(current, incoming),
    );
    void queryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
  };

  const reconcileConflict = (error: unknown) => {
    if (isGoalRevisionConflict(error)) {
      void queryClient.invalidateQueries({ queryKey: goalKey });
    }
  };

  const cancelGoalRead = () => queryClient.cancelQueries({ queryKey: goalKey });

  const createMutation = useMutation({
    mutationFn: async (
      request: Omit<GoalCreateRequest, "client_request_id">,
    ) => {
      if (!sessionId) throw new Error("Session is unavailable");
      return createSessionGoal(sessionId, {
        ...request,
        client_request_id: createGoalClientRequestId(),
      });
    },
    onMutate: cancelGoalRead,
    onSuccess: writeSnapshot,
    onError: reconcileConflict,
  });

  const updateMutation = useMutation({
    mutationFn: async (
      update: Omit<GoalUpdateRequest, "client_request_id" | "expected_revision">
        & { expected_revision?: number },
    ) => {
      if (!sessionId || !goalQuery.data) throw new Error("Goal is unavailable");
      const { expected_revision, ...fields } = update;
      return updateSessionGoal(sessionId, {
        ...fields,
        expected_revision: expected_revision ?? goalQuery.data.revision,
        client_request_id: createGoalClientRequestId(),
      });
    },
    onMutate: cancelGoalRead,
    onSuccess: writeSnapshot,
    onError: reconcileConflict,
  });

  const pauseMutation = useMutation({
    mutationFn: async () => {
      if (!sessionId || !goalQuery.data) throw new Error("Goal is unavailable");
      return pauseSessionGoal(sessionId, {
        expected_revision: goalQuery.data.revision,
        client_request_id: createGoalClientRequestId(),
      });
    },
    onMutate: cancelGoalRead,
    onSuccess: writeSnapshot,
    onError: reconcileConflict,
  });

  const resumeMutation = useMutation({
    mutationFn: async () => {
      if (!sessionId || !goalQuery.data) throw new Error("Goal is unavailable");
      return resumeSessionGoal(sessionId, {
        expected_revision: goalQuery.data.revision,
        client_request_id: createGoalClientRequestId(),
      });
    },
    onMutate: cancelGoalRead,
    onSuccess: (response) => {
      const goal = isGoalStartResponse(response) ? response.goal : response;
      writeSnapshot(goal);
      if (isGoalStartResponse(response)) {
        // Autonomous resume admits a fresh run. Seed the keyed chat bucket
        // before attaching SSE so no early event can write into a missing or
        // stale stream owner.
        useChatStore.getState().startGeneration(response.session_id, response.stream_id);
        void startStream(response.session_id, response.stream_id);
      }
    },
    onError: reconcileConflict,
  });

  const clearMutation = useMutation({
    mutationFn: async () => {
      if (!sessionId || !goalQuery.data) throw new Error("Goal is unavailable");
      return clearSessionGoal(sessionId, {
        expected_revision: goalQuery.data.revision,
        client_request_id: createGoalClientRequestId(),
      });
    },
    onMutate: cancelGoalRead,
    onSuccess: () => {
      queryClient.setQueryData<SessionGoal | null>(goalKey, null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
    },
    onError: reconcileConflict,
  });

  return {
    ...goalQuery,
    goal: goalQuery.data ?? null,
    createGoal: createMutation.mutateAsync,
    updateGoal: updateMutation.mutateAsync,
    pauseGoal: pauseMutation.mutateAsync,
    resumeGoal: resumeMutation.mutateAsync,
    clearGoal: clearMutation.mutateAsync,
    isCreating: createMutation.isPending,
    isUpdating: updateMutation.isPending,
    isPausing: pauseMutation.isPending,
    isResuming: resumeMutation.isPending,
    isClearing: clearMutation.isPending,
    isRevisionConflict:
      isGoalRevisionConflict(createMutation.error)
      || isGoalRevisionConflict(updateMutation.error)
      || isGoalRevisionConflict(pauseMutation.error)
      || isGoalRevisionConflict(resumeMutation.error)
      || isGoalRevisionConflict(clearMutation.error),
  };
}
