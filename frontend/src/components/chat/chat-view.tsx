"use client";

import { useCallback, useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useChat } from "@/hooks/use-chat";
import { useMessages } from "@/hooks/use-messages";
import { useSessionModelRestore } from "@/hooks/use-session-model-restore";
import { useKeyboardShortcuts } from "@/hooks/use-keyboard-shortcuts";
import { useChatStore } from "@/stores/chat-store";
import { useArtifactStore } from "@/stores/artifact-store";
import { useActivityStore } from "@/stores/activity-store";
import { useWorkspaceStore, type WorkspaceTodo, type WorkspaceFile } from "@/stores/workspace-store";
import { api } from "@/lib/api";
import { API, queryKeys } from "@/lib/constants";
import { isInteractionAwaitingResolution } from "@/lib/interaction-response";
import { ChatHeader } from "./chat-header";
import { ChatForm } from "./chat-form";
import { MessageList } from "@/components/messages/message-list";
import { PermissionDialog } from "@/components/interactive/permission-dialog";
import { QuestionPrompt } from "@/components/interactive/question-prompt";
import { PlanAcceptPrompt } from "@/components/interactive/plan-accept-prompt";
import { OfflineOverlay } from "@/components/layout/offline-overlay";
import type { SessionResponse } from "@/types/session";

interface ChatViewProps {
  sessionId: string;
}

export function ChatView({ sessionId }: ChatViewProps) {
  const {
    sendMessage,
    queueMessage,
    cancelQueuedInput,
    pendingInputs,
    sendTaskBatch,
    editAndResend,
    stopGeneration,
    reconnectGeneration,
    recoverInteraction,
    respondToPermission,
    respondToQuestion,
    respondToPlanReview,
    isGenerating,
    isCompacting,
    streamId,
    pendingUserText,
    pendingAttachments,
    streamingParts,
    streamingText,
    streamingReasoning,
    pendingPermission,
    pendingQuestion,
    pendingPlanReview,
    isProgressStalled,
    lastBusinessProgressAt,
  } = useChat(sessionId);

  const { messages, isLoading, hasPreviousPage, isFetchingPreviousPage, fetchPreviousPage } = useMessages(sessionId);

  // Per-session model memory — restore this session's last-used model on entry.
  useSessionModelRestore(sessionId);

  const { data: session } = useQuery({
    queryKey: queryKeys.sessions.detail(sessionId),
    queryFn: () => api.get<SessionResponse>(API.SESSIONS.DETAIL(sessionId)),
    staleTime: 30_000,
  });

  // Auto-fix sessions with default title — set to first user message
  const qc = useQueryClient();
  useEffect(() => {
    if (!session || !messages || messages.length === 0) return;
    if (session.title && session.title !== "New Session") return;
    const firstUser = messages.find((m) => m.data?.role === "user");
    if (!firstUser) return;
    const textPart = firstUser.parts.find((p) => p.data?.type === "text");
    const text = textPart?.data?.type === "text" ? (textPart.data as { type: "text"; text: string }).text : undefined;
    if (!text) return;
    const title = text.trim().slice(0, 60);
    if (!title) return;
    api.patch(API.SESSIONS.DETAIL(sessionId), { title }).then(() => {
      qc.invalidateQueries({ queryKey: queryKeys.sessions.all });
      qc.setQueryData<SessionResponse>(
        queryKeys.sessions.detail(sessionId),
        (old) => (old ? { ...old, title } : old),
      );
    }).catch((e) => console.warn("[chat-view] Failed to auto-set title:", e));
  }, [session, messages, sessionId, qc]);

  // On session entry: hydrate this session's panels (artifact, activity,
  // workspace todos/files). We intentionally do NOT abort the in-flight
  // stream for any other session — the stream registry keeps it running in
  // the background so the user can come back to it later.
  useEffect(() => {
    useChatStore.getState().ensureSession(sessionId);
    useChatStore.getState().setFocusedSession(sessionId);
    useArtifactStore.getState().clearAll();
    useActivityStore.getState().close();
    useWorkspaceStore.getState().resetForSession();

    api.get<SessionResponse>(API.SESSIONS.DETAIL(sessionId)).then((s) => {
      if (s.directory) {
        useWorkspaceStore.getState().setActiveWorkspacePath(s.directory);
      }
    }).catch(() => {});

    api.get<{ todos: Array<{ content: string; status: string; activeForm?: string }> }>(
      API.SESSIONS.TODOS(sessionId),
    ).then((res) => {
      if (res.todos && res.todos.length > 0) {
        useWorkspaceStore.getState().setTodos(res.todos as WorkspaceTodo[]);
        useWorkspaceStore.getState().open();
      }
    }).catch(() => {});

    api.get<{ files: Array<{ name: string; path: string; type: string; tool: string }> }>(
      API.SESSIONS.FILES(sessionId),
    ).then((res) => {
      if (res.files && res.files.length > 0) {
        useWorkspaceStore.getState().setWorkspaceFiles(
          res.files.map((f) => ({ name: f.name, path: f.path, type: f.type as WorkspaceFile["type"] })),
        );
      }
    }).catch(() => {});

    return () => {
      // Only clear focus if we are still the focused session on unmount —
      // a fast route swap to another ChatView would otherwise wipe the
      // other view's focus claim on its way in.
      const cur = useChatStore.getState().focusedSessionId;
      if (cur === sessionId) {
        useChatStore.getState().setFocusedSession(null);
      }
    };
  }, [sessionId]);

  // Copy last assistant message to clipboard
  const handleCopyLast = useCallback(() => {
    if (!messages || messages.length === 0) return;

    // Find last assistant message
    const lastAssistantMessage = [...messages]
      .reverse()
      .find((msg) => (msg.data as { role: string }).role === "assistant");

    if (!lastAssistantMessage) {
      toast.error("No assistant message found");
      return;
    }

    // Extract text content
    const textContent = lastAssistantMessage.parts
      .filter((p) => p.data.type === "text")
      .map((p) => (p.data as { type: "text"; text: string }).text)
      .join("\n");

    if (!textContent) {
      toast.error("No text content to copy");
      return;
    }

    navigator.clipboard.writeText(textContent);
    toast.success("Copied to clipboard");
  }, [messages]);

  // Global keyboard shortcuts
  useKeyboardShortcuts({
    onStop: stopGeneration,
    onCopyLast: handleCopyLast,
    // Interactive cards own Escape (deny/cancel/keep planning). Letting the
    // global shortcut also abort the stream would turn one keypress into two
    // conflicting decisions.
    enabled: !pendingPermission && !pendingQuestion && !pendingPlanReview,
  });

  return (
    <div className="relative flex flex-1 flex-col h-full overflow-hidden bg-[var(--surface-chat)]">
      <OfflineOverlay />
      <ChatHeader sessionId={sessionId} />

      {/* Message list */}
      <MessageList
        messages={messages}
        isLoading={isLoading}
        isGenerating={isGenerating}
        streamId={streamId}
        pendingUserText={pendingUserText}
        pendingAttachments={pendingAttachments}
        streamingParts={streamingParts}
        streamingText={streamingText}
        streamingReasoning={streamingReasoning}
        isAwaitingConfirmation={
          (!!pendingPermission && isInteractionAwaitingResolution(pendingPermission.responseState)) ||
          (!!pendingQuestion && isInteractionAwaitingResolution(pendingQuestion.responseState)) ||
          (!!pendingPlanReview && isInteractionAwaitingResolution(pendingPlanReview.responseState))
        }
        onEditAndResend={editAndResend}
        directory={session?.directory}
        sessionId={sessionId}
        hasPreviousPage={hasPreviousPage}
        isFetchingPreviousPage={isFetchingPreviousPage}
        fetchPreviousPage={fetchPreviousPage}
      />

      {/* Interactive prompts */}
      {pendingPermission && (
        <PermissionDialog
          permission={pendingPermission}
          onRespond={respondToPermission}
          onRecover={() => recoverInteraction("permission", pendingPermission.callId)}
          onStop={stopGeneration}
        />
      )}

      {pendingQuestion && (
        <QuestionPrompt
          question={pendingQuestion}
          onRespond={respondToQuestion}
          onRecover={() => recoverInteraction("question", pendingQuestion.callId)}
          onStop={stopGeneration}
        />
      )}

      {/* A plan awaiting a decision replaces the composer. If confirmation
          recovery exhausts, keep the truthful recovery card but restore the
          queue composer so the session is never permanently locked. */}
      {pendingPlanReview && (
        <PlanAcceptPrompt
          review={pendingPlanReview}
          onRespond={respondToPlanReview}
          onRecover={() => recoverInteraction("plan", pendingPlanReview.callId)}
          onStop={stopGeneration}
        />
      )}
      {(!pendingPlanReview || pendingPlanReview.responseState === "recovery_needed") && (
        <ChatForm
          isGenerating={isGenerating}
          isCompacting={isCompacting || !!session?.time_compacting}
          onSend={sendMessage}
          onQueue={queueMessage}
          pendingInputs={pendingInputs}
          onCancelInput={cancelQueuedInput}
          isProgressStalled={isProgressStalled}
          lastBusinessProgressAt={lastBusinessProgressAt}
          onReconnect={reconnectGeneration}
          onSendTaskBatch={sendTaskBatch}
          onStop={stopGeneration}
          sessionId={sessionId}
          directory={session?.directory}
        />
      )}
    </div>
  );
}
