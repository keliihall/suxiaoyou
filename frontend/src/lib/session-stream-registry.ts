"use client";

import type { QueryClient, InfiniteData } from "@tanstack/react-query";
import { toast } from "sonner";
import i18n from "@/i18n/config";
import { SSEClient, type SSEConnectionStatus, type SSEEventHandler } from "@/lib/sse";
import { API, IS_DESKTOP, getBackendToken, getBackendUrl, queryKeys } from "@/lib/constants";
import {
  goalSSEWatermarkForClear,
  goalSSEWatermarkForSnapshot,
  isGoalSnapshotBlockedByWatermark,
  reconcileGoalClearedEvent,
  reconcileGoalSnapshotEvent,
  shouldRefetchGoalForPartialEvent,
  type GoalSSEWatermark,
} from "@/lib/goal-sse";
import { isRemoteMode } from "@/lib/remote-connection";
import { desktopAPI } from "@/lib/tauri-api";
import { api } from "@/lib/api";
import { SSE_EVENTS, type SSEEventData } from "@/types/streaming";
import { notifyBackgroundFinish } from "@/lib/background-notify";
import {
  hasProgressStalled,
  isBusinessProgressEvent,
  isWaitingForUserInteraction,
} from "@/lib/stream-progress";
import { StreamLeaseRegistry, type StreamLease } from "@/lib/stream-lifecycle";
import {
  canMarkInteractionContinuing,
  INTERACTION_CONTINUATION_GRACE_MS,
  INTERACTION_RECOVERY_VERIFY_MS,
  isInteractionContinuationEvent,
  isInteractionPendingContinuation,
  matchesInteractionRecoveryTarget,
  type InteractionPromptType,
  type InteractionRecoveryTarget,
} from "@/lib/interaction-response";
import { useChatStore } from "@/stores/chat-store";
import { useConnectionStore } from "@/stores/connection-store";
import { useArtifactStore } from "@/stores/artifact-store";
import { useWorkspaceStore, type WorkspaceTodo, type WorkspaceFile, type WorkspaceTaskBatch } from "@/stores/workspace-store";
import { useSettingsStore } from "@/stores/settings-store";
import type { SessionResponse } from "@/types/session";
import type { ArtifactType } from "@/types/artifact";
import type { PaginatedMessages } from "@/types/message";
import type { SessionGoal } from "@/types/goal";

/**
 * Module-level registry of live SSE streams, keyed by sessionId.
 *
 * Replaces the per-component useSSE hook. Streams continue running across
 * route changes — closing only on terminal events (DONE / abort / agent
 * error / explicit stop). This is what lets a user start a chat in session
 * A, navigate to session B, start another generation, and have both stream
 * concurrently into their own per-session bucket.
 *
 * Each StreamInstance owns its own SSEClient, buffers, last-event-id,
 * debounce/idle timers, etc. Cross-stream concerns (visibility, backend
 * restart) are handled by a single global listener pair that dispatches to
 * every live instance.
 */

const PROGRESSIVE_BUFFER_INTERVAL_MS = 60;
const DISCONNECTED_RECOVERY_DELAYS_MS = [1_000, 3_000, 10_000] as const;

// After a desktop backend restart, wait a beat before reconciling: the
// companion onBackendRestart handler in constants.ts (registered at module
// load, fires in the same event dispatch) must reset the URL/token caches
// first, and the freshly-spawned backend needs a moment to bind its port.
const RESTART_RECONCILE_DELAY_MS = 250;

class ProgressiveBuffer {
  private pending = "";
  private timerId: ReturnType<typeof setTimeout> | null = null;

  constructor(private appendFn: (text: string) => void) {}

  push(text: string) {
    this.pending += text;
    if (!this.timerId) {
      this.timerId = setTimeout(this.flushPending, PROGRESSIVE_BUFFER_INTERVAL_MS);
    }
  }

  flush() {
    if (this.timerId) {
      clearTimeout(this.timerId);
      this.timerId = null;
    }
    if (this.pending) {
      this.appendFn(this.pending);
      this.pending = "";
    }
  }

  dispose() {
    if (this.timerId) {
      clearTimeout(this.timerId);
      this.timerId = null;
    }
    this.pending = "";
  }

  private flushPending = () => {
    if (!this.pending) {
      this.timerId = null;
      return;
    }
    const chunk = this.pending;
    this.pending = "";
    this.timerId = null;
    this.appendFn(chunk);
  };
}

interface StreamInstance {
  sessionId: string;
  streamId: string;
  lease: StreamLease;
  client: SSEClient;
  textBuffer: ProgressiveBuffer;
  reasoningBuffer: ProgressiveBuffer;
  stepFinishTimer: ReturnType<typeof setTimeout> | null;
  idleCheckTimer: ReturnType<typeof setInterval> | null;
  mobilePauseTimer: ReturnType<typeof setTimeout> | null;
  lastEventTimestamp: number;
  lastProgressTimestamp: number;
  disconnectRecoveryAttempt: number;
  disconnectRecoveryEpoch: number;
  disconnectRecoveryInFlight: boolean;
  disconnectRecoveryTimer: ReturnType<typeof setTimeout> | null;
  disconnectNotified: boolean;
  connectionStatus: SSEConnectionStatus;
  interactionRecoveryTimer: ReturnType<typeof setTimeout> | null;
  interactionRecoveryWatch: InteractionRecoveryWatch | null;
  interactionRecoverySequence: number;
  retryInteractionRecovery: (
    promptType: InteractionPromptType,
    callId: string,
  ) => boolean;
}

interface InteractionRecoveryWatch {
  target: InteractionRecoveryTarget;
  sequence: number;
}

const instances = new Map<string, StreamInstance>();
const streamLeases = new StreamLeaseRegistry();

let queryClientRef: QueryClient | null = null;
let globalListenersInstalled = false;
let unlistenBackendRestarting: (() => void) | null = null;
let unlistenBackendRestarted: (() => void) | null = null;
let unlistenVisibilityChange: (() => void) | null = null;

/**
 * Inject the React Query client. Must be called once before any start().
 * Wired in providers.tsx at app boot.
 */
export function setStreamRegistryQueryClient(qc: QueryClient): void {
  queryClientRef = qc;
}

/** Is there an active stream for this session? */
export function isStreamActive(sessionId: string): boolean {
  return instances.has(sessionId);
}

/** Get the streamId currently attached to this session, if any. */
export function getActiveStreamId(sessionId: string): string | null {
  return instances.get(sessionId)?.streamId ?? null;
}

/** Lease generation changes even if a future backend reuses a stream id. */
export function getActiveStreamGeneration(sessionId: string): number | null {
  return instances.get(sessionId)?.lease.generation ?? null;
}

/** User-requested reconnect; business progress time intentionally stays put. */
export function reconnectStream(sessionId: string): boolean {
  const instance = instances.get(sessionId);
  if (!instance) return false;
  // This is connection activity only. Deliberately do not touch
  // lastProgressTimestamp / lastBusinessProgressAt.
  instance.lastEventTimestamp = Date.now();
  resetDisconnectedRecovery(instance);
  return instance.client.reconnectNow();
}

/** Retry authoritative recovery for a resolved interaction that did not continue. */
export function recoverInteractionState(
  sessionId: string,
  streamId: string,
  promptType: InteractionPromptType,
  callId: string,
): boolean {
  const instance = instances.get(sessionId);
  if (!instance || instance.streamId !== streamId) return false;
  return instance.retryInteractionRecovery(promptType, callId);
}

/** Stop a session's stream (used by the abort flow). Idempotent. */
export function stopStream(sessionId: string): void {
  const instance = instances.get(sessionId);
  if (instance) {
    disposeInstance(instance);
    instances.delete(sessionId);
  }
  // Invalidate async setup even when no client has attached yet. Clear after
  // disposal so a live instance can flush its final buffered deltas first.
  streamLeases.clear(sessionId);
  if (instances.size === 0) {
    useConnectionStore.getState().setStatus("idle");
  }
}

function disposeInstance(instance: StreamInstance): void {
  instance.disconnectRecoveryEpoch += 1;
  instance.interactionRecoverySequence += 1;
  instance.interactionRecoveryWatch = null;
  if (instance.interactionRecoveryTimer) {
    clearTimeout(instance.interactionRecoveryTimer);
    instance.interactionRecoveryTimer = null;
  }
  if (instance.disconnectRecoveryTimer) {
    clearTimeout(instance.disconnectRecoveryTimer);
    instance.disconnectRecoveryTimer = null;
  }
  if (instance.idleCheckTimer) {
    clearInterval(instance.idleCheckTimer);
    instance.idleCheckTimer = null;
  }
  if (instance.mobilePauseTimer) {
    clearTimeout(instance.mobilePauseTimer);
    instance.mobilePauseTimer = null;
  }
  if (instance.stepFinishTimer) {
    clearTimeout(instance.stepFinishTimer);
    instance.stepFinishTimer = null;
  }
  // Flush any buffered text into the store so navigation doesn't lose it.
  const bucket = useChatStore.getState().sessions[instance.sessionId];
  if (bucket?.isGenerating && bucket.streamId === instance.streamId) {
    instance.textBuffer.flush();
    instance.reasoningBuffer.flush();
  }
  instance.textBuffer.dispose();
  instance.reasoningBuffer.dispose();
  instance.client.close();
}

function resetDisconnectedRecovery(instance: StreamInstance): void {
  instance.disconnectRecoveryEpoch += 1;
  instance.disconnectRecoveryAttempt = 0;
  instance.disconnectRecoveryInFlight = false;
  instance.disconnectNotified = false;
  if (instance.disconnectRecoveryTimer) {
    clearTimeout(instance.disconnectRecoveryTimer);
    instance.disconnectRecoveryTimer = null;
  }
}

/**
 * Start streaming events for (sessionId, streamId). Idempotent: starting the
 * same (sessionId, streamId) pair twice is a no-op; starting a new streamId
 * for an already-active session closes the old stream first.
 */
export async function startStream(sessionId: string, streamId: string): Promise<void> {
  const existing = instances.get(sessionId);
  if (
    existing?.streamId === streamId
    && streamLeases.isCurrent(existing.lease)
  ) {
    return;
  }

  // The latest request wins, including while an older request is parked on
  // desktop URL/token discovery. Its unique lease gates setup and every later
  // event continuation, so an out-of-order old start can never attach.
  const lease = streamLeases.expect(sessionId, streamId);
  if (existing) {
    disposeInstance(existing);
    if (instances.get(sessionId) === existing) instances.delete(sessionId);
  }

  if (IS_DESKTOP) {
    try {
      await Promise.all([getBackendUrl(), getBackendToken()]);
    } catch (err) {
      streamLeases.clear(sessionId, lease);
      throw err;
    }
  }
  if (!streamLeases.isCurrent(lease)) return;

  ensureGlobalListeners();

  const store = useChatStore;
  const connectionStore = useConnectionStore;

  const isCurrentStream = () =>
    streamLeases.isCurrent(lease)
    && instances.get(sessionId) === instance;

  const isCurrentGeneration = () =>
    isCurrentStream()
    && store.getState().sessions[sessionId]?.streamId === streamId;

  // Chat state is session-keyed and may keep updating in the background.
  // Workspace/artifact stores are a single visible projection, so only the
  // currently focused session may write to them.
  const isFocusedSession = () =>
    store.getState().focusedSessionId === sessionId;

  const textBuffer = new ProgressiveBuffer((text) => {
    if (isCurrentGeneration()) store.getState().appendTextDelta(sessionId, text);
  });
  const reasoningBuffer = new ProgressiveBuffer((text) => {
    if (isCurrentGeneration()) store.getState().appendReasoningDelta(sessionId, text);
  });

  const finishCurrentGeneration = () => {
    if (!isCurrentGeneration()) return false;
    store.getState().finishGeneration(sessionId);
    return true;
  };

  const stopCurrentStream = () => {
    if (!isCurrentStream()) return;
    disposeInstance(instance);
    instances.delete(sessionId);
    streamLeases.clear(sessionId, lease);
    if (instances.size === 0) connectionStore.getState().setStatus("idle");
  };

  const waitForNextPaint = () =>
    new Promise<void>((r) =>
      requestAnimationFrame(() => requestAnimationFrame(() => r())),
    );

  const canFinalizeFromCache = (sid: string) => {
    const qc = queryClientRef;
    if (!qc) return false;
    const data = qc.getQueryData<InfiniteData<PaginatedMessages>>(
      queryKeys.messages.list(sid),
    );
    const latestMessage = data?.pages.at(-1)?.messages.at(-1);
    if (!latestMessage || latestMessage.data.role !== "assistant") return false;
    return latestMessage.parts.some((part) => {
      if (part.data.type !== "step-finish") return false;
      return part.data.reason !== "tool_use";
    });
  };

  const canFinalizeFromPayload = (messages: PaginatedMessages | null | undefined) => {
    const latestMessage = messages?.messages.at(-1);
    if (!latestMessage || latestMessage.data.role !== "assistant") return false;
    return latestMessage.parts.some((part) => {
      if (part.data.type !== "step-finish") return false;
      return part.data.reason !== "tool_use";
    });
  };

  const finishFromDatabase = async (sid: string) => {
    if (!isCurrentGeneration()) return false;
    textBuffer.flush();
    reasoningBuffer.flush();
    const qc = queryClientRef;
    if (qc) {
      await qc.invalidateQueries({ queryKey: queryKeys.messages.list(sid) });
      if (!isCurrentGeneration()) return false;
      await waitForNextPaint();
      if (!isCurrentGeneration()) return false;
    }

    // Do not finalize while the backend still reports this session as active.
    try {
      const activeJobs = await api.get<Array<{ stream_id: string; session_id: string }>>(
        API.CHAT.ACTIVE,
      );
      if (!isCurrentGeneration()) return false;
      const ourStreamId = store.getState().sessions[sid]?.streamId;
      const stillActive = activeJobs.some(
        (job) =>
          job.session_id === sid &&
          (!ourStreamId || job.stream_id === ourStreamId),
      );
      if (stillActive) return false;
    } catch {
      // Without authoritative active-job state, an older terminal message in
      // the session is not enough evidence that this generation finished.
      return false;
    }

    if (!canFinalizeFromCache(sid)) {
      try {
        const latestPage = await api.get<PaginatedMessages>(API.MESSAGES.LIST(sid, 50, -1));
        if (!isCurrentGeneration()) return false;
        if (qc) {
          qc.setQueryData<InfiniteData<PaginatedMessages>>(
            queryKeys.messages.list(sid),
            (old) => {
              if (!old) return { pages: [latestPage], pageParams: [-1] };
              return { ...old, pages: [...old.pages.slice(0, -1), latestPage] };
            },
          );
        }
        if (!canFinalizeFromPayload(latestPage)) return false;
      } catch {
        return false;
      }
    }

    // Network and DB reconciliation above contain await points. A user may
    // have started another generation in this session meanwhile; an old DONE
    // handler must never clear or close that newer stream.
    if (!isCurrentGeneration()) return false;
    store.getState().finishGeneration(sid);
    if (instances.size === 0) connectionStore.getState().setStatus("idle");
    if (isFocusedSession()) {
      const workspace = useWorkspaceStore.getState();
      if (
        workspace.todos.length > 0 &&
        workspace.todos.every((todo) => todo.status === "completed")
      ) {
        workspace.collapseSection("progress");
      }
    }
    if (qc) qc.invalidateQueries({ queryKey: queryKeys.sessions.all });
    return true;
  };

  const recoverDisconnectedStream = async () => {
    if (!isCurrentStream()) return;
    if (
      instance.disconnectRecoveryInFlight
      || instance.disconnectRecoveryTimer
    ) {
      return;
    }

    const recoveryEpoch = ++instance.disconnectRecoveryEpoch;
    instance.disconnectRecoveryInFlight = true;
    try {
      const finished = await finishFromDatabase(sessionId);
      if (
        !isCurrentStream()
        || instance.disconnectRecoveryEpoch !== recoveryEpoch
      ) {
        return;
      }
      if (finished) {
        stopCurrentStream();
        return;
      }

      // SSEClient already exhausted its bounded internal backoff. If the
      // database cannot prove completion, keep the generation and its controls
      // alive, then run a small bounded set of fresh reconnect cycles. After
      // that the explicit reconnect button remains available to the user.
      const delay = DISCONNECTED_RECOVERY_DELAYS_MS[
        instance.disconnectRecoveryAttempt
      ];
      if (delay === undefined) return;
      instance.disconnectRecoveryAttempt += 1;
      instance.disconnectRecoveryTimer = setTimeout(() => {
        instance.disconnectRecoveryTimer = null;
        if (
          !isCurrentStream()
          || instance.disconnectRecoveryEpoch !== recoveryEpoch
        ) {
          return;
        }
        instance.client.reconnectNow();
      }, delay);
    } finally {
      if (
        isCurrentStream()
        && instance.disconnectRecoveryEpoch === recoveryEpoch
      ) {
        instance.disconnectRecoveryInFlight = false;
      }
    }
  };

  const client = new SSEClient({
    url: API.CHAT.STREAM(streamId),
    urlProvider: () => API.CHAT.STREAM(streamId),
    initialLastEventId: 0,
    onEvent: (eventType) => {
      if (!isCurrentStream()) return;
      const now = Date.now();
      instance.lastEventTimestamp = now;
      if (isBusinessProgressEvent(eventType)) {
        instance.lastProgressTimestamp = now;
        store.getState().markBusinessProgress(sessionId, now);
      }
    },
    onStatusChange: (status) => {
      if (!isCurrentStream()) return;
      instance.connectionStatus = status;
      connectionStore.getState().setStatus(status);
      if (status === "connected") {
        resetDisconnectedRecovery(instance);
        return;
      }
      if (status === "disconnected") {
        if (!instance.disconnectNotified) {
          instance.disconnectNotified = true;
          toast.error("Connection lost. Reconnecting while the task continues.");
        }
        void recoverDisconnectedStream();
      }
    },
  });

  const instance: StreamInstance = {
    sessionId,
    streamId,
    lease,
    client,
    textBuffer,
    reasoningBuffer,
    stepFinishTimer: null,
    idleCheckTimer: null,
    mobilePauseTimer: null,
    lastEventTimestamp: Date.now(),
    lastProgressTimestamp:
      store.getState().sessions[sessionId]?.lastBusinessProgressAt
      ?? Date.now(),
    disconnectRecoveryAttempt: 0,
    disconnectRecoveryEpoch: 0,
    disconnectRecoveryInFlight: false,
    disconnectRecoveryTimer: null,
    disconnectNotified: false,
    connectionStatus: "connecting",
    interactionRecoveryTimer: null,
    interactionRecoveryWatch: null,
    interactionRecoverySequence: 0,
    retryInteractionRecovery: () => false,
  };

  const cancelPendingStepFinish = () => {
    if (instance.stepFinishTimer) {
      clearTimeout(instance.stepFinishTimer);
      instance.stepFinishTimer = null;
    }
  };

  const getPendingInteraction = (promptType: InteractionPromptType) => {
    const bucket = store.getState().sessions[sessionId];
    if (promptType === "permission") return bucket?.pendingPermission ?? null;
    if (promptType === "plan") return bucket?.pendingPlanReview ?? null;
    return bucket?.pendingQuestion ?? null;
  };

  const clearInteractionRecovery = (watch?: InteractionRecoveryWatch) => {
    if (watch && instance.interactionRecoveryWatch !== watch) return;
    instance.interactionRecoverySequence += 1;
    instance.interactionRecoveryWatch = null;
    if (instance.interactionRecoveryTimer) {
      clearTimeout(instance.interactionRecoveryTimer);
      instance.interactionRecoveryTimer = null;
    }
  };

  const isCurrentInteractionWatch = (watch: InteractionRecoveryWatch) => {
    if (
      !isCurrentGeneration()
      || instance.interactionRecoveryWatch !== watch
      || instance.interactionRecoverySequence !== watch.sequence
    ) {
      return false;
    }
    const pending = getPendingInteraction(watch.target.promptType);
    if (
      !pending
      || !isInteractionPendingContinuation(pending.responseState)
    ) {
      return false;
    }
    return matchesInteractionRecoveryTarget(watch.target, {
      sessionId,
      streamId,
      callId: pending.callId,
      promptType: watch.target.promptType,
      streamGeneration: lease.generation,
    });
  };

  const runInteractionRecovery = async (
    watch: InteractionRecoveryWatch,
    finalAttempt: boolean,
  ) => {
    if (!isCurrentInteractionWatch(watch)) return;
    store.getState().setInteractionResponseState(
      sessionId,
      watch.target.promptType,
      watch.target.callId,
      "recovering",
    );

    const qc = queryClientRef;
    if (qc) {
      await Promise.allSettled([
        qc.invalidateQueries({ queryKey: queryKeys.messages.list(sessionId) }),
        qc.invalidateQueries({ queryKey: queryKeys.sessionInputs(sessionId) }),
        qc.invalidateQueries({ queryKey: queryKeys.sessions.detail(sessionId) }),
      ]);
      if (!isCurrentInteractionWatch(watch)) return;
    }

    let activeJobs: Array<{
      stream_id: string;
      session_id: string;
      needs_input?: boolean;
    }> | null = null;
    try {
      activeJobs = await api.get(API.CHAT.ACTIVE);
    } catch {
      // Unknown state is not completion. Reconnect below, then expose a
      // recoverable state if the bounded verification still sees no progress.
    }
    if (!isCurrentInteractionWatch(watch)) return;

    const jobStillActive = activeJobs?.some(
      (job) =>
        job.session_id === sessionId
        && job.stream_id === streamId,
    );
    if (activeJobs !== null && !jobStillActive) {
      const finished = await finishFromDatabase(sessionId);
      if (
        !isCurrentStream()
        || instance.interactionRecoveryWatch !== watch
        || instance.interactionRecoverySequence !== watch.sequence
      ) {
        return;
      }
      if (finished) {
        clearInteractionRecovery(watch);
        stopCurrentStream();
        return;
      }
      if (!isCurrentInteractionWatch(watch)) return;
      store.getState().setInteractionResponseState(
        sessionId,
        watch.target.promptType,
        watch.target.callId,
        "recovery_needed",
      );
      return;
    }

    // Replay from Last-Event-ID only when the job is still active or status is
    // unknown. A confirmed-absent job is reconciled from the database above.
    // This recovers missed events without resubmitting the user's choice.
    instance.client.reconnectNow();

    if (finalAttempt) {
      store.getState().setInteractionResponseState(
        sessionId,
        watch.target.promptType,
        watch.target.callId,
        "recovery_needed",
      );
      instance.interactionRecoveryTimer = null;
      return;
    }

    if (instance.interactionRecoveryTimer) {
      clearTimeout(instance.interactionRecoveryTimer);
    }
    instance.interactionRecoveryTimer = setTimeout(() => {
      instance.interactionRecoveryTimer = null;
      if (!isCurrentInteractionWatch(watch)) return;
      void runInteractionRecovery(watch, true);
    }, INTERACTION_RECOVERY_VERIFY_MS);
  };

  const beginInteractionRecovery = (
    promptType: InteractionPromptType,
    callId: string,
    explicitRetry = false,
  ) => {
    if (!isCurrentGeneration()) return false;
    const pending = getPendingInteraction(promptType);
    if (
      !pending
      || pending.callId !== callId
    ) {
      return false;
    }
    if (explicitRetry) {
      if (pending.responseState !== "recovery_needed") return false;
      store.getState().beginInteractionRecoveryRetry(
        sessionId,
        promptType,
        callId,
      );
      if (getPendingInteraction(promptType)?.responseState !== "recovering") {
        return false;
      }
    } else if (pending.responseState !== "resolved") {
      // The automatic watchdog is single-shot. Recovering and actionable
      // states require either continuation or an explicit user retry.
      return false;
    }
    clearInteractionRecovery();
    const sequence = ++instance.interactionRecoverySequence;
    const watch: InteractionRecoveryWatch = {
      sequence,
      target: {
        sessionId,
        streamId,
        callId,
        promptType,
        streamGeneration: lease.generation,
      },
    };
    instance.interactionRecoveryWatch = watch;
    void runInteractionRecovery(watch, false);
    return true;
  };

  instance.retryInteractionRecovery = (promptType, callId) =>
    beginInteractionRecovery(promptType, callId, true);

  const markInteractionContinuing = (
    promptType: InteractionPromptType,
    callId: string,
  ) => {
    const watch = instance.interactionRecoveryWatch;
    if (
      watch?.target.promptType === promptType
      && watch.target.callId === callId
    ) {
      clearInteractionRecovery(watch);
    }
    store.getState().setInteractionResponseState(
      sessionId,
      promptType,
      callId,
      "continuing",
    );
    // Keep the acknowledgement visible briefly instead of making the card
    // disappear in the same render as the first continuation event.
    setTimeout(() => {
      if (!isCurrentStream()) return;
      const bucket = store.getState().sessions[sessionId];
      if (promptType === "permission") {
        if (
          bucket?.pendingPermission?.callId === callId
          && bucket.pendingPermission.responseState === "continuing"
        ) {
          store.getState().clearPermissionRequest(sessionId);
        }
        return;
      }
      if (promptType === "plan") {
        if (
          bucket?.pendingPlanReview?.callId === callId
          && bucket.pendingPlanReview.responseState === "continuing"
        ) {
          store.getState().clearPlanReview(sessionId);
        }
        return;
      }
      if (
        bucket?.pendingQuestion?.callId === callId
        && bucket.pendingQuestion.responseState === "continuing"
      ) {
        store.getState().clearQuestion(sessionId);
      }
    }, 1200);
  };

  const markToolContinuation = (toolCallId: string) => {
    const bucket = store.getState().sessions[sessionId];
    const permission = bucket?.pendingPermission;
    if (
      permission?.toolCallId === toolCallId
      && canMarkInteractionContinuing(permission.responseState)
    ) {
      markInteractionContinuing("permission", permission.callId);
    }
    const question = bucket?.pendingQuestion;
    if (
      question?.callId === toolCallId
      && canMarkInteractionContinuing(question.responseState)
    ) {
      markInteractionContinuing("question", question.callId);
    }
    const plan = bucket?.pendingPlanReview;
    if (
      plan?.callId === toolCallId
      && canMarkInteractionContinuing(plan.responseState)
    ) {
      markInteractionContinuing("plan", plan.callId);
    }
  };

  const markPendingInteractionsContinuing = () => {
    const bucket = store.getState().sessions[sessionId];
    const permission = bucket?.pendingPermission;
    const question = bucket?.pendingQuestion;
    const plan = bucket?.pendingPlanReview;
    if (permission && canMarkInteractionContinuing(permission.responseState)) {
      markInteractionContinuing("permission", permission.callId);
    }
    if (question && canMarkInteractionContinuing(question.responseState)) {
      markInteractionContinuing("question", question.callId);
    }
    if (plan && canMarkInteractionContinuing(plan.responseState)) {
      markInteractionContinuing("plan", plan.callId);
    }
  };

  const onCurrent = (eventType: string, handler: SSEEventHandler) =>
    client.on(eventType, (data, id) => {
      if (!isCurrentStream()) return;
      if (isInteractionContinuationEvent(eventType)) {
        markPendingInteractionsContinuing();
      }
      handler(data, id);
    });

  const goalKey = queryKeys.sessions.goal(sessionId);
  const goalWatermarkKey = [...goalKey, "sse-watermark"] as const;
  const invalidateGoalCache = () => {
    const qc = queryClientRef;
    if (qc) void qc.invalidateQueries({ queryKey: goalKey });
  };
  const syncGoalSnapshot = (data: SSEEventData) => {
    const qc = queryClientRef;
    if (!qc) return;
    const current = qc.getQueryData<SessionGoal | null>(goalKey);
    const decision = reconcileGoalSnapshotEvent(current, data, sessionId);
    if (decision.kind === "apply") {
      const watermark = qc.getQueryData<GoalSSEWatermark>(goalWatermarkKey);
      if (isGoalSnapshotBlockedByWatermark(decision.goal, watermark)) return;
      qc.setQueryData<SessionGoal | null>(goalKey, decision.goal);
      qc.setQueryData(
        goalWatermarkKey,
        goalSSEWatermarkForSnapshot(decision.goal),
      );
    } else if (decision.kind === "refetch") {
      invalidateGoalCache();
    }
  };
  const clearGoalSnapshot = (data: SSEEventData) => {
    const qc = queryClientRef;
    if (!qc) return;
    const current = qc.getQueryData<SessionGoal | null>(goalKey);
    const watermark = qc.getQueryData<GoalSSEWatermark>(goalWatermarkKey);
    if (
      !current
      && data.goal_id
      && watermark
      && (
        data.goal_id !== watermark.goalId
        || (
          typeof data.revision === "number"
          && data.revision < watermark.revision
        )
      )
    ) {
      return;
    }
    const decision = reconcileGoalClearedEvent(current, data, sessionId);
    if (decision.kind === "clear") {
      qc.setQueryData<SessionGoal | null>(goalKey, null);
      const nextWatermark = goalSSEWatermarkForClear(current, data, watermark);
      if (nextWatermark) qc.setQueryData(goalWatermarkKey, nextWatermark);
    }
  };
  const refreshGoalFromPartialEvent = (data: SSEEventData) => {
    if (shouldRefetchGoalForPartialEvent(data, sessionId)) {
      invalidateGoalCache();
    }
  };
  const refreshGoalAfterInteractionResolution = () => {
    const qc = queryClientRef;
    if (!qc?.getQueryData<SessionGoal | null>(goalKey)) return;
    invalidateGoalCache();
    // The acknowledgement can arrive just before the prompt coroutine commits
    // waiting_user -> running. Recheck once across that short DB boundary.
    setTimeout(() => {
      if (isCurrentStream()) invalidateGoalCache();
    }, 250);
  };

  // ─── Event handlers ───

  onCurrent(SSE_EVENTS.GOAL_UPDATED, syncGoalSnapshot);
  onCurrent(SSE_EVENTS.GOAL_RUN_STARTED, syncGoalSnapshot);
  onCurrent(SSE_EVENTS.GOAL_RUN_FINISHED, syncGoalSnapshot);
  onCurrent(SSE_EVENTS.GOAL_BUDGET_WARNING, refreshGoalFromPartialEvent);
  onCurrent(SSE_EVENTS.GOAL_NEEDS_USER, refreshGoalFromPartialEvent);
  onCurrent(SSE_EVENTS.GOAL_CLEARED, clearGoalSnapshot);

  onCurrent(SSE_EVENTS.MODEL_LOADING, () => {
    store.getState().setModelLoading(sessionId, true);
  });

  onCurrent(SSE_EVENTS.TEXT_DELTA, (data) => {
    cancelPendingStepFinish();
    const bucket = store.getState().sessions[sessionId];
    if (bucket?.isModelLoading) store.getState().setModelLoading(sessionId, false);
    if (data.text) textBuffer.push(data.text);
  });

  onCurrent(SSE_EVENTS.REASONING_DELTA, (data) => {
    cancelPendingStepFinish();
    if (data.text) reasoningBuffer.push(data.text);
  });

  onCurrent(SSE_EVENTS.TOOL_START, (data) => {
    cancelPendingStepFinish();
    if (data.tool && data.call_id) {
      store.getState().addToolStart(
        sessionId,
        data.tool,
        data.call_id,
        data.arguments ?? {},
        data.title,
      );
      markToolContinuation(data.call_id);

      if (isFocusedSession() && data.tool === "artifact" && data.arguments) {
        const args = data.arguments as Record<string, string>;
        const command = args.command || "create";
        if (command === "create" && args.type && args.title && args.content) {
          useArtifactStore.getState().openArtifact({
            id: data.call_id,
            type: args.type as ArtifactType,
            title: args.title,
            content: args.content,
            language: args.language,
            identifier: args.identifier,
          });
        }
      }
    }
  });

  onCurrent(SSE_EVENTS.TOOL_RESULT, (data) => {
    cancelPendingStepFinish();
    if (!data.call_id) return;
    store.getState().setToolResult(
      sessionId,
      data.call_id,
      data.output ?? "",
      data.title,
      data.metadata,
    );
    markToolContinuation(data.call_id);

    if (isFocusedSession() && data.tool === "todo" && data.metadata) {
      const meta = data.metadata as { todos?: Array<{ content: string; status: string; activeForm?: string }> };
      if (meta.todos) {
        useWorkspaceStore.getState().setTodos(meta.todos as WorkspaceTodo[]);
        const ws = useWorkspaceStore.getState();
        if (!ws.isOpen) ws.open();
        ws.expandSection("progress");
      }
    }

    const resultMetadata = (data.metadata ?? {}) as Record<string, unknown>;
    const deliveredFiles =
      Array.isArray(resultMetadata.artifact_files) ||
      Array.isArray(resultMetadata.written_files) ||
      resultMetadata.artifact_delivery === true;
    if (
      isFocusedSession() &&
      isCurrentGeneration() &&
      data.tool &&
      (
        deliveredFiles ||
        ["write", "edit", "image_generate", "office", "code_execute", "bash", "artifact"].includes(data.tool)
      )
    ) {
      api.get<{ files: Array<{ name: string; path: string; type: string }> }>(
        API.SESSIONS.FILES(sessionId),
      ).then((res) => {
        if (!isFocusedSession() || !isCurrentGeneration()) return;
        if (res.files) {
          useWorkspaceStore.getState().setWorkspaceFiles(
            res.files.map((f) => ({ name: f.name, path: f.path, type: f.type as WorkspaceFile["type"] })),
          );
        }
      }).catch((e) => console.warn("[stream-registry] Failed to refresh workspace files:", e));
    }

    if (isFocusedSession() && data.tool === "artifact" && data.metadata) {
      const meta = data.metadata as Record<string, string>;
      if (
        (meta.command === "update" || meta.command === "rewrite") &&
        meta.content &&
        meta.identifier
      ) {
        useArtifactStore.getState().openArtifact({
          id: data.call_id,
          type: (meta.type || "code") as ArtifactType,
          title: meta.title || "Untitled",
          content: meta.content,
          language: meta.language,
          identifier: meta.identifier,
        });
      }
    }
  });

  onCurrent(SSE_EVENTS.TOOL_METADATA, (data) => {
    if (!data.call_id) return;
    store.getState().setToolMetadata(
      sessionId,
      data.call_id,
      data.title,
      data.metadata,
    );
    markToolContinuation(data.call_id);
  });

  onCurrent(SSE_EVENTS.TOOL_ERROR, (data) => {
    cancelPendingStepFinish();
    if (data.call_id) {
      store.getState().setToolError(sessionId, data.call_id, data.output ?? data.error_message ?? "Error");
      markToolContinuation(data.call_id);
    }
  });

  onCurrent(SSE_EVENTS.STEP_START, (data) => {
    cancelPendingStepFinish();
    store.getState().addStepStart(sessionId, data.step ?? 0);
  });

  onCurrent(SSE_EVENTS.STEP_FINISH, (data, id) => {
    store.getState().addStepFinish(
      sessionId,
      data.reason ?? "stop",
      data.tokens ?? {},
      data.cost ?? 0,
      data.total_cost ?? null,
      id ?? null,
    );

    const terminalReasons = new Set(["stop", "length", "error", "aborted"]);
    const isTerminalStep = terminalReasons.has(data.reason ?? "");
    if (!isTerminalStep) {
      cancelPendingStepFinish();
      return;
    }
    cancelPendingStepFinish();
    instance.stepFinishTimer = setTimeout(async () => {
      instance.stepFinishTimer = null;
      if (!isCurrentGeneration()) return;

      const finished = await finishFromDatabase(sessionId);
      if (!isCurrentStream()) return;
      if (finished) {
        stopCurrentStream();
      }
    }, 1_200);
  });

  const updateTaskBatch = (data: { batch_id?: string | null; mode?: string | null; tasks?: unknown[] | null }) => {
    if (!data.batch_id || !data.mode || !Array.isArray(data.tasks)) return;
    if (!isFocusedSession()) return;
    const ws = useWorkspaceStore.getState();
    ws.setTaskBatch({
      batch_id: data.batch_id,
      mode: data.mode === "sequential" ? "sequential" : "parallel",
      tasks: data.tasks as WorkspaceTaskBatch["tasks"],
    });
    if (!ws.isOpen) ws.open();
    ws.expandSection("progress");
  };

  onCurrent(SSE_EVENTS.TASK_BATCH_START, (data) => {
    cancelPendingStepFinish();
    updateTaskBatch(data);
  });
  onCurrent(SSE_EVENTS.TASK_BATCH_UPDATE, (data) => {
    cancelPendingStepFinish();
    updateTaskBatch(data);
  });
  onCurrent(SSE_EVENTS.TASK_BATCH_FINISH, (data) => {
    updateTaskBatch(data);
  });

  onCurrent(SSE_EVENTS.COMPACTION_START, (data) => {
    store.getState().startCompaction(sessionId, data.phases ?? ["prune", "summarize"]);
  });
  onCurrent(SSE_EVENTS.COMPACTION_PHASE, (data) => {
    if (data.phase && data.status) {
      store.getState().updateCompactionPhase(sessionId, data.phase, data.status);
    }
  });
  onCurrent(SSE_EVENTS.COMPACTION_PROGRESS, (data) => {
    if (data.phase && data.chars != null) {
      store.getState().updateCompactionProgress(sessionId, data.phase, data.chars);
    }
  });
  onCurrent(SSE_EVENTS.COMPACTED, (data) => {
    store.getState().addCompaction(sessionId, true);
    if (data.summary_created) toast.success("Context compacted");
  });

  onCurrent(SSE_EVENTS.PERMISSION_REQUEST, (data) => {
    if (!data.call_id) return;
    clearInteractionRecovery();
    const workMode = useSettingsStore.getState().workMode;
    const requiresPerCallApproval = data.metadata?.approval_mode === "per_call";
    if (workMode === "auto" && !requiresPerCallApproval) {
      api.post(API.CHAT.RESPOND, {
        stream_id: streamId,
        call_id: data.call_id,
        response: true,
      }).catch((e) => console.warn("[stream-registry] Failed to auto-approve permission:", e));
      return;
    }
    store.getState().setPermissionRequest(sessionId, {
      callId: data.call_id,
      toolCallId: data.tool_call_id,
      tool: data.tool ?? data.permission ?? "",
      permission: data.permission ?? "",
      patterns: data.patterns ?? [],
      arguments: data.arguments ?? {},
      metadata: data.metadata,
      message: data.message,
      argumentsTruncated: data.arguments_truncated ?? false,
    });
  });

  onCurrent(SSE_EVENTS.QUESTION, (data) => {
    if (!data.call_id) return;
    clearInteractionRecovery();
    const nestedArguments = data.arguments && typeof data.arguments === "object"
      ? data.arguments
      : {};
    const questionArguments: Record<string, unknown> = { ...nestedArguments };
    if (questionArguments.question == null && data.question != null) {
      questionArguments.question = data.question;
    }
    if (questionArguments.options == null && data.options != null) {
      questionArguments.options = data.options;
    }
    if (questionArguments.questions == null && data.questions != null) {
      questionArguments.questions = data.questions;
    }
    store.getState().setQuestion(sessionId, {
      callId: data.call_id,
      tool: data.tool ?? "question",
      arguments: questionArguments,
    });
  });

  onCurrent(SSE_EVENTS.PERMISSION_RESOLVED, (data) => {
    const pending = store.getState().sessions[sessionId]?.pendingPermission;
    if (pending && data.call_id === pending.callId) {
      store.getState().setInteractionResponseState(
        sessionId,
        "permission",
        pending.callId,
        "resolved",
        { decision: data.decision, source: data.source },
      );
    }
    refreshGoalAfterInteractionResolution();
  });

  onCurrent(SSE_EVENTS.QUESTION_RESOLVED, (data) => {
    const pending = store.getState().sessions[sessionId]?.pendingQuestion;
    if (pending && data.call_id === pending.callId) {
      store.getState().setInteractionResponseState(
        sessionId,
        "question",
        pending.callId,
        "resolved",
        { decision: data.decision, source: data.source },
      );
    }
    refreshGoalAfterInteractionResolution();
  });

  onCurrent(SSE_EVENTS.PLAN_REVIEW_RESOLVED, (data) => {
    const pending = store.getState().sessions[sessionId]?.pendingPlanReview;
    if (pending && data.call_id === pending.callId) {
      store.getState().setInteractionResponseState(
        sessionId,
        "plan",
        pending.callId,
        "resolved",
        { decision: data.decision, source: data.source },
      );
    }
    refreshGoalAfterInteractionResolution();
  });

  onCurrent(SSE_EVENTS.PLAN_REVIEW, (data) => {
    if (!data.call_id) return;
    clearInteractionRecovery();
    const reviewData = {
      callId: data.call_id,
      title: data.title ?? "Plan",
      plan: data.plan ?? "",
      filesToModify: data.files_to_modify ?? [],
    };
    store.getState().setPlanReview(sessionId, reviewData);
    if (!isFocusedSession()) return;
    try {
      const { usePlanReviewStore } = require("@/stores/plan-review-store");
      usePlanReviewStore.getState().openReview(reviewData);
    } catch {
      // ignore — store may not be available during SSR
    }
  });

  onCurrent(SSE_EVENTS.TITLE_UPDATE, (data) => {
    if (!data.title) return;
    const qc = queryClientRef;
    if (!qc) return;
    qc.setQueryData<InfiniteData<SessionResponse[]>>(
      queryKeys.sessions.all,
      (old) => {
        if (!old) return old;
        return {
          ...old,
          pages: old.pages.map((page) =>
            page.map((s) => (s.id === sessionId ? { ...s, title: data.title! } : s)),
          ),
        };
      },
    );
    qc.setQueryData<SessionResponse>(
      queryKeys.sessions.detail(sessionId),
      (old) => (old ? { ...old, title: data.title! } : old),
    );
  });

  const refreshPendingInputs = () => {
    const qc = queryClientRef;
    if (qc) qc.invalidateQueries({ queryKey: queryKeys.sessionInputs(sessionId) });
  };
  onCurrent(SSE_EVENTS.INPUT_QUEUED, refreshPendingInputs);
  onCurrent(SSE_EVENTS.INPUT_STARTED, refreshPendingInputs);
  onCurrent(SSE_EVENTS.INPUT_APPLIED, refreshPendingInputs);
  onCurrent(SSE_EVENTS.INPUT_FAILED, (data) => {
    refreshPendingInputs();
    toast.error(
      data.error
        ? i18n.t("inputExecutionFailedWithReason", {
            ns: "chat",
            reason: data.error,
          })
        : i18n.t("inputExecutionFailed", { ns: "chat" }),
    );
  });

  onCurrent("heartbeat", () => {
    // No-op: the SSEClient resets its heartbeat timer on any event
  });

  onCurrent(SSE_EVENTS.DESYNC, () => {
    const qc = queryClientRef;
    if (qc) {
      qc.invalidateQueries({ queryKey: queryKeys.messages.list(sessionId) });
      invalidateGoalCache();
    }
  });

  onCurrent(SSE_EVENTS.COMPACTION_ERROR, (data) => {
    toast.warning(data.error_message || "Context compression failed. Consider starting a new chat.");
  });

  onCurrent(SSE_EVENTS.DONE, () => {
    // Close synchronously: the stream ends right after DONE, so the dying
    // EventSource must not schedule a reconnect to a job
    // that is already complete (→ a spurious "Job not found").
    client.close();
    cancelPendingStepFinish();
    textBuffer.flush();
    reasoningBuffer.flush();
    // DONE is the authoritative terminal boundary.  Do not keep the product
    // in "Finalizing" while a messages refetch or the backend's active-job
    // cleanup races behind this event; DB verification is only required when
    // recovering a stream that missed DONE.
    finishCurrentGeneration();
    if (!isCurrentStream()) return;
    const qc = queryClientRef;
    if (qc) {
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: queryKeys.messages.list(sessionId) });
      }, 500);
      qc.invalidateQueries({ queryKey: queryKeys.sessions.all });
      qc.invalidateQueries({ queryKey: queryKeys.sessions.detail(sessionId) });
      qc.invalidateQueries({ queryKey: queryKeys.sessionInputs(sessionId) });
      invalidateGoalCache();
    }
    maybeNotifyFinish(sessionId, "done");
    stopCurrentStream();
  });

  const handleAgentError = (data: { error_message?: string | null; code?: string | null }) => {
    // Close the dead connection synchronously. The server ends the response
    // right after this single error event, so the
    // EventSource would otherwise fire onerror mid-await and schedule a
    // reconnect to a stream the backend no longer has.
    client.close();

    const message = data.error_message ?? "Unknown stream error";
    // A missing job almost always means the local backend restarted out from
    // under an in-flight generation. The conversation is safe in the DB, so
    // recover quietly rather than alarming the user with an opaque toast.
    const streamGone = data.code === "JOB_NOT_FOUND" || message === "Job not found";
    const contextLimitError = /maximum context length|requested about/i.test(message);
    if (streamGone) {
      // Silent — recovered from the DB below.
    } else if (contextLimitError) {
      toast.error("Context too long for this model. Start a new chat or shorten the conversation.");
    } else {
      toast.error(message);
    }
    console.warn("SSE agent error:", message);
    textBuffer.flush();
    reasoningBuffer.flush();
    finishCurrentGeneration();
    if (!isCurrentStream()) return;
    const qc = queryClientRef;
    if (qc) {
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: queryKeys.messages.list(sessionId) });
      }, 500);
      qc.invalidateQueries({ queryKey: queryKeys.sessions.detail(sessionId) });
      qc.invalidateQueries({ queryKey: queryKeys.sessionInputs(sessionId) });
      invalidateGoalCache();
    }
    if (!streamGone) maybeNotifyFinish(sessionId, "error", message);
    stopCurrentStream();
  };
  onCurrent(SSE_EVENTS.AGENT_ERROR, handleAgentError);
  onCurrent(SSE_EVENTS.ERROR, handleAgentError);

  // Publish ownership before connect(): SSEClient reports "connecting"
  // synchronously, and every callback must already be able to validate it.
  if (!streamLeases.isCurrent(lease)) {
    disposeInstance(instance);
    return;
  }
  instances.set(sessionId, instance);
  client.connect();

  // ─── Per-instance idle recovery ───
  const IDLE_RECOVERY_MS = 15_000;
  const IDLE_CHECK_INTERVAL_MS = 5_000;
  instance.idleCheckTimer = setInterval(async () => {
    if (!isCurrentStream()) return;
    const bucket = store.getState().sessions[sessionId];
    const isGenerating = bucket?.isGenerating ?? false;
    if (!isGenerating) {
      if (instance.idleCheckTimer) {
        clearInterval(instance.idleCheckTimer);
        instance.idleCheckTimer = null;
      }
      return;
    }
    const now = Date.now();
    const resolvedInteractions: Array<[
      InteractionPromptType,
      { callId: string; responseState?: string; responseResolvedAt?: number | null } | null | undefined,
    ]> = [
      ["permission", bucket?.pendingPermission],
      ["question", bucket?.pendingQuestion],
      ["plan", bucket?.pendingPlanReview],
    ];
    for (const [promptType, pending] of resolvedInteractions) {
      if (
        pending?.responseState === "resolved"
        && pending.responseResolvedAt != null
        && now - pending.responseResolvedAt >= INTERACTION_CONTINUATION_GRACE_MS
      ) {
        beginInteractionRecovery(promptType, pending.callId);
      }
    }
    const waitingForUser = isWaitingForUserInteraction(
      bucket?.pendingPermission,
      bucket?.pendingQuestion,
      bucket?.pendingPlanReview,
    );
    if (waitingForUser) {
      store.getState().setProgressStalled(sessionId, false);
    } else if (hasProgressStalled(
      now,
      instance.lastProgressTimestamp,
      isGenerating,
      waitingForUser,
    )) {
      store.getState().setProgressStalled(sessionId, true);
    }
    if (instance.lastEventTimestamp > 0 && now - instance.lastEventTimestamp > IDLE_RECOVERY_MS) {
      console.warn(`SSE idle recovery for ${sessionId}: no events for 15s, attempting DB recovery`);
      const finished = await finishFromDatabase(sessionId);
      if (!isCurrentStream()) return;
      if (finished) {
        stopCurrentStream();
        return;
      }
      instance.lastEventTimestamp = Date.now();
      if (instance.connectionStatus !== "disconnected") {
        client.checkHealth();
      }
    }
  }, IDLE_CHECK_INTERVAL_MS);

}

// ─── Global cross-stream listeners (installed once on first start) ───

function ensureGlobalListeners(): void {
  if (globalListenersInstalled) return;
  globalListenersInstalled = true;

  // Desktop: pause SSE reconnection while backend is restarting; resume after.
  if (IS_DESKTOP) {
    unlistenBackendRestarting = desktopAPI.onBackendRestarting(() => {
      for (const inst of instances.values()) inst.client.pauseReconnect();
    });
    unlistenBackendRestarted = desktopAPI.onBackendRestart(() => {
      // Stop every client's auto-reconnect immediately so none races to a
      // stream_id the freshly-restarted backend no longer has — that race is
      // exactly what produced the spurious "Job not found" toasts. Then
      // reconcile against the new backend once its caches/port have settled.
      for (const inst of instances.values()) inst.client.pauseReconnect();
      setTimeout(() => {
        void reconcileStreamsAfterRestart();
      }, RESTART_RECONCILE_DELAY_MS);
    });
  }

  // Visibility: mobile (remote) pauses streams when hidden to save battery,
  // desktop just rechecks health. Same logic as the old useSSE handler, but
  // applied to every live instance instead of one.
  const handleVisibilityChange = () => {
    for (const inst of instances.values()) {
      if (!store_isGenerating(inst.sessionId)) continue;

      if (document.visibilityState === "visible") {
        if (inst.mobilePauseTimer) {
          clearTimeout(inst.mobilePauseTimer);
          inst.mobilePauseTimer = null;
        }
        inst.client.resumeReconnect();
        inst.client.checkHealth();
      } else if (isRemoteMode()) {
        inst.mobilePauseTimer = setTimeout(() => {
          inst.client.pauseReconnect();
          inst.mobilePauseTimer = null;
        }, 30_000);
      }
    }
  };
  document.addEventListener("visibilitychange", handleVisibilityChange);
  unlistenVisibilityChange = () => document.removeEventListener("visibilitychange", handleVisibilityChange);
}

function store_isGenerating(sessionId: string): boolean {
  return useChatStore.getState().sessions[sessionId]?.isGenerating ?? false;
}

/**
 * After a desktop backend restart the in-memory StreamManager is empty: every
 * pre-restart stream_id is gone. Blindly reconnecting those dead ids is what
 * surfaced "Job not found" to users. Instead, ask the new backend which
 * generations are actually still running and reconcile each live stream:
 *  - resume it if it survived (a health blip that didn't kill the process),
 *  - re-attach if the backend now reports a different job for that session,
 *  - otherwise finalize it from the DB (the generation died with the old
 *    process; the conversation itself is safe).
 */
async function reconcileStreamsAfterRestart(): Promise<void> {
  if (instances.size === 0) return;
  // Reconcile only streams that existed when this recovery began. A new
  // generation created while /active is in flight must not be judged against
  // that older snapshot.
  const candidates = [...instances.values()];

  let activeJobs: Array<{ stream_id: string; session_id: string }> | null = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      activeJobs = await api.get<Array<{ stream_id: string; session_id: string }>>(API.CHAT.ACTIVE);
      break;
    } catch {
      // New backend not serving yet — back off and retry.
      await new Promise((r) => setTimeout(r, 400 * (attempt + 1)));
    }
  }

  if (activeJobs === null) {
    // Unknown is not terminal. Preserve generation/Stop/queue controls and
    // let the client continue its bounded recovery instead of pretending the
    // task ended merely because the restarted backend was slow to answer.
    for (const inst of candidates) {
      if (
        instances.get(inst.sessionId) !== inst
        || !streamLeases.isCurrent(inst.lease)
      ) {
        continue;
      }
      inst.client.resumeReconnect();
      inst.client.checkHealth();
    }
    return;
  }

  const liveStreamBySession = new Map(activeJobs.map((job) => [job.session_id, job.stream_id]));

  // Sessions we already track are reconciled in the loop below; the final
  // attach loop must skip them so it can't double-start one whose async
  // startStream() has not yet re-registered its instance.
  const handledSessions = new Set(candidates.map((inst) => inst.sessionId));

  for (const inst of candidates) {
    if (
      instances.get(inst.sessionId) !== inst
      || !streamLeases.isCurrent(inst.lease)
    ) {
      continue;
    }
    const liveStreamId = liveStreamBySession.get(inst.sessionId);
    if (liveStreamId === inst.streamId) {
      inst.client.resumeReconnect();
    } else if (liveStreamId) {
      stopStream(inst.sessionId);
      useChatStore.getState().startGeneration(inst.sessionId, liveStreamId);
      void startStream(inst.sessionId, liveStreamId);
    } else {
      await finalizeInterruptedStream(inst);
    }
  }

  // Attach any still-running jobs we are not yet tracking (parity with boot
  // hydration — e.g. a background session started just before the restart).
  for (const job of activeJobs) {
    if (
      handledSessions.has(job.session_id)
      || instances.has(job.session_id)
      || streamLeases.current(job.session_id)
    ) {
      continue;
    }
    useChatStore.getState().startGeneration(job.session_id, job.stream_id);
    void startStream(job.session_id, job.stream_id);
  }
}

/**
 * Wind down a stream whose backend job no longer exists: flush partial output,
 * drop the dead client, clear the generating flag, and refetch authoritative
 * state from the DB. No error toast — an interrupted local generation is a
 * recoverable, expected event, not a failure the user must act on.
 */
async function finalizeInterruptedStream(instance: StreamInstance): Promise<void> {
  const { sessionId, streamId } = instance;
  if (
    instances.get(sessionId) !== instance
    || !streamLeases.isCurrent(instance.lease)
    || useChatStore.getState().sessions[sessionId]?.streamId !== streamId
  ) {
    return;
  }
  stopStream(sessionId); // disposeInstance flushes buffered text while still generating
  if (useChatStore.getState().sessions[sessionId]?.streamId === streamId) {
    useChatStore.getState().finishGeneration(sessionId);
  }
  const qc = queryClientRef;
  if (qc) {
    await qc.invalidateQueries({ queryKey: queryKeys.messages.list(sessionId) });
    qc.invalidateQueries({ queryKey: queryKeys.sessions.all });
    qc.invalidateQueries({ queryKey: queryKeys.sessions.detail(sessionId) });
  }
}

/**
 * Fire a native notification when a session finishes, unless the user is
 * currently looking at that session in the foreground — in that case the
 * normal UI is the notification.
 */
function maybeNotifyFinish(sessionId: string, kind: "done" | "error", errorMessage?: string): void {
  const focusedSessionId = useChatStore.getState().focusedSessionId;
  if (focusedSessionId === sessionId && typeof document !== "undefined" && !document.hidden) {
    return;
  }
  const qc = queryClientRef;
  const session = qc?.getQueryData<SessionResponse>(queryKeys.sessions.detail(sessionId));
  const sessionTitle = session?.title?.trim() || "Background task";
  const title = kind === "done"
    ? `${sessionTitle} finished`
    : `${sessionTitle} stopped`;
  const body = kind === "done"
    ? "Click to open the conversation."
    : (errorMessage ?? "Click to open the conversation.");
  void notifyBackgroundFinish({ sessionId, title, body, kind });
}

/** Cleanup, for tests / hot reload. Not used in production app code. */
export function disposeAllStreams(): void {
  for (const inst of instances.values()) disposeInstance(inst);
  instances.clear();
  streamLeases.clearAll();
  unlistenBackendRestarting?.();
  unlistenBackendRestarted?.();
  unlistenVisibilityChange?.();
  unlistenBackendRestarting = null;
  unlistenBackendRestarted = null;
  unlistenVisibilityChange = null;
  globalListenersInstalled = false;
}

// Silence unused-status-import warning — the registry uses the type indirectly.
export type _SSEStatus = SSEConnectionStatus;
