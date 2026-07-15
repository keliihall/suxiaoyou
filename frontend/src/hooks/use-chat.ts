"use client";

import { useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, ApiError } from "@/lib/api";
import { API, queryKeys } from "@/lib/constants";
import i18n from "@/i18n/config";
import { getChatRoute } from "@/lib/routes";
import { useChatStore, useChatSession } from "@/stores/chat-store";
import { useSettingsStore } from "@/stores/settings-store";
import { useWorkspaceStore } from "@/stores/workspace-store";
import { useActivityStore } from "@/stores/activity-store";
import {
  reconnectStream,
  recoverInteractionState,
  startStream,
  stopStream,
} from "@/lib/session-stream-registry";
import {
  clearSessionGoal,
  createGoalClientRequestId,
  getSessionGoal,
  isGoalStartResponse,
  pauseSessionGoal,
  preferNewestGoalSnapshot,
  resumeSessionGoal,
  startGoal,
  updateSessionGoal,
} from "@/lib/goal";
import { isActiveGoalStream } from "@/lib/goal-state";
import { requestOpenSessionGoal } from "@/lib/goal-ui";
import type { ParsedGoalCommand } from "@/lib/goal-command";
import { canSubmitInteraction, type InteractionPromptType } from "@/lib/interaction-response";
import {
  clearSessionInputRequestId,
  removeSessionInput,
  reserveSessionInputRequestId,
  sortSessionInputs,
  upsertSessionInput,
} from "@/lib/session-inputs";
import {
  clearPromptRequestId,
  promptRequestFingerprint,
  reservePromptRequestId,
} from "@/lib/prompt-idempotency";
import {
  DESKTOP_PERMISSION_SOURCE,
  normalizePermissionWorkspace,
  savedPermissionRulesForContext,
  type SavedPermissionContext,
} from "@/lib/saved-permissions";
import { useRemoteGenerationSync } from "./use-remote-generation-sync";
import type { InfiniteData } from "@tanstack/react-query";
import type {
  FileAttachment,
  EditAndResendResult,
  PromptRequest,
  PromptResponse,
  RespondRequest,
  RespondResult,
  SessionInputMode,
  SessionInputResponse,
  SessionInputUpdateRequest,
  TaskBatchRequest,
} from "@/types/chat";
import type { ConversationTurnIndex, PaginatedMessages } from "@/types/message";
import type { SessionResponse } from "@/types/session";
import type { ModelInfo } from "@/types/model";
import type { GoalStartRequest, SessionGoal } from "@/types/goal";

const MODEL_DOES_NOT_SUPPORT_IMAGES = "MODEL_DOES_NOT_SUPPORT_IMAGES";
const IMAGE_EXTENSIONS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]);
const VISION_MODEL_REQUIRED_MESSAGE = "The selected model does not support images. Choose a vision model and try again.";

function queuedInputFingerprint(
  sessionId: string,
  mode: SessionInputMode,
  text: string,
  attachments: FileAttachment[] | undefined,
): string {
  return JSON.stringify([
    sessionId,
    mode,
    text.trim(),
    (attachments ?? []).map((file) => [file.file_id, file.path, file.size]),
  ]);
}

function isImageAttachment(attachment: FileAttachment): boolean {
  if (attachment.mime_type?.startsWith("image/")) return true;
  const source = attachment.name || attachment.path || "";
  const dot = source.lastIndexOf(".");
  if (dot < 0) return false;
  return IMAGE_EXTENSIONS.has(source.slice(dot).toLowerCase());
}

export function hasImageAttachments(attachments?: FileAttachment[]): boolean {
  return !!attachments?.some(isImageAttachment);
}

export function selectedModelSupportsVision(
  models: ModelInfo[] | undefined,
  modelId: string | null,
  providerId: string | null,
): boolean {
  if (!modelId || !models) return false;
  const selected =
    models.find((model) => model.id === modelId && (!providerId || model.provider_id === providerId)) ??
    models.find((model) => model.id === modelId);
  return selected?.capabilities.vision === true;
}

function isUnsupportedImagesError(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  const detail = (err.body as { detail?: unknown } | undefined)?.detail;
  return (
    typeof detail === "object" &&
    detail !== null &&
    (detail as { code?: unknown }).code === MODEL_DOES_NOT_SUPPORT_IMAGES
  );
}

function respondErrorDetail(err: unknown): Record<string, unknown> | null {
  if (!(err instanceof ApiError)) return null;
  const detail = (err.body as { detail?: unknown } | undefined)?.detail;
  return typeof detail === "object" && detail !== null
    ? detail as Record<string, unknown>
    : null;
}

function interactionWasAcknowledgedOrDismissed(
  sessionId: string | null,
  promptType: "permission" | "question" | "plan",
  callId: string,
): boolean {
  const state = useChatStore.getState();
  const bucket = sessionId === null ? state.draftSession : state.sessions[sessionId];
  const request = promptType === "permission"
    ? bucket?.pendingPermission
    : promptType === "plan"
      ? bucket?.pendingPlanReview
      : bucket?.pendingQuestion;
  return (
    !request
    || request.callId !== callId
    || request.responseState === "resolved"
    || request.responseState === "continuing"
  );
}

function formatTaskBatchPrompt(batch: Pick<TaskBatchRequest, "mode" | "tasks">): string {
  const heading = i18n.t(batch.mode === "parallel" ? "taskBatchParallel" : "taskBatchSequential", {
    ns: "chat",
  });
  const lines = batch.tasks.map((task, index) => `${index + 1}. ${task.title}`);
  return [heading, ...lines].join("\n");
}

function desktopPermissionContext(
  queryClient: QueryClient,
  sessionId: string | null,
  fallbackWorkspace: string | null,
): SavedPermissionContext | null {
  if (sessionId) {
    const session = queryClient.getQueryData<SessionResponse>(
      queryKeys.sessions.detail(sessionId),
    );
    return {
      workspace: session
        ? normalizePermissionWorkspace(session.directory)
        : null,
      // Keep the session id even for workspace-backed sessions so a rule that
      // was safely remembered before session metadata loaded remains usable
      // only in that same conversation.
      sessionId,
      source: DESKTOP_PERMISSION_SOURCE,
    };
  }

  const workspace = normalizePermissionWorkspace(fallbackWorkspace);
  if (!workspace) return null;
  return {
    workspace,
    sessionId: null,
    source: DESKTOP_PERMISSION_SOURCE,
  };
}

function rememberedPermissionRules(
  queryClient: QueryClient,
  sessionId: string | null,
  settingsState: ReturnType<typeof useSettingsStore.getState>,
) {
  const context = desktopPermissionContext(
    queryClient,
    sessionId,
    settingsState.workspaceDirectory,
  );
  return context
    ? savedPermissionRulesForContext(settingsState.savedPermissions, context)
    : [];
}

function goalObjectivePreview(goal: SessionGoal | null): string | null {
  if (!goal) return null;
  const normalized = goal.objective.trim().split(/\s+/u).join(" ");
  return normalized.slice(0, 120) || null;
}

function updateSessionGoalSummary(
  queryClient: QueryClient,
  sessionId: string,
  goal: SessionGoal | null,
): void {
  const patch = {
    goal_status: goal?.status ?? null,
    goal_run_state: goal?.run_state ?? null,
    goal_needs_input: Boolean(goal?.needs_review || goal?.run_state === "waiting_user"),
    goal_objective_preview: goalObjectivePreview(goal),
  };
  queryClient.setQueryData<SessionResponse>(
    queryKeys.sessions.detail(sessionId),
    (current) => current ? { ...current, ...patch } : current,
  );
  queryClient.setQueryData<InfiniteData<SessionResponse[]>>(
    queryKeys.sessions.all,
    (current) => current
      ? {
          ...current,
          pages: current.pages.map((page) =>
            page.map((item) => item.id === sessionId ? { ...item, ...patch } : item),
          ),
        }
      : current,
  );
}

function writeGoalSnapshot(
  queryClient: QueryClient,
  incoming: SessionGoal,
): SessionGoal {
  const key = queryKeys.sessions.goal(incoming.session_id);
  const current = queryClient.getQueryData<SessionGoal | null>(key);
  const next = preferNewestGoalSnapshot(current, incoming);
  queryClient.setQueryData<SessionGoal | null>(key, next);
  updateSessionGoalSummary(queryClient, incoming.session_id, next);
  return next;
}

function clearGoalSnapshot(queryClient: QueryClient, sessionId: string): void {
  queryClient.setQueryData<SessionGoal | null>(queryKeys.sessions.goal(sessionId), null);
  updateSessionGoalSummary(queryClient, sessionId, null);
}

function goalCommandFailureKey(action: ParsedGoalCommand["action"]): string {
  if (action === "edit") return "goalUpdateFailed";
  if (action === "pause") return "goalPauseFailed";
  if (action === "resume") return "goalResumeFailed";
  if (action === "clear") return "goalClearFailed";
  if (action === "view") return "goalLoadFailed";
  return "goalCommandFailed";
}

/**
 * Core chat hook — orchestrates the prompt → stream → assemble cycle for one
 * session. When called from Landing without a sessionId, all state lives in
 * the draft bucket until the backend assigns an id; from then on the keyed
 * bucket takes over and the actual SSE stream is owned by the
 * SessionStreamRegistry, not by this hook.
 */
export function useChat(currentSessionId?: string) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const stopInFlightRef = useRef(false);

  // One subscription, one selector — re-renders only when the bucket reference
  // changes (i.e. when this session's state mutates).
  const session = useChatSession(currentSessionId ?? null);

  const { data: pendingInputs = [] } = useQuery({
    queryKey: queryKeys.sessionInputs(currentSessionId ?? "__draft__"),
    queryFn: async () => {
      if (!currentSessionId) return [];
      return sortSessionInputs(
        await api.get<SessionInputResponse[]>(API.CHAT.SESSION_INPUTS(currentSessionId)),
      );
    },
    enabled: !!currentSessionId,
    staleTime: 0,
    refetchOnMount: "always",
    refetchInterval: session.isGenerating ? 5_000 : false,
  });

  // Polling sync for streams started by other clients (e.g. mobile)
  useRemoteGenerationSync(currentSessionId);

  const handleGoalCommand = useCallback(
    async (
      command: ParsedGoalCommand,
      attachments?: FileAttachment[],
    ): Promise<boolean> => {
      const targetSessionId = currentSessionId ?? null;
      const reportFailure = (error: unknown): false => {
        console.error(`Failed to run Goal command ${command.action}:`, error);
        if (targetSessionId && error instanceof ApiError && error.status === 409) {
          void queryClient.invalidateQueries({
            queryKey: queryKeys.sessions.goal(targetSessionId),
          });
        }
        toast.error(i18n.t(goalCommandFailureKey(command.action), { ns: "chat" }));
        return false;
      };
      const readLatestGoal = async (): Promise<SessionGoal | null> => {
        if (!targetSessionId) return null;
        const latest = await getSessionGoal(targetSessionId);
        if (latest) return writeGoalSnapshot(queryClient, latest);
        clearGoalSnapshot(queryClient, targetSessionId);
        return null;
      };

      if (attachments?.length && command.action !== "create") {
        toast.error(i18n.t("goalCommandAttachmentsUnsupported", { ns: "chat" }));
        return false;
      }

      if (command.action === "view") {
        if (!targetSessionId) {
          toast.info(i18n.t("goalEmptyDescription", { ns: "chat" }));
          return true;
        }
        try {
          await readLatestGoal();
          // Opening the workspace handles desktop; the event reaches the
          // mobile-only sheet without introducing Goal state into the store.
          useWorkspaceStore.getState().open();
          requestOpenSessionGoal(targetSessionId);
          return true;
        } catch (error) {
          return reportFailure(error);
        }
      }

      if (command.action === "create") {
        const objective = command.objective?.trim();
        if (!objective) return reportFailure(new Error("Goal objective is unavailable"));

        const chatState = useChatStore.getState();
        const currentBucket = targetSessionId === null
          ? chatState.draftSession
          : chatState.sessions[targetSessionId];
        if (currentBucket?.isGenerating || currentBucket?.isCompacting) {
          return reportFailure(new Error("The conversation is busy"));
        }

        const settingsState = useSettingsStore.getState();
        if (
          hasImageAttachments(attachments) &&
          !selectedModelSupportsVision(
            queryClient.getQueryData<ModelInfo[]>(queryKeys.models),
            settingsState.selectedModel,
            settingsState.selectedProviderId,
          )
        ) {
          toast.error(VISION_MODEL_REQUIRED_MESSAGE);
          return false;
        }
        const presets = settingsState.permissionPresets;
        const permissionPresets = {
          file_changes: presets.fileChanges,
          run_commands: presets.runCommands,
        };
        const permissionRules = rememberedPermissionRules(
          queryClient,
          targetSessionId,
          settingsState,
        );
        const payload: Omit<GoalStartRequest, "client_request_id"> = {
          session_id: targetSessionId,
          objective,
          model: settingsState.selectedModel,
          provider_id: settingsState.selectedProviderId,
          agent: settingsState.selectedAgent,
          reasoning: settingsState.reasoningEnabled,
          workspace: settingsState.workspaceDirectory,
          attachments: attachments ?? [],
          permission_presets: Object.values(permissionPresets).some(Boolean)
            ? permissionPresets
            : null,
          permission_rules: permissionRules.length > 0 ? permissionRules : null,
        };
        const goalRequestScope = `goal-start:${targetSessionId ?? "__new__"}`;
        const clientRequestId = reservePromptRequestId(
          goalRequestScope,
          promptRequestFingerprint(payload),
        );

        if (!currentSessionId) chatState.resetSession(null);
        // A Goal objective is genuine user-authored conversation content.
        // Keep its optimistic bubble visible while admission is in flight so
        // the prior assistant turn cannot be mistaken for this Goal stream.
        chatState.beginSending(targetSessionId, objective, attachments);
        let response: Awaited<ReturnType<typeof startGoal>>;
        try {
          response = await startGoal({
            ...payload,
            client_request_id: clientRequestId,
          });
        } catch (error) {
          chatState.resetSession(targetSessionId);
          // Preserve this id across uncertain transport/5xx failures so a
          // retry converges on a durably admitted Goal instead of duplicating
          // the operation. A concrete 4xx is definitive and can release it.
          if (
            error instanceof ApiError
            && error.status >= 400
            && error.status < 500
          ) {
            clearPromptRequestId(goalRequestScope, clientRequestId);
          }
          return reportFailure(error);
        }

        // From here on the backend has durably accepted the Goal. Clear the
        // command's idempotency reservation and always treat the composer
        // command as accepted, even if a later UI refresh needs reconciliation.
        clearPromptRequestId(goalRequestScope, clientRequestId);
        writeGoalSnapshot(queryClient, response.goal);

        // The atomic endpoint has already admitted this exact stream. Seed
        // its bucket before SSE attachment so early events cannot be lost.
        chatState.startGeneration(response.session_id, response.stream_id);
        void startStream(response.session_id, response.stream_id);

        if (!currentSessionId) {
          const now = new Date().toISOString();
          const tempSession: SessionResponse = {
            id: response.session_id,
            project_id: null,
            parent_id: null,
            slug: null,
            directory: settingsState.workspaceDirectory || null,
            title: objective.slice(0, 60),
            version: 0,
            summary_additions: 0,
            summary_deletions: 0,
            summary_files: 0,
            summary_diffs: [],
            is_pinned: false,
            permission: {},
            model_id: settingsState.selectedModel,
            provider_id: settingsState.selectedProviderId,
            time_created: now,
            time_updated: now,
            time_compacting: null,
            time_archived: null,
            goal_status: response.goal.status,
            goal_run_state: response.goal.run_state,
            goal_needs_input:
              response.goal.needs_review || response.goal.run_state === "waiting_user",
            goal_objective_preview: goalObjectivePreview(response.goal),
          };
          queryClient.setQueryData<InfiniteData<SessionResponse[]>>(
            queryKeys.sessions.all,
            (old) => {
              if (!old) return { pages: [[tempSession]], pageParams: [0] };
              const pages = old.pages.map((page) =>
                page.filter((item) => item.id !== tempSession.id),
              );
              return {
                ...old,
                pages: [[tempSession, ...(pages[0] ?? [])], ...pages.slice(1)],
              };
            },
          );
          router.push(getChatRoute(response.session_id));
        }
        return true;
      }

      if (!targetSessionId) {
        toast.error(i18n.t("goalCommandSessionRequired", { ns: "chat" }));
        return false;
      }

      try {
        const latest = await readLatestGoal();
        if (!latest) throw new Error("Goal is unavailable");

        if (command.action === "edit") {
          const objective = command.objective?.trim();
          if (!objective) throw new Error("Goal objective is unavailable");
          const updated = await updateSessionGoal(targetSessionId, {
            objective,
            expected_revision: latest.revision,
            client_request_id: createGoalClientRequestId(),
          });
          writeGoalSnapshot(queryClient, updated);
          toast.success(i18n.t("goalUpdated", { ns: "chat" }));
          return true;
        }

        if (command.action === "pause") {
          const paused = await pauseSessionGoal(targetSessionId, {
            expected_revision: latest.revision,
            client_request_id: createGoalClientRequestId(),
          });
          writeGoalSnapshot(queryClient, paused);
          toast.success(i18n.t("goalPaused", { ns: "chat" }));
          return true;
        }

        if (command.action === "resume") {
          const resumed = await resumeSessionGoal(targetSessionId, {
            expected_revision: latest.revision,
            client_request_id: createGoalClientRequestId(),
          });
          const resumedGoal = isGoalStartResponse(resumed) ? resumed.goal : resumed;
          writeGoalSnapshot(queryClient, resumedGoal);
          if (isGoalStartResponse(resumed)) {
            useChatStore.getState().startGeneration(resumed.session_id, resumed.stream_id);
            void startStream(resumed.session_id, resumed.stream_id);
          }
          toast.success(i18n.t("goalResumed", { ns: "chat" }));
          return true;
        }

        if (["reserved", "running", "waiting_user", "pausing"].includes(latest.run_state)) {
          toast.error(i18n.t("goalClearPauseFirst", { ns: "chat" }));
          return false;
        }
        await clearSessionGoal(targetSessionId, {
          expected_revision: latest.revision,
          client_request_id: createGoalClientRequestId(),
        });
        clearGoalSnapshot(queryClient, targetSessionId);
        toast.success(i18n.t("goalCleared", { ns: "chat" }));
        return true;
      } catch (error) {
        return reportFailure(error);
      }
    },
    [currentSessionId, queryClient, router],
  );

  const sendMessage = useCallback(
    async (text: string, attachments?: FileAttachment[]): Promise<boolean> => {
      const chatState = useChatStore.getState();
      const settingsState = useSettingsStore.getState();
      const targetSessionId = currentSessionId ?? null;
      const currentBucket = targetSessionId === null
        ? chatState.draftSession
        : chatState.sessions[targetSessionId];

      if (currentBucket?.isGenerating || currentBucket?.isCompacting || (!text.trim() && (!attachments || attachments.length === 0))) {
        return false;
      }
      if (
        hasImageAttachments(attachments) &&
        !selectedModelSupportsVision(
          queryClient.getQueryData<ModelInfo[]>(queryKeys.models),
          settingsState.selectedModel,
          settingsState.selectedProviderId,
        )
      ) {
        toast.error(VISION_MODEL_REQUIRED_MESSAGE);
        return false;
      }

      // New chat must start from a clean draft.
      if (!currentSessionId) {
        chatState.resetSession(null);
      }

      // Starting a fresh generation invalidates any side panels showing the
      // previous assistant response.
      useActivityStore.getState().close();
      try {
        const { useArtifactStore } = require("@/stores/artifact-store");
        useArtifactStore.getState().close();
      } catch {}
      try {
        const { usePlanReviewStore } = require("@/stores/plan-review-store");
        usePlanReviewStore.getState().close();
      } catch {}

      chatState.beginSending(targetSessionId, text.trim(), attachments);

      const promptRequestScope = currentSessionId ?? "__new__";
      let promptRequestId: string | null = null;
      try {
        const presets = settingsState.permissionPresets;
        const permissionPresets = {
          file_changes: presets.fileChanges,
          run_commands: presets.runCommands,
        };
        const hasActivePresets = Object.values(permissionPresets).some(Boolean);
        const permissionRules = rememberedPermissionRules(
          queryClient,
          currentSessionId ?? null,
          settingsState,
        );

        const promptPayload: PromptRequest = {
          text: text.trim(),
          session_id: currentSessionId ?? null,
          model: settingsState.selectedModel,
          provider_id: settingsState.selectedProviderId,
          agent: settingsState.selectedAgent,
          attachments: attachments ?? [],
          permission_presets: hasActivePresets ? permissionPresets : null,
          permission_rules: permissionRules.length > 0 ? permissionRules : null,
          reasoning: settingsState.reasoningEnabled,
          workspace: settingsState.workspaceDirectory,
        };
        promptRequestId = reservePromptRequestId(
          promptRequestScope,
          promptRequestFingerprint(promptPayload),
        );
        const res = await api.post<PromptResponse>(
          API.CHAT.PROMPT,
          { ...promptPayload, client_request_id: promptRequestId },
          // The backend durably binds this key to one session/stream before
          // execution, so a lost HTTP response can be retried safely.
          { retryNetworkErrors: true },
        );
        clearPromptRequestId(promptRequestScope, promptRequestId);

        // Seed the keyed bucket (carries over the draft contents if any) and
        // attach the SSE stream. Order matters: store update first so the
        // registry's handlers see a bucket they can write into.
        chatState.startGeneration(res.session_id, res.stream_id);
        void startStream(res.session_id, res.stream_id);

        if (!currentSessionId) {
          const tempSession: SessionResponse = {
            id: res.session_id,
            project_id: null,
            parent_id: null,
            slug: null,
            directory: settingsState.workspaceDirectory || null,
            title: text.trim().slice(0, 60),
            version: 0,
            summary_additions: 0,
            summary_deletions: 0,
            summary_files: 0,
            summary_diffs: [],
            is_pinned: false,
            permission: {},
            model_id: settingsState.selectedModel,
            provider_id: settingsState.selectedProviderId,
            time_created: new Date().toISOString(),
            time_updated: new Date().toISOString(),
            time_compacting: null,
            time_archived: null,
            goal_status: null,
            goal_run_state: null,
            goal_needs_input: false,
            goal_objective_preview: null,
          };
          queryClient.setQueryData<InfiniteData<SessionResponse[]>>(
            queryKeys.sessions.all,
            (old) => {
              if (!old) return { pages: [[tempSession]], pageParams: [0] };
              return {
                ...old,
                pages: [[tempSession, ...old.pages[0]], ...old.pages.slice(1)],
              };
            },
          );
          router.push(getChatRoute(res.session_id));
        }
        return true;
      } catch (err) {
        console.error("Failed to start generation:", err);
        chatState.resetSession(targetSessionId);

        // A concrete client error proves the request was not ambiguously
        // accepted. Keep the key across network/timeout/5xx failures so the
        // user's next click converges on any already-committed execution.
        if (
          promptRequestId
          && err instanceof ApiError
          && err.status >= 400
          && err.status < 500
        ) {
          clearPromptRequestId(promptRequestScope, promptRequestId);
        }

        if (err instanceof ApiError) {
          if (isUnsupportedImagesError(err)) {
            toast.error(VISION_MODEL_REQUIRED_MESSAGE);
            return false;
          }
          toast.error(err.message, { duration: 8000 });
          return false;
        }

        toast.error("Failed to send message", { duration: 8000 });
        return false;
      }
    },
    [currentSessionId, router, queryClient],
  );

  const sendTaskBatch = useCallback(
    async (batch: Pick<TaskBatchRequest, "mode" | "tasks">): Promise<boolean> => {
      const chatState = useChatStore.getState();
      const settingsState = useSettingsStore.getState();
      const targetSessionId = currentSessionId ?? null;
      const currentBucket = targetSessionId === null
        ? chatState.draftSession
        : chatState.sessions[targetSessionId];

      const tasks = batch.tasks
        .map((task) => ({
          ...task,
          title: task.title.trim(),
          prompt: task.prompt.trim(),
          agent: task.agent || settingsState.selectedAgent,
          model: task.model || settingsState.selectedModel,
          provider_id: task.provider_id || settingsState.selectedProviderId,
        }))
        .filter((task) => task.title && task.prompt);

      if (currentBucket?.isGenerating || currentBucket?.isCompacting || tasks.length === 0) return false;

      if (!currentSessionId) {
        chatState.resetSession(null);
      }

      useActivityStore.getState().close();
      try {
        const { useArtifactStore } = require("@/stores/artifact-store");
        useArtifactStore.getState().close();
      } catch {}
      try {
        const { usePlanReviewStore } = require("@/stores/plan-review-store");
        usePlanReviewStore.getState().close();
      } catch {}

      const optimisticText = formatTaskBatchPrompt({ mode: batch.mode, tasks });
      chatState.beginSending(targetSessionId, optimisticText);

      try {
        const presets = settingsState.permissionPresets;
        const permissionPresets = {
          file_changes: presets.fileChanges,
          run_commands: presets.runCommands,
        };
        const hasActivePresets = Object.values(permissionPresets).some(Boolean);
        const permissionRules = rememberedPermissionRules(
          queryClient,
          currentSessionId ?? null,
          settingsState,
        );
        const res = await api.post<PromptResponse>(API.CHAT.TASK_BATCH, {
          session_id: currentSessionId ?? null,
          mode: batch.mode,
          tasks,
          workspace: settingsState.workspaceDirectory,
          permission_presets: hasActivePresets ? permissionPresets : null,
          permission_rules: permissionRules.length > 0 ? permissionRules : null,
        });

        chatState.startGeneration(res.session_id, res.stream_id);
        void startStream(res.session_id, res.stream_id);

        if (!currentSessionId) {
          const tempSession: SessionResponse = {
            id: res.session_id,
            project_id: null,
            parent_id: null,
            slug: null,
            directory: settingsState.workspaceDirectory || null,
            title: tasks[0]?.title?.slice(0, 60) || "Multi-agent task batch",
            version: 0,
            summary_additions: 0,
            summary_deletions: 0,
            summary_files: 0,
            summary_diffs: [],
            is_pinned: false,
            permission: {},
            model_id: settingsState.selectedModel,
            provider_id: settingsState.selectedProviderId,
            time_created: new Date().toISOString(),
            time_updated: new Date().toISOString(),
            time_compacting: null,
            time_archived: null,
            goal_status: null,
            goal_run_state: null,
            goal_needs_input: false,
            goal_objective_preview: null,
          };
          queryClient.setQueryData<InfiniteData<SessionResponse[]>>(
            queryKeys.sessions.all,
            (old) => {
              if (!old) return { pages: [[tempSession]], pageParams: [0] };
              return {
                ...old,
                pages: [[tempSession, ...old.pages[0]], ...old.pages.slice(1)],
              };
            },
          );
          router.push(getChatRoute(res.session_id));
        }
        return true;
      } catch (err) {
        console.error("Failed to start task batch:", err);
        chatState.resetSession(targetSessionId);

        if (err instanceof ApiError) {
          toast.error(err.message, { duration: 8000 });
          return false;
        }

        toast.error("Failed to start task batch", { duration: 8000 });
        return false;
      }
    },
    [currentSessionId, router, queryClient],
  );

  const queueMessage = useCallback(
    async (
      text: string,
      attachments?: FileAttachment[],
      mode: SessionInputMode = "queue",
    ): Promise<boolean> => {
      if (!currentSessionId || (!text.trim() && (!attachments || attachments.length === 0))) {
        return false;
      }

      const settingsState = useSettingsStore.getState();
      if (
        hasImageAttachments(attachments) &&
        !selectedModelSupportsVision(
          queryClient.getQueryData<ModelInfo[]>(queryKeys.models),
          settingsState.selectedModel,
          settingsState.selectedProviderId,
        )
      ) {
        toast.error(VISION_MODEL_REQUIRED_MESSAGE);
        return false;
      }

      const presets = settingsState.permissionPresets;
      const permissionPresets = {
        file_changes: presets.fileChanges,
        run_commands: presets.runCommands,
      };
      const hasActivePresets = Object.values(permissionPresets).some(Boolean);
      const permissionRules = rememberedPermissionRules(
        queryClient,
        currentSessionId,
        settingsState,
      );
      const fingerprint = queuedInputFingerprint(currentSessionId, mode, text, attachments);
      const clientRequestId = reserveSessionInputRequestId(fingerprint);

      try {
        const queued = await api.post<SessionInputResponse>(
          API.CHAT.INPUTS,
          {
            session_id: currentSessionId,
            client_request_id: clientRequestId,
            mode,
            text: text.trim(),
            attachments: attachments ?? [],
            model: settingsState.selectedModel,
            provider_id: settingsState.selectedProviderId,
            agent: settingsState.selectedAgent,
            permission_presets: hasActivePresets ? permissionPresets : null,
            permission_rules: permissionRules.length > 0 ? permissionRules : null,
            reasoning: settingsState.reasoningEnabled,
            workspace: settingsState.workspaceDirectory,
          },
          // Safe because client_request_id is a backend-enforced idempotency key.
          { retryNetworkErrors: true },
        );
        clearSessionInputRequestId(fingerprint, clientRequestId);
        queryClient.setQueryData<SessionInputResponse[]>(
          queryKeys.sessionInputs(currentSessionId),
          (old) => upsertSessionInput(old, queued),
        );
        if (queued.status === "failed" || queued.status === "cancelled") {
          toast.error(
            queued.error_message
              ? i18n.t("inputExecutionFailedWithReason", {
                  ns: "chat",
                  reason: queued.error_message,
                })
              : i18n.t("inputExecutionFailed", { ns: "chat" }),
          );
          // The idempotent replay found a terminal failure. Restore the
          // composer so an explicit next click can submit a fresh request id.
          return false;
        }
        if (queued.status === "consumed") {
          queryClient.invalidateQueries({
            queryKey: queryKeys.messages.list(currentSessionId),
          });
          toast.info(i18n.t("inputAlreadyCompleted", { ns: "chat" }));
          return true;
        }
        toast.success(i18n.t(mode === "steer" ? "inputSteerSubmitted" : "inputQueued", { ns: "chat" }));
        return true;
      } catch (err) {
        const detail = respondErrorDetail(err);
        if (
          err instanceof ApiError
          && err.status >= 400
          && err.status < 500
        ) {
          // A concrete client error is definitive. Keep the key for 5xx,
          // timeout and transport failures because the server may already
          // have committed the queued input before the response was lost.
          clearSessionInputRequestId(fingerprint, clientRequestId);
        }

        if (err instanceof ApiError && err.status === 409 && detail?.code === "session_idle") {
          // The task finished between the user's click and the enqueue request.
          // Detach the stale stream before starting the ordinary follow-up so a
          // late DONE from the previous stream cannot clear the new generation.
          stopStream(currentSessionId);
          useChatStore.getState().finishGeneration(currentSessionId);
          const sent = await sendMessage(text, attachments);
          if (sent) {
            toast.info(i18n.t("inputSentAfterTaskFinished", { ns: "chat" }));
          }
          return sent;
        }

        console.error("Failed to queue follow-up:", err);
        toast.error(i18n.t("inputQueueFailed", { ns: "chat" }));
        return false;
      }
    },
    [currentSessionId, queryClient, sendMessage],
  );

  const cancelQueuedInput = useCallback(
    async (inputId: string): Promise<boolean> => {
      if (!currentSessionId) return false;
      const key = queryKeys.sessionInputs(currentSessionId);
      const previous = queryClient.getQueryData<SessionInputResponse[]>(key);
      queryClient.setQueryData<SessionInputResponse[]>(key, (old) =>
        removeSessionInput(old, inputId),
      );
      try {
        await api.delete(API.CHAT.SESSION_INPUT(currentSessionId, inputId));
        return true;
      } catch (err) {
        // DELETE may have committed before its response was lost. Reconcile
        // with the durable list before restoring the optimistic row; absence
        // proves cancellation and makes move-back-to-composer lossless.
        try {
          const latest = sortSessionInputs(
            await api.get<SessionInputResponse[]>(
              API.CHAT.SESSION_INPUTS(currentSessionId),
            ),
          );
          queryClient.setQueryData(key, latest);
          if (!latest.some((item) => item.id === inputId)) return true;
        } catch {
          // Fall through to the original row when reconciliation is unavailable.
        }
        queryClient.setQueryData(key, previous);
        console.error("Failed to cancel queued input:", err);
        toast.error(i18n.t("inputCancelFailed", { ns: "chat" }));
        return false;
      } finally {
        queryClient.invalidateQueries({ queryKey: key });
      }
    },
    [currentSessionId, queryClient],
  );

  const updateQueuedInput = useCallback(
    async (
      inputId: string,
      update: SessionInputUpdateRequest,
    ): Promise<boolean> => {
      if (!currentSessionId) return false;
      const key = queryKeys.sessionInputs(currentSessionId);
      const previous = queryClient.getQueryData<SessionInputResponse[]>(key);

      queryClient.setQueryData<SessionInputResponse[]>(key, (old) => {
        const items = sortSessionInputs(old ?? []);
        const index = items.findIndex((item) => item.id === inputId);
        if (index < 0) return items;
        if (update.mode) {
          items[index] = { ...items[index], mode: update.mode };
        }
        if (update.move) {
          const neighborIndex = update.move === "up" ? index - 1 : index + 1;
          if (neighborIndex >= 0 && neighborIndex < items.length) {
            [items[index], items[neighborIndex]] = [items[neighborIndex], items[index]];
          }
        }
        if (update.position) {
          const [moving] = items.splice(index, 1);
          const targetIndex = Math.min(Math.max(update.position - 1, 0), items.length);
          items.splice(targetIndex, 0, moving);
        }
        return items;
      });

      try {
        await api.patch<SessionInputResponse>(
          API.CHAT.SESSION_INPUT(currentSessionId, inputId),
          update,
        );
        if (update.mode === "steer") {
          toast.success(i18n.t("inputSteerSubmitted", { ns: "chat" }));
        }
        await queryClient.invalidateQueries({ queryKey: key });
        return true;
      } catch (err) {
        queryClient.setQueryData(key, previous);
        console.error("Failed to update queued input:", err);
        toast.error(i18n.t("inputUpdateFailed", { ns: "chat" }));
        return false;
      }
    },
    [currentSessionId, queryClient],
  );

  const stopGeneration = useCallback(async (): Promise<boolean> => {
    if (stopInFlightRef.current) return false;
    const chatState = useChatStore.getState();
    const targetSessionId = currentSessionId ?? null;
    const bucket = targetSessionId === null
      ? chatState.draftSession
      : chatState.sessions[targetSessionId];
    const streamId = bucket?.streamId;
    if (!streamId) return false;
    stopInFlightRef.current = true;
    try {
      if (targetSessionId) {
        const goalKey = queryKeys.sessions.goal(targetSessionId);
        const cachedGoal = queryClient.getQueryData<SessionGoal | null>(goalKey);
        let latestGoal: SessionGoal | null = null;
        let latestGoalReadSucceeded = false;

        try {
          // Pause is a revision-CAS operation. Always read the authoritative
          // revision immediately before it instead of trusting a header card
          // or an earlier SSE snapshot.
          latestGoal = await getSessionGoal(targetSessionId);
          latestGoalReadSucceeded = true;
          if (latestGoal) latestGoal = writeGoalSnapshot(queryClient, latestGoal);
          else clearGoalSnapshot(queryClient, targetSessionId);
        } catch (error) {
          // If the cached Goal proves this stream is autonomous, fail closed:
          // never turn a transient Goal-read failure into an emergency abort.
          if (isActiveGoalStream(cachedGoal, streamId)) {
            console.error("Could not read the latest Goal revision before pausing:", error);
            toast.error(i18n.t("goalPauseFailed", { ns: "chat" }));
            return false;
          }
        }

        if (
          latestGoalReadSucceeded
          && isActiveGoalStream(latestGoal, streamId)
        ) {
          try {
            const paused = await pauseSessionGoal(targetSessionId, {
              expected_revision: latestGoal.revision,
              client_request_id: createGoalClientRequestId(),
            });
            writeGoalSnapshot(queryClient, paused);
            toast.success(i18n.t("goalPaused", { ns: "chat" }));
            // Keep SSE attached and keep the generation bucket running. The
            // Goal worker stops at its next safe boundary and emits its own
            // durable terminal events; /abort must never run on this branch.
            return true;
          } catch (error) {
            console.error("Failed to request a safe Goal pause:", error);
            if (error instanceof ApiError && error.status === 409) {
              void queryClient.invalidateQueries({ queryKey: goalKey });
            }
            toast.error(i18n.t("goalPauseFailed", { ns: "chat" }));
            return false;
          }
        }
      }

      try {
        const result = await api.post<{ status: "aborted" | "not_found" }>(
          API.CHAT.ABORT,
          { stream_id: streamId },
          // Aborting the same ordinary stream more than once has the same
          // outcome, so a response lost at the network boundary is safe to retry.
          { retryNetworkErrors: true },
        );
        if (result.status !== "aborted" && result.status !== "not_found") {
          throw new Error(`Unexpected abort status: ${String(result.status)}`);
        }
      } catch (error) {
        // Truthful stop semantics: if the backend did not acknowledge the abort,
        // keep the stream attached and let the user retry Stop.
        console.error("Failed to abort — backend may still be generating:", error);
        toast.error(i18n.t("stopFailed", { ns: "chat" }));
        return false;
      }
    } finally {
      stopInFlightRef.current = false;
    }
    // Stop the SSE stream and clear local state immediately — don't wait for
    // backend DONE (backend may delay DONE while doing post-generation work
    // like title generation).
    if (targetSessionId !== null) stopStream(targetSessionId);
    chatState.finishGeneration(targetSessionId);

    const ws = useWorkspaceStore.getState();
    if (ws.todos.some((t) => t.status === "in_progress")) {
      ws.setTodos(
        ws.todos.map((t) =>
          t.status === "in_progress" ? { ...t, status: "pending" as const, activeForm: undefined } : t,
        ),
      );
    }
    if (targetSessionId) {
      queryClient.invalidateQueries({ queryKey: queryKeys.messages.list(targetSessionId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.sessions.detail(targetSessionId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.sessionInputs(targetSessionId) });
    }
    queryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
    return true;
  }, [currentSessionId, queryClient]);

  const reconnectGeneration = useCallback(() => {
    if (!currentSessionId || !reconnectStream(currentSessionId)) return false;
    toast.info(i18n.t("taskReconnectStarted", { ns: "chat" }));
    return true;
  }, [currentSessionId]);

  const recoverInteraction = useCallback((
    promptType: InteractionPromptType,
    callId: string,
  ) => {
    if (!currentSessionId) return false;
    const streamId = useChatStore.getState().sessions[currentSessionId]?.streamId;
    if (!streamId) return false;
    const started = recoverInteractionState(
      currentSessionId,
      streamId,
      promptType,
      callId,
    );
    if (started) {
      toast.info(i18n.t("taskReconnectStarted", { ns: "chat" }));
    }
    return started;
  }, [currentSessionId]);

  const respondToPermission = useCallback(
    async (allow: boolean, remember = false) => {
      const chatState = useChatStore.getState();
      const targetSessionId = currentSessionId ?? null;
      const bucket = targetSessionId === null
        ? chatState.draftSession
        : chatState.sessions[targetSessionId];
      const perm = bucket?.pendingPermission;
      const streamId = bucket?.streamId;
      if (!perm || !streamId || !canSubmitInteraction(perm.responseState)) return;

      const explicitPattern = perm.patterns.find(
        (pattern) => typeof pattern === "string" && Boolean(pattern.trim()),
      );
      const settingsState = useSettingsStore.getState();
      const permissionContext = desktopPermissionContext(
        queryClient,
        targetSessionId,
        settingsState.workspaceDirectory,
      );
      const shouldRemember = Boolean(
        remember && explicitPattern && permissionContext,
      );

      const req: RespondRequest = {
        stream_id: streamId,
        call_id: perm.callId,
        response: {
          allowed: allow,
          remember: shouldRemember,
          permission: perm.tool || perm.permission,
          pattern: explicitPattern ?? "*",
        },
      };

      try {
        chatState.setInteractionResponseState(
          targetSessionId,
          "permission",
          perm.callId,
          "submitting",
        );
        const result = await api.post<RespondResult>(API.CHAT.RESPOND, req);
        chatState.setInteractionResponseState(
          targetSessionId,
          "permission",
          perm.callId,
          "resolved",
          { decision: result.decision, source: result.source },
        );
        if (shouldRemember && explicitPattern && permissionContext) {
          useSettingsStore.getState().savePermissionRule({
            tool: perm.tool || perm.permission,
            allow,
            pattern: explicitPattern,
            ...permissionContext,
          });
        }
      } catch (err) {
        const detail = respondErrorDetail(err);
        if (detail?.code === "response_conflict") {
          chatState.setInteractionResponseState(
            targetSessionId,
            "permission",
            perm.callId,
            "resolved",
            {
              decision: typeof detail.existing_decision === "string"
                ? detail.existing_decision
                : null,
              source: typeof detail.source === "string" ? detail.source : null,
            },
          );
        } else {
          chatState.resetInteractionResponseState(
            targetSessionId,
            "permission",
            perm.callId,
          );
          if (interactionWasAcknowledgedOrDismissed(
            targetSessionId,
            "permission",
            perm.callId,
          )) return;
        }
        console.error("Failed to respond to permission:", err);
        toast.error(
          typeof detail?.message === "string" ? detail.message : "Failed to respond",
        );
      }
    },
    [currentSessionId, queryClient],
  );

  const editAndResend = useCallback(
    async (messageId: string, newText: string, attachments?: FileAttachment[]): Promise<EditAndResendResult> => {
      const chatState = useChatStore.getState();
      const settingsState = useSettingsStore.getState();
      const bucket = currentSessionId ? chatState.sessions[currentSessionId] : null;

      if (bucket?.isGenerating || bucket?.isCompacting || (!newText.trim() && (!attachments || attachments.length === 0)) || !currentSessionId) return { status: "failed" };
      if (
        hasImageAttachments(attachments) &&
        !selectedModelSupportsVision(
          queryClient.getQueryData<ModelInfo[]>(queryKeys.models),
          settingsState.selectedModel,
          settingsState.selectedProviderId,
        )
      ) {
        toast.error(VISION_MODEL_REQUIRED_MESSAGE);
        return { status: "failed" };
      }

      useActivityStore.getState().close();
      try {
        const { useArtifactStore } = require("@/stores/artifact-store");
        useArtifactStore.getState().close();
      } catch {}
      try {
        const { usePlanReviewStore } = require("@/stores/plan-review-store");
        usePlanReviewStore.getState().close();
      } catch {}

      chatState.beginSending(currentSessionId, newText.trim(), attachments);

      let editCommitted = false;
      try {
        const presets = settingsState.permissionPresets;
        const permissionPresets = {
          file_changes: presets.fileChanges,
          run_commands: presets.runCommands,
        };
        const hasActivePresets = Object.values(permissionPresets).some(Boolean);
        const permissionRules = rememberedPermissionRules(
          queryClient,
          currentSessionId,
          settingsState,
        );

        const res = await api.post<PromptResponse>(API.CHAT.EDIT, {
          session_id: currentSessionId,
          message_id: messageId,
          text: newText.trim(),
          model: settingsState.selectedModel,
          provider_id: settingsState.selectedProviderId,
          agent: settingsState.selectedAgent,
          attachments: attachments ?? [],
          permission_presets: hasActivePresets ? permissionPresets : null,
          permission_rules: permissionRules.length > 0 ? permissionRules : null,
          reasoning: settingsState.reasoningEnabled,
          workspace: settingsState.workspaceDirectory,
        });
        editCommitted = true;

        chatState.startGeneration(res.session_id, res.stream_id);
        void startStream(res.session_id, res.stream_id);

        useWorkspaceStore.getState().setTodos([]);
        useWorkspaceStore.getState().setWorkspaceFiles([]);

        const trimmed = newText.trim();
        const liveCacheBeforeEdit = queryClient.getQueryData<
          InfiniteData<PaginatedMessages>
        >(queryKeys.messages.list(currentSessionId));
        const targetWasInLiveCache = !!liveCacheBeforeEdit?.pages.some((page) =>
          page.messages.some((message) => message.id === messageId),
        );
        queryClient.setQueryData<InfiniteData<PaginatedMessages>>(
          queryKeys.messages.list(currentSessionId),
          (old) => {
            if (!old) return old;
            const newPages = old.pages.map((page) => {
              const idx = page.messages.findIndex((m) => m.id === messageId);
              if (idx === -1) return page;
              return {
                ...page,
                messages: page.messages.slice(0, idx + 1).map((m, i) => {
                  if (i !== idx) return m;
                  return {
                    ...m,
                    parts: m.parts.map((p) =>
                      p.data.type === "text"
                        ? { ...p, data: { ...p.data, text: trimmed } }
                        : p,
                    ),
                  };
                }),
              };
            });
            const pageIdx = newPages.findIndex((p) =>
              p.messages.some((m) => m.id === messageId),
            );
            return {
              ...old,
              pages: pageIdx >= 0 ? newPages.slice(0, pageIdx + 1) : newPages,
              pageParams: pageIdx >= 0 ? old.pageParams.slice(0, pageIdx + 1) : old.pageParams,
            };
          },
        );
        const normalizedSummary = trimmed.replace(/\s+/g, " ");
        const summary = normalizedSummary.length <= 160
          ? normalizedSummary
          : `${normalizedSummary.slice(0, 159).trimEnd()}…`;
        queryClient.setQueryData<ConversationTurnIndex>(
          queryKeys.messages.turnIndex(currentSessionId),
          (old) => {
            if (!old) return old;
            const turnIndex = old.turns.findIndex(
              (turn) => turn.message_id === messageId,
            );
            if (turnIndex < 0) return old;
            const turns = old.turns.slice(0, turnIndex + 1).map((turn, index) =>
              index === turnIndex
                ? {
                    ...turn,
                    summary,
                    attachment_names: (attachments ?? []).map((file) => file.name),
                  }
                : turn,
            );
            return {
              total_messages: turns.at(-1)!.message_offset + 1,
              total_turns: turns.length,
              turns,
            };
          },
        );
        void queryClient.invalidateQueries({
          queryKey: queryKeys.messages.turnIndex(currentSessionId),
          exact: true,
        });
        // No pending bubble needed — the edited message is already in cache.
        // Clear it explicitly on this session's bucket.
        useChatStore.setState((s) => {
          const cur = s.sessions[currentSessionId];
          if (!cur) return s;
          return {
            sessions: {
              ...s.sessions,
              [currentSessionId]: { ...cur, pendingUserText: null, pendingAttachments: null },
            },
          };
        });

        try {
          // Editing an early message can happen entirely inside the isolated
          // history window, so the live cache may not contain the target and
          // cannot be safely truncated client-side. Wait for the committed
          // latest page, then replace pages/pageParams together in one write.
          const authoritativeLatest = await api.get<PaginatedMessages>(
            API.MESSAGES.LIST(currentSessionId, 50, -1),
            { timeoutMs: 10_000, retryNetworkErrors: false },
          );
          queryClient.setQueryData<InfiniteData<PaginatedMessages>>(
            queryKeys.messages.list(currentSessionId),
            {
              pages: [authoritativeLatest],
              pageParams: [-1],
            },
          );
          return { status: "reconciled" };
        } catch (reconcileError) {
          // Never expose an untouched pre-edit latest page. If the target was
          // absent from live cache, replace that unsafe cache with an empty
          // latest shell; active polling/stream completion can reconcile it
          // later, while the user remains on the truthful history window.
          const currentLiveCache = queryClient.getQueryData<
            InfiniteData<PaginatedMessages>
          >(queryKeys.messages.list(currentSessionId));
          const currentContainsTarget = !!currentLiveCache?.pages.some((page) =>
            page.messages.some((message) => message.id === messageId),
          );
          if (!targetWasInLiveCache && !currentContainsTarget) {
            queryClient.setQueryData<InfiniteData<PaginatedMessages>>(
              queryKeys.messages.list(currentSessionId),
              {
                pages: [{ total: 0, offset: 0, messages: [] }],
                pageParams: [-1],
              },
            );
          }
          console.error(
            "Edit committed but latest-page reconciliation failed:",
            reconcileError,
          );
          toast.error(i18n.t("conversationEditReconcileFailed", { ns: "chat" }));
          return { status: "committed_unreconciled" };
        }
      } catch (err) {
        console.error("Failed to edit and resend:", err);
        if (editCommitted) {
          queryClient.setQueryData<InfiniteData<PaginatedMessages>>(
            queryKeys.messages.list(currentSessionId),
            {
              pages: [{ total: 0, offset: 0, messages: [] }],
              pageParams: [-1],
            },
          );
          toast.error(i18n.t("conversationEditReconcileFailed", { ns: "chat" }));
          return { status: "committed_unreconciled" };
        }
        chatState.resetSession(currentSessionId);

        if (err instanceof ApiError) {
          if (isUnsupportedImagesError(err)) {
            toast.error(VISION_MODEL_REQUIRED_MESSAGE);
            return { status: "failed" };
          }
          toast.error(err.message);
          return { status: "failed" };
        }

        toast.error("Failed to edit message");
        return { status: "failed" };
      }
    },
    [currentSessionId, queryClient],
  );

  const respondToQuestion = useCallback(
    async (answer: string | Record<string, string>) => {
      const chatState = useChatStore.getState();
      const targetSessionId = currentSessionId ?? null;
      const bucket = targetSessionId === null
        ? chatState.draftSession
        : chatState.sessions[targetSessionId];
      const question = bucket?.pendingQuestion;
      const streamId = bucket?.streamId;
      if (!question || !streamId || !canSubmitInteraction(question.responseState)) return;

      const response =
        typeof answer === "string" ? answer.trim() : JSON.stringify(answer);
      if (!response) return;

      const req: RespondRequest = {
        stream_id: streamId,
        call_id: question.callId,
        response,
      };

      try {
        chatState.setInteractionResponseState(
          targetSessionId,
          "question",
          question.callId,
          "submitting",
        );
        const result = await api.post<RespondResult>(API.CHAT.RESPOND, req);
        chatState.setInteractionResponseState(
          targetSessionId,
          "question",
          question.callId,
          "resolved",
          { decision: result.decision, source: result.source },
        );
      } catch (err) {
        const detail = respondErrorDetail(err);
        if (detail?.code === "response_conflict") {
          chatState.setInteractionResponseState(
            targetSessionId,
            "question",
            question.callId,
            "resolved",
            {
              decision: typeof detail.existing_decision === "string"
                ? detail.existing_decision
                : null,
              source: typeof detail.source === "string" ? detail.source : null,
            },
          );
        } else {
          chatState.resetInteractionResponseState(
            targetSessionId,
            "question",
            question.callId,
          );
          if (interactionWasAcknowledgedOrDismissed(
            targetSessionId,
            "question",
            question.callId,
          )) return;
        }
        console.error("Failed to respond to question:", err);
        toast.error(
          typeof detail?.message === "string" ? detail.message : "Failed to respond",
        );
      }
    },
    [currentSessionId],
  );

  const respondToPlanReview = useCallback(
    async (action: "accept" | "revise" | "stop", options?: { mode?: "auto" | "ask"; feedback?: string }) => {
      const chatState = useChatStore.getState();
      const targetSessionId = currentSessionId ?? null;
      const bucket = targetSessionId === null
        ? chatState.draftSession
        : chatState.sessions[targetSessionId];
      const review = bucket?.pendingPlanReview;
      const streamId = bucket?.streamId;
      if (!review || !streamId || !canSubmitInteraction(review.responseState)) return;

      let response: Record<string, string>;
      if (action === "accept") {
        response = { action: "accept", mode: options?.mode ?? "auto" };
      } else if (action === "stop") {
        response = { action: "stop" };
      } else {
        response = { action: "revise", feedback: options?.feedback ?? "" };
      }

      const req: RespondRequest = {
        stream_id: streamId,
        call_id: review.callId,
        response: JSON.stringify(response),
      };

      try {
        chatState.setInteractionResponseState(
          targetSessionId,
          "plan",
          review.callId,
          "submitting",
        );
        const result = await api.post<RespondResult>(API.CHAT.RESPOND, req);
        chatState.setInteractionResponseState(
          targetSessionId,
          "plan",
          review.callId,
          "resolved",
          { decision: result.decision, source: result.source },
        );

        if (action === "accept") {
          try {
            const { usePlanReviewStore } = require("@/stores/plan-review-store");
            usePlanReviewStore.getState().close();
          } catch {}
          useSettingsStore.getState().setWorkMode(options?.mode ?? "auto");
        }
      } catch (err) {
        const detail = respondErrorDetail(err);
        if (detail?.code === "response_conflict") {
          chatState.setInteractionResponseState(
            targetSessionId,
            "plan",
            review.callId,
            "resolved",
            {
              decision: typeof detail.existing_decision === "string"
                ? detail.existing_decision
                : null,
              source: typeof detail.source === "string" ? detail.source : null,
            },
          );
        } else {
          chatState.resetInteractionResponseState(
            targetSessionId,
            "plan",
            review.callId,
          );
          if (interactionWasAcknowledgedOrDismissed(
            targetSessionId,
            "plan",
            review.callId,
          )) return;
        }
        console.error("Failed to respond to plan review:", err);
        toast.error(
          typeof detail?.message === "string" ? detail.message : "Failed to respond",
        );
      }
    },
    [currentSessionId],
  );

  return {
    sendMessage,
    handleGoalCommand,
    queueMessage,
    cancelQueuedInput,
    updateQueuedInput,
    pendingInputs,
    sendTaskBatch,
    editAndResend,
    stopGeneration,
    reconnectGeneration,
    recoverInteraction,
    respondToPermission,
    respondToQuestion,
    respondToPlanReview,
    isGenerating: session.isGenerating,
    isCompacting: session.isCompacting,
    streamId: session.streamId,
    pendingUserText: session.pendingUserText,
    pendingAttachments: session.pendingAttachments,
    streamingParts: session.streamingParts,
    streamingText: session.streamingText,
    streamingReasoning: session.streamingReasoning,
    pendingPermission: session.pendingPermission,
    pendingQuestion: session.pendingQuestion,
    pendingPlanReview: session.pendingPlanReview,
    isProgressStalled: session.isProgressStalled,
    lastBusinessProgressAt: session.lastBusinessProgressAt,
  };
}
