"use client";

import { useCallback, useMemo, useRef, useEffect, useState } from "react";
import { ArrowDown, Loader2 } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { useTranslation } from "react-i18next";
import { useScrollAnchor } from "@/hooks/use-scroll-anchor";
import { MessageItem } from "./message-item";
import { ConversationOutline } from "./conversation-outline";
import { AssistantMessageGroup } from "./assistant-message-group";
import { StreamingMessage } from "./assistant-message";
import { FileChip } from "@/components/chat/file-chip";
import { Skeleton } from "@/components/ui/skeleton";
import type { EditAndResendResult, FileAttachment } from "@/types/chat";
import { extractTextFromPartResponses } from "@/lib/utils";
import type {
  ConversationTurn,
  MessageResponse,
  PartData,
} from "@/types/message";

/** A user message or a group of consecutive assistant messages. */
type MessageGroup =
  | { kind: "user"; message: MessageResponse }
  | { kind: "assistant"; messages: MessageResponse[] };

/**
 * Group consecutive assistant messages into a single visual block.
 *
 * The backend creates a separate assistant message for each agent step,
 * but the user expects to see a single response per prompt.
 */
function groupMessages(messages: MessageResponse[]): MessageGroup[] {
  const groups: MessageGroup[] = [];
  let assistantBatch: MessageResponse[] = [];

  const isStandaloneAssistantMessage = (msg: MessageResponse) => {
    const data = msg.data as unknown as Record<string, unknown>;
    return data.role === "assistant" && (
      data.summary === true ||
      data.system === true ||
      msg.parts.some((part) => part.data.type === "compaction")
    );
  };

  const flushBatch = () => {
    if (assistantBatch.length > 0) {
      groups.push({ kind: "assistant", messages: assistantBatch });
      assistantBatch = [];
    }
  };

  for (const msg of messages) {
    if (msg.data.role === "assistant") {
      if (isStandaloneAssistantMessage(msg)) {
        flushBatch();
        groups.push({ kind: "assistant", messages: [msg] });
        continue;
      }
      assistantBatch.push(msg);
    } else if (
      msg.data.role === "user" &&
      (msg.data as unknown as Record<string, unknown>).system
    ) {
      // System-injected user messages (continuations, nudges) are invisible
      // and must NOT break the assistant message grouping.
      continue;
    } else {
      flushBatch();
      groups.push({ kind: "user", message: msg });
    }
  }
  flushBatch();

  return groups;
}

interface MessageListProps {
  messages: MessageResponse[];
  isLoading: boolean;
  isGenerating: boolean;
  /** Stream ID — only set after the backend confirms the generation. */
  streamId: string | null;
  /** Optimistic user message text shown before the API confirms. */
  pendingUserText: string | null;
  /** Attachments for the optimistic user bubble. */
  pendingAttachments?: FileAttachment[] | null;
  streamingParts: PartData[];
  streamingText: string;
  streamingReasoning: string;
  /** Whether the active stream is waiting for a user decision. */
  isAwaitingConfirmation?: boolean;
  /** Callback to edit a user message and re-generate from that point. */
  onEditAndResend?: (messageId: string, newText: string, attachments?: FileAttachment[]) => Promise<EditAndResendResult>;
  /** Workspace directory for @mention in edit mode. */
  directory?: string | null;
  /** Session ID for @mention file ingestion. */
  sessionId?: string;
  /** Whether there are older messages to load. */
  hasPreviousPage?: boolean;
  /** Whether older messages are currently being fetched. */
  isFetchingPreviousPage?: boolean;
  /** Fetch the next batch of older messages. */
  fetchPreviousPage?: () => Promise<void>;
  /** Whether a directed history window has newer pages below it. */
  hasNextPage?: boolean;
  /** Whether newer history pages are currently being fetched. */
  isFetchingNextPage?: boolean;
  /** Fetch the next batch of newer history messages. */
  fetchNextPage?: () => Promise<void>;
  /** Complete lightweight outline for all visible user turns. */
  turns?: ConversationTurn[];
  /** Fetch a contiguous history window containing an unloaded turn. */
  onLocateTurn?: (messageOffset: number) => Promise<void>;
  /** Invalidate and abort any in-flight directed history request. */
  onCancelLocate?: () => void;
  /** Total authoritative message count, independent of loaded page count. */
  totalMessageCount?: number;
  /** True while displaying isolated directed-history pages. */
  isHistoryWindow?: boolean;
  /** Restore the live latest-page view. */
  onExitHistoryWindow?: () => void;
}

export function MessageList({
  messages,
  isLoading,
  isGenerating,
  streamId,
  pendingUserText,
  pendingAttachments,
  streamingParts,
  streamingText,
  streamingReasoning,
  isAwaitingConfirmation = false,
  onEditAndResend,
  directory,
  sessionId,
  hasPreviousPage,
  isFetchingPreviousPage,
  fetchPreviousPage,
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
  turns = [],
  onLocateTurn,
  onCancelLocate,
  totalMessageCount = messages.length,
  isHistoryWindow = false,
  onExitHistoryWindow,
}: MessageListProps) {
  const { t } = useTranslation("chat");
  const {
    scrollRef,
    scrollElementRef,
    bottomRef,
    isAtBottom,
    scrollToBottom,
    suspendAutoScroll,
  } = useScrollAnchor();
  const topSentinelRef = useRef<HTMLDivElement>(null);
  const bottomSentinelRef = useRef<HTMLDivElement>(null);
  const paginationInFlightRef = useRef(false);
  const [listShellElement, setListShellElement] = useState<HTMLDivElement | null>(null);
  const [contentIsTallEnough, setContentIsTallEnough] = useState(false);
  const messageElementsRef = useRef<Map<string, HTMLDivElement>>(new Map());
  const [activeTurnMessageId, setActiveTurnMessageId] = useState<string | null>(null);
  const [pendingLocateMessageId, setPendingLocateMessageId] = useState<string | null>(null);
  const pendingLocateRef = useRef<string | null>(null);
  const [locateErrorMessageId, setLocateErrorMessageId] = useState<string | null>(null);
  const locateSequenceRef = useRef(0);
  const locateRestoreActiveRef = useRef<string | null>(null);
  const locateRestoreForwardPagingRef = useRef<boolean | null>(null);
  const locateTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const enableForwardPagingTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const returnToLatestRef = useRef(false);
  const [unreadCount, setUnreadCount] = useState(0);
  const [canFetchOlderMessages, setCanFetchOlderMessages] = useState(false);
  const [canFetchNewerMessages, setCanFetchNewerMessages] = useState(false);
  const anchoredSessionRef = useRef<string | undefined>(undefined);

  // Keep StreamingMessage visible briefly after generation finishes so the
  // DB-fetched AssistantMessageGroup has time to render. Without this,
  // there's a 1-frame blank flash between StreamingMessage unmounting and
  // the DB messages mounting.
  const wasGeneratingRef = useRef(false);
  const prevMessageCountRef = useRef(messages?.length ?? 0);
  const [showStreamingFallback, setShowStreamingFallback] = useState(false);

  const listShellRef = useCallback((element: HTMLDivElement | null) => {
    setListShellElement(element);
  }, []);

  const registerMessageElement = useCallback(
    (messageId: string, element: HTMLDivElement | null) => {
      if (element) messageElementsRef.current.set(messageId, element);
      else messageElementsRef.current.delete(messageId);
    },
    [],
  );

  useEffect(() => {
    if (!listShellElement) return;
    const update = () => setContentIsTallEnough(listShellElement.clientHeight >= 280);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(listShellElement);
    return () => observer.disconnect();
  }, [listShellElement]);

  useEffect(() => {
    return () => {
      onCancelLocate?.();
      if (locateTimeoutRef.current) clearTimeout(locateTimeoutRef.current);
      if (enableForwardPagingTimeoutRef.current) {
        clearTimeout(enableForwardPagingTimeoutRef.current);
      }
    };
  }, [onCancelLocate]);

  useEffect(() => {
    if (isGenerating) {
      wasGeneratingRef.current = true;
      prevMessageCountRef.current = messages?.length ?? 0;
      setShowStreamingFallback(false);
    } else if (wasGeneratingRef.current) {
      wasGeneratingRef.current = false;
      setShowStreamingFallback(true);
      const timer = setTimeout(() => setShowStreamingFallback(false), 2000);
      return () => clearTimeout(timer);
    }
  }, [isGenerating, messages.length]);

  useEffect(() => {
    if (showStreamingFallback && (messages?.length ?? 0) > prevMessageCountRef.current) {
      setShowStreamingFallback(false);
    }
  }, [messages.length, showStreamingFallback]);

  useEffect(() => {
    if (anchoredSessionRef.current === sessionId) return;
    anchoredSessionRef.current = sessionId;
    setCanFetchOlderMessages(false);
    setCanFetchNewerMessages(false);
    streamedHandoffIdsRef.current = new Set();
    messageElementsRef.current.clear();
    locateSequenceRef.current += 1;
    pendingLocateRef.current = null;
    locateRestoreActiveRef.current = null;
    locateRestoreForwardPagingRef.current = null;
    setPendingLocateMessageId(null);
    setLocateErrorMessageId(null);
    setActiveTurnMessageId(null);
    setUnreadCount(0);
    returnToLatestRef.current = false;
  }, [sessionId]);

  useEffect(() => {
    if (canFetchOlderMessages || isLoading || messages.length === 0) return;
    if (sessionId && messages.some((message) => message.session_id !== sessionId)) return;

    const frame = requestAnimationFrame(() => {
      const container = scrollElementRef.current;
      if (!container) return;
      container.scrollTop = container.scrollHeight;
      setCanFetchOlderMessages(true);
    });

    return () => cancelAnimationFrame(frame);
  }, [canFetchOlderMessages, isLoading, messages, scrollElementRef, sessionId]);

  // Reverse infinite scroll: observe top sentinel to load older messages
  useEffect(() => {
    const sentinel = topSentinelRef.current;
    const container = scrollElementRef.current;
    if (!sentinel || !container || !canFetchOlderMessages || !hasPreviousPage || isFetchingPreviousPage) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (
          entry.isIntersecting
          && hasPreviousPage
          && !isFetchingPreviousPage
          && !paginationInFlightRef.current
        ) {
          // Save scroll height before prepending for scroll position restoration
          const prevHeight = container.scrollHeight;
          paginationInFlightRef.current = true;
          void fetchPreviousPage?.()
            .then(() => {
              // Restore the same visible content after the prepend commits.
              requestAnimationFrame(() => {
                const newHeight = container.scrollHeight;
                container.scrollTop += newHeight - prevHeight;
              });
            })
            .catch(() => {})
            .finally(() => {
              paginationInFlightRef.current = false;
            });
        }
      },
      { root: container, rootMargin: "200px 0px 0px 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [canFetchOlderMessages, hasPreviousPage, isFetchingPreviousPage, fetchPreviousPage, scrollElementRef]);

  // Directed history windows can be traversed in both directions. The live
  // infinite query never enters this path and remains latest-page anchored.
  useEffect(() => {
    const sentinel = bottomSentinelRef.current;
    const container = scrollElementRef.current;
    if (
      !sentinel
      || !container
      || !canFetchNewerMessages
      || !hasNextPage
      || isFetchingNextPage
    ) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (
          entry.isIntersecting
          && hasNextPage
          && !isFetchingNextPage
          && !paginationInFlightRef.current
        ) {
          paginationInFlightRef.current = true;
          void fetchNextPage?.()
            .catch(() => {})
            .finally(() => {
              paginationInFlightRef.current = false;
            });
        }
      },
      { root: container, rootMargin: "0px 0px 200px 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [
    canFetchNewerMessages,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    scrollElementRef,
  ]);

  // Track known message IDs to distinguish historical vs new messages.
  // Messages present on first render (or first data load) are "old" — skip animation.
  // Messages that appear later are "new" — animate in.
  const knownIdsRef = useRef<Set<string>>(new Set());
  const initialLoadDoneRef = useRef(false);

  // Message IDs whose content was already shown live by the StreamingMessage.
  // When the stream ends, the persisted DB bubble replaces the live one in the
  // same commit. Without this, the persisted bubble counts as "new" and fades
  // in from opacity 0 — so the content jumps from the stream's opacity-1 down
  // to 0 and fades back, reading as the message blinking out and flashing back.
  // Recording these IDs lets us suppress that entry animation on handoff.
  const streamedHandoffIdsRef = useRef<Set<string>>(new Set());

  // On first non-loading render with messages, seed the known IDs set
  useEffect(() => {
    if (!isLoading && messages.length > 0 && !initialLoadDoneRef.current) {
      initialLoadDoneRef.current = true;
      knownIdsRef.current = new Set(messages.map((m) => m.id));
    }
  }, [isLoading, messages]);

  // Build a set of "new" message IDs (messages not in the initial set)
  const newMessageIds = useMemo(() => {
    if (!initialLoadDoneRef.current) return new Set<string>();
    const newIds = new Set<string>();
    for (const msg of messages) {
      if (!knownIdsRef.current.has(msg.id)) {
        newIds.add(msg.id);
        knownIdsRef.current.add(msg.id);
      }
    }
    return newIds;
  }, [messages]);

  // Reset unread count when user scrolls to bottom
  useEffect(() => {
    if (isAtBottom && !isHistoryWindow) setUnreadCount(0);
  }, [isAtBottom, isHistoryWindow]);

  // Increment unread count when new messages arrive while scrolled up
  const prevTotalCountRef = useRef(totalMessageCount);
  const totalCountSessionRef = useRef(sessionId);

  useEffect(() => {
    if (totalCountSessionRef.current !== sessionId) {
      totalCountSessionRef.current = sessionId;
      prevTotalCountRef.current = totalMessageCount;
      return;
    }
    const previousTotal = prevTotalCountRef.current;
    prevTotalCountRef.current = totalMessageCount;
    if (
      totalMessageCount > previousTotal
      && (isHistoryWindow || !isAtBottom)
    ) {
      setUnreadCount((count) => count + (totalMessageCount - previousTotal));
    }
  }, [isAtBottom, isHistoryWindow, sessionId, totalMessageCount]);

  // Group consecutive assistant messages so multi-step responses render as one block
  // Regroup whenever message content changes. Parts can be appended to existing
  // message IDs during/after generation, so depending only on length/last-id can
  // leave stale groups that miss the final assistant text until a full refresh.
  const groups = useMemo(
    () => groupMessages(messages),
    [messages]
  );

  useEffect(() => {
    if (turns.length === 0) {
      setActiveTurnMessageId(null);
      return;
    }
    setActiveTurnMessageId((current) => {
      if (current && turns.some((turn) => turn.message_id === current)) {
        return current;
      }
      return turns.at(-1)?.message_id ?? null;
    });
  }, [sessionId, turns]);

  useEffect(() => {
    const turnIds = new Set(turns.map((turn) => turn.message_id));
    const pending = pendingLocateRef.current;
    if (pending && !turnIds.has(pending)) {
      locateSequenceRef.current += 1;
      onCancelLocate?.();
      pendingLocateRef.current = null;
      locateRestoreActiveRef.current = null;
      locateRestoreForwardPagingRef.current = null;
      setPendingLocateMessageId(null);
      if (locateTimeoutRef.current) {
        clearTimeout(locateTimeoutRef.current);
        locateTimeoutRef.current = null;
      }
      if (enableForwardPagingTimeoutRef.current) {
        clearTimeout(enableForwardPagingTimeoutRef.current);
        enableForwardPagingTimeoutRef.current = null;
      }
    }
    setLocateErrorMessageId((current) =>
      current && !turnIds.has(current) ? null : current,
    );
  }, [onCancelLocate, turns]);

  // User-message anchors are point markers. Observe the top portion of the
  // scroller so a long assistant response keeps the preceding turn active
  // until the next user prompt crosses into that reading region.
  useEffect(() => {
    const container = scrollElementRef.current;
    if (!container || turns.length === 0) return;
    const turnIds = new Set(turns.map((turn) => turn.message_id));
    let frame = 0;
    const updateActiveTurn = () => {
      frame = 0;
      const rootRect = container.getBoundingClientRect();
      const readingLine = rootRect.top + Math.min(96, container.clientHeight * 0.25);
      let candidate: string | null = null;
      let firstAfterLine: string | null = null;
      for (const turn of turns) {
        const element = messageElementsRef.current.get(turn.message_id);
        if (!element) continue;
        if (element.getBoundingClientRect().top <= readingLine) {
          candidate = turn.message_id;
        } else {
          firstAfterLine = turn.message_id;
          break;
        }
      }
      const next = candidate ?? firstAfterLine;
      if (next) setActiveTurnMessageId(next);
    };
    const scheduleUpdate = () => {
      if (!frame) frame = requestAnimationFrame(updateActiveTurn);
    };
    const observer = new IntersectionObserver(
      scheduleUpdate,
      { root: container, threshold: 0 },
    );
    for (const [messageId, element] of messageElementsRef.current) {
      if (turnIds.has(messageId)) observer.observe(element);
    }
    container.addEventListener("scroll", scheduleUpdate, { passive: true });
    scheduleUpdate();
    return () => {
      observer.disconnect();
      container.removeEventListener("scroll", scheduleUpdate);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [messages, scrollElementRef, turns]);

  const finishPendingLocate = useCallback((messageId: string) => {
    const element = messageElementsRef.current.get(messageId);
    if (!element) return false;
    if (locateTimeoutRef.current) {
      clearTimeout(locateTimeoutRef.current);
      locateTimeoutRef.current = null;
    }
    pendingLocateRef.current = null;
    locateRestoreActiveRef.current = null;
    locateRestoreForwardPagingRef.current = null;
    setPendingLocateMessageId(null);
    setLocateErrorMessageId(null);
    setActiveTurnMessageId(messageId);
    requestAnimationFrame(() => {
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      element.scrollIntoView({
        block: "start",
        behavior: reduceMotion ? "auto" : "smooth",
      });
    });
    if (enableForwardPagingTimeoutRef.current) {
      clearTimeout(enableForwardPagingTimeoutRef.current);
    }
    // Do not let the bottom sentinel append newer pages while the replacement
    // window is still at its inherited scrollTop. Wait for the smooth target
    // jump to settle, then allow deliberate downward traversal.
    enableForwardPagingTimeoutRef.current = setTimeout(() => {
      setCanFetchNewerMessages(true);
    }, 650);
    return true;
  }, []);

  useEffect(() => {
    const messageId = pendingLocateRef.current;
    if (messageId) finishPendingLocate(messageId);
  }, [finishPendingLocate, messages]);

  const beginPendingLocate = useCallback((messageId: string) => {
    pendingLocateRef.current = messageId;
    setPendingLocateMessageId(messageId);
    setLocateErrorMessageId(null);
    if (locateTimeoutRef.current) clearTimeout(locateTimeoutRef.current);
    locateTimeoutRef.current = setTimeout(() => {
      if (pendingLocateRef.current !== messageId) return;
      locateTimeoutRef.current = null;
      locateSequenceRef.current += 1;
      onCancelLocate?.();
      if (enableForwardPagingTimeoutRef.current) {
        clearTimeout(enableForwardPagingTimeoutRef.current);
        enableForwardPagingTimeoutRef.current = null;
      }
      pendingLocateRef.current = null;
      setPendingLocateMessageId(null);
      setLocateErrorMessageId(messageId);
      setActiveTurnMessageId(locateRestoreActiveRef.current);
      setCanFetchNewerMessages(
        locateRestoreForwardPagingRef.current ?? false,
      );
      locateRestoreActiveRef.current = null;
      locateRestoreForwardPagingRef.current = null;
    }, 4_000);
  }, [onCancelLocate]);

  const handleSelectTurn = useCallback(async (turn: ConversationTurn) => {
    const hadPendingLocate = pendingLocateRef.current !== null;
    const restoreForwardPaging = hadPendingLocate
      ? (locateRestoreForwardPagingRef.current ?? canFetchNewerMessages)
      : canFetchNewerMessages;
    const restoreActive = hadPendingLocate
      ? locateRestoreActiveRef.current
      : activeTurnMessageId;
    locateSequenceRef.current += 1;
    onCancelLocate?.();
    if (locateTimeoutRef.current) {
      clearTimeout(locateTimeoutRef.current);
      locateTimeoutRef.current = null;
    }
    if (enableForwardPagingTimeoutRef.current) {
      clearTimeout(enableForwardPagingTimeoutRef.current);
      enableForwardPagingTimeoutRef.current = null;
    }
    pendingLocateRef.current = null;
    setPendingLocateMessageId(null);
    const sequence = ++locateSequenceRef.current;
    locateRestoreActiveRef.current = restoreActive;
    locateRestoreForwardPagingRef.current = restoreForwardPaging;
    suspendAutoScroll();
    setActiveTurnMessageId(turn.message_id);
    setLocateErrorMessageId(null);

    if (finishPendingLocate(turn.message_id)) return;
    setCanFetchNewerMessages(false);
    beginPendingLocate(turn.message_id);

    // The main query already owns the true latest page. Leaving an older
    // isolated window is enough to mount a recent target without another API
    // request, and preserves live-stream cache invariants.
    if (
      isHistoryWindow
      && turn.message_offset >= Math.max(0, totalMessageCount - 50)
    ) {
      onExitHistoryWindow?.();
      return;
    }

    try {
      if (!onLocateTurn) throw new Error("History navigation is unavailable");
      await onLocateTurn(turn.message_offset);
      if (sequence !== locateSequenceRef.current) return;
      // React commits the isolated window on the next render; the messages
      // effect above completes the scroll once its stable anchor is mounted.
    } catch {
      if (sequence !== locateSequenceRef.current) return;
      if (locateTimeoutRef.current) clearTimeout(locateTimeoutRef.current);
      locateTimeoutRef.current = null;
      pendingLocateRef.current = null;
      setPendingLocateMessageId(null);
      setLocateErrorMessageId(turn.message_id);
      setActiveTurnMessageId(restoreActive);
      locateRestoreActiveRef.current = null;
      locateRestoreForwardPagingRef.current = null;
      setCanFetchNewerMessages(restoreForwardPaging);
    }
  }, [
    activeTurnMessageId,
    beginPendingLocate,
    canFetchNewerMessages,
    finishPendingLocate,
    isHistoryWindow,
    onExitHistoryWindow,
    onLocateTurn,
    onCancelLocate,
    suspendAutoScroll,
    totalMessageCount,
  ]);

  const handleRetryLocate = useCallback(() => {
    const turn = turns.find((item) => item.message_id === locateErrorMessageId);
    if (turn) void handleSelectTurn(turn);
  }, [handleSelectTurn, locateErrorMessageId, turns]);

  const handleReturnToLatest = useCallback(() => {
    setUnreadCount(0);
    locateSequenceRef.current += 1;
    onCancelLocate?.();
    pendingLocateRef.current = null;
    locateRestoreActiveRef.current = null;
    locateRestoreForwardPagingRef.current = null;
    setPendingLocateMessageId(null);
    setLocateErrorMessageId(null);
    if (locateTimeoutRef.current) {
      clearTimeout(locateTimeoutRef.current);
      locateTimeoutRef.current = null;
    }
    if (enableForwardPagingTimeoutRef.current) {
      clearTimeout(enableForwardPagingTimeoutRef.current);
      enableForwardPagingTimeoutRef.current = null;
    }
    setCanFetchNewerMessages(false);
    if (isHistoryWindow) {
      returnToLatestRef.current = true;
      onExitHistoryWindow?.();
      return;
    }
    scrollToBottom();
  }, [isHistoryWindow, onCancelLocate, onExitHistoryWindow, scrollToBottom]);

  useEffect(() => {
    if (!returnToLatestRef.current || isHistoryWindow) return;
    returnToLatestRef.current = false;
    const frame = requestAnimationFrame(() => scrollToBottom());
    return () => cancelAnimationFrame(frame);
  }, [isHistoryWindow, messages, scrollToBottom]);

  // The shell message only exists after the backend created it (streamId is set).
  // During beginSending (streamId is null), we must NOT hide the previous response.
  const hasActiveStream = !!streamId && !isHistoryWindow;
  const hasVisibleStreamingReplacement = useMemo(() => {
    if (streamingText.trim() || streamingReasoning.trim()) return true;
    return streamingParts.some(
      (part) => part.type !== "step-start" && part.type !== "step-finish",
    );
  }, [streamingParts, streamingReasoning, streamingText]);

  // Don't show the optimistic user bubble if the DB-fetched messages already
  // contain a matching user message. This prevents duplicates after navigating
  // from /c/new to /c/{sessionId} (where useMessages fetches the persisted
  // user message while pendingUserText is still set in the global store).
  const showPendingBubble = useMemo(() => {
    if (isHistoryWindow) return false;
    if (!pendingUserText) return false;
    if (messages.length === 0) return true;
    const hasPendingInDb = messages.some((m) => {
      if ((m.data as { role: string }).role !== "user") return false;
      const fullText = extractTextFromPartResponses(m.parts);
      return fullText.includes(pendingUserText);
    });
    return !hasPendingInDb;
  }, [pendingUserText, messages, isHistoryWindow]);

  // Only show the loading state on the very first load (no cached/placeholder data).
  // When switching sessions with keepPreviousData, messages.length > 0 so we
  // skip the skeleton and render the (placeholder) messages for a seamless transition.
  const isFirstLoad = isLoading && messages.length === 0;

  if (isFirstLoad) {
    // When generating, skip skeletons and show the streaming message directly
    // to avoid a jarring skeleton → content transition during page navigation
    if (isGenerating || !!streamId) {
      return (
        <div
          ref={scrollRef}
          data-testid="message-list-scroller"
          className="relative flex-1 overflow-y-auto overscroll-contain scrollbar-auto"
        >
          {/* Show optimistic user bubble during loading so it doesn't flash
              away between navigation and message fetch completion */}
          {pendingUserText && (
            <div className="px-4 py-3">
              <div className="mx-auto max-w-3xl xl:max-w-4xl">
                <div className="flex justify-end">
                  <div className="max-w-[85%] sm:max-w-[70%] rounded-2xl bg-[var(--user-bubble-bg)] px-4 py-2.5 shadow-[var(--shadow-sm)] border border-[var(--border-default)]">
                    <div className="text-[13px] text-[var(--text-primary)] whitespace-pre-wrap break-words leading-relaxed">
                      {pendingUserText}
                    </div>
                    {pendingAttachments && pendingAttachments.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 mt-2">
                        {pendingAttachments.map((att) => (
                          <FileChip key={att.file_id} file={att} />
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          )}
          <div className="px-4 py-5">
            <div className="mx-auto max-w-3xl xl:max-w-4xl">
              <StreamingMessage
                sessionId={sessionId ?? null}
                parts={streamingParts}
                streamingText={streamingText}
                streamingReasoning={streamingReasoning}
                isAwaitingConfirmation={isAwaitingConfirmation}
              />
            </div>
          </div>
          <div ref={bottomRef} className="h-px" />
        </div>
      );
    }

    return (
      <div className="flex-1 overflow-y-auto p-4">
        <div
          className="mx-auto max-w-3xl xl:max-w-4xl space-y-6 animate-fade-in"
          style={{ animationDelay: "150ms", animationFillMode: "backwards" }}
        >
          {/* User message skeleton — right aligned */}
          <div className="flex justify-end">
            <Skeleton className="h-10 w-48 rounded-2xl" />
          </div>
          {/* Assistant message skeleton — left aligned */}
          <div className="space-y-2">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-4/5" />
            <Skeleton className="h-4 w-3/5" />
          </div>
          {/* Second pair */}
          <div className="flex justify-end">
            <Skeleton className="h-10 w-64 rounded-2xl" />
          </div>
          <div className="space-y-2">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-2/3" />
          </div>
        </div>
      </div>
    );
  }

  // The last assistant group is the live response while a stream is active for
  // this session (streamId set) or during the brief post-finish fallback. Its
  // content is shown by StreamingMessage throughout, so when the stream ends
  // and the persisted DB bubble takes over, that bubble must NOT animate in —
  // a fade would read as the just-finished response blinking out and flashing
  // back. Record its IDs here (the moment it becomes the last group under an
  // active stream, regardless of whether streaming content is "visible" yet)
  // so the swap to the persisted bubble is always seamless. Skipped while
  // showPendingBubble is true — then the last assistant group is a PREVIOUS
  // turn that should stay untouched.
  const lastGroupForCover = groups[groups.length - 1];
  if (
    (hasActiveStream || (showStreamingFallback && !isHistoryWindow)) &&
    !showPendingBubble &&
    lastGroupForCover?.kind === "assistant"
  ) {
    for (const m of lastGroupForCover.messages) {
      streamedHandoffIdsRef.current.add(m.id);
    }
  }

  // The most-recent user message must not animate in either: on send it hands
  // off from the optimistic bubble (which already played the send animation),
  // and otherwise it's known history. Keying off "latest user message" instead
  // of pendingUserText avoids the same fast-turn race the assistant fix hit —
  // the persisted copy can land AFTER pendingUserText is cleared.
  let lastUserMessageId: string | null = null;
  for (let i = groups.length - 1; i >= 0; i--) {
    if (groups[i].kind === "user") {
      lastUserMessageId = (groups[i] as { kind: "user"; message: MessageResponse }).message.id;
      break;
    }
  }

  return (
    <div ref={listShellRef} className="relative flex-1 overflow-hidden">
      <div
        ref={scrollRef}
        data-testid="message-list-scroller"
        className="h-full overflow-y-auto overscroll-contain scrollbar-auto"
      >
        {/* Top sentinel for reverse infinite scroll */}
        <div ref={topSentinelRef} className="h-px" />
        {isFetchingPreviousPage && (
          <div className="flex justify-center py-4">
            <Loader2 className="h-4 w-4 animate-spin text-[var(--text-tertiary)]" />
          </div>
        )}

        {messages.length === 0 && !isGenerating ? (
          <div className="flex items-center justify-center h-full text-[var(--text-tertiary)] text-sm">
            No messages yet
          </div>
        ) : (
          <>
            {groups.map((group) => {
              if (group.kind === "user") {
                return (
                  <MessageItem
                    key={group.message.id}
                    message={group.message}
                    isNew={newMessageIds.has(group.message.id) && group.message.id !== lastUserMessageId}
                    onEditAndResend={onEditAndResend}
                    isGenerating={isGenerating}
                    directory={directory}
                    sessionId={sessionId}
                    onElementChange={registerMessageElement}
                  />
                );
              }

              // Assistant group — hide the entire last group during active
              // streaming ONLY if it belongs to the current generation.
              // ``streamingParts`` in the chat-store accumulates every part
              // seen during the turn (from ``beginSending`` through
              // ``finishGeneration``), so the StreamingMessage below already
              // renders the earlier persisted step-messages' content. Showing
              // both here causes duplicate blocks with duplicate Sources
              // footers and an overlapping tool-call timeline.
              //
              // If ``showPendingBubble`` is true, the user just sent a new
              // message that isn't yet in the DB cache — meaning the last
              // assistant group is from a PREVIOUS turn. Don't hide it or the
              // previous AI response disappears when a follow-up is sent.
              const lastMsg = group.messages[group.messages.length - 1];
              const isLastOverall =
                messages.length > 0 && lastMsg.id === messages[messages.length - 1].id;

              // Assistant content always streams in live (via StreamingMessage)
              // before it persists, so the persisted bubble must NOT also fade
              // in — that double-appearance is the "blink out then flash back"
              // seen at stream end. The last assistant group is always either
              // the just-streamed response or known history, so it never
              // animates. (The final reply can finalize as a fresh message id
              // AFTER the stream ends — e.g. tool-call turns — so keying off the
              // streamed id alone misses it; the isLastOverall guard catches
              // every timing.) streamedHandoffIdsRef additionally covers a
              // streamed reply that a later message, like a post-compaction
              // summary, pushed out of last place.
              const groupIsNew =
                !isLastOverall &&
                group.messages.some((m) => newMessageIds.has(m.id)) &&
                !group.messages.some((m) => streamedHandoffIdsRef.current.has(m.id));

              if (
                (hasActiveStream || (showStreamingFallback && !isHistoryWindow)) &&
                hasVisibleStreamingReplacement &&
                isLastOverall &&
                !showPendingBubble
              ) {
                return null;
              }

              return (
                <AssistantMessageGroup
                  key={group.messages[0].id}
                  messages={group.messages}
                  isNew={groupIsNew}
                />
              );
            })}

            {/* Optimistic user message — shown instantly before API confirms.
                Hidden once the DB-fetched messages include the same text to
                avoid duplicates after page navigation. */}
            {showPendingBubble && (
              <div className="px-4 py-5">
                <div className="mx-auto max-w-3xl xl:max-w-4xl">
                  <motion.div
                    className="flex justify-end"
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{
                      type: "spring",
                      stiffness: 300,
                      damping: 30,
                      opacity: { duration: 0.2 },
                    }}
                  >
                    <div className="max-w-[85%] sm:max-w-[70%] rounded-2xl bg-[var(--user-bubble-bg)] px-4 py-2.5 shadow-[var(--shadow-sm)] border border-[var(--border-default)]">
                      <div className="text-[13px] text-[var(--text-primary)] whitespace-pre-wrap break-words leading-relaxed">
                        {pendingUserText}
                      </div>
                      {pendingAttachments && pendingAttachments.length > 0 && (
                        <div className="flex flex-wrap gap-1.5 mt-2">
                          {pendingAttachments.map((att) => (
                            <FileChip key={att.file_id} file={att} />
                          ))}
                        </div>
                      )}
                    </div>
                  </motion.div>
                </div>
              </div>
            )}

            {/* Currently streaming message — kept visible briefly after
                generation finishes so DB messages can mount first. */}
            {!isHistoryWindow && (isGenerating || !!streamId || showStreamingFallback) && (
              <div className="px-4 py-5">
                <div className="mx-auto max-w-3xl xl:max-w-4xl">
                  <StreamingMessage
                    sessionId={sessionId ?? null}
                    parts={streamingParts}
                    streamingText={streamingText}
                    streamingReasoning={streamingReasoning}
                    isAwaitingConfirmation={isAwaitingConfirmation}
                  />
                </div>
              </div>
            )}
          </>
        )}

        <div ref={bottomSentinelRef} className="h-px" />
        {isFetchingNextPage && (
          <div className="flex justify-center py-4">
            <Loader2 className="h-4 w-4 animate-spin text-[var(--text-tertiary)]" />
          </div>
        )}

        {/* Scroll anchor */}
        <div ref={bottomRef} className="h-px" />
      </div>

      <ConversationOutline
        turns={turns}
        activeMessageId={activeTurnMessageId}
        locatingMessageId={pendingLocateMessageId}
        locateErrorMessageId={locateErrorMessageId}
        contentIsTallEnough={contentIsTallEnough}
        onSelect={(turn) => { void handleSelectTurn(turn); }}
        onRetry={handleRetryLocate}
      />

      <span className="sr-only" aria-live="polite">
        {pendingLocateMessageId ? t("conversationLocating") : ""}
      </span>

      {/* Scroll to bottom button — outside scroll container so it never affects scrollHeight */}
      <AnimatePresence>
        {(!isAtBottom || isHistoryWindow) && (
          <motion.button
            initial={{ opacity: 0, scale: 0.8 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.8 }}
            transition={{ type: "spring", stiffness: 400, damping: 25 }}
            onClick={handleReturnToLatest}
            aria-label={t(isHistoryWindow ? "conversationReturnToLatest" : "scrollToBottom")}
            className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10 flex items-center justify-center h-9 w-9 rounded-full border border-[var(--border-default)] bg-[var(--surface-primary)] shadow-[var(--shadow-lg)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--surface-secondary)] transition-colors hover:[&_svg]:translate-y-0.5 [&_svg]:transition-transform [&_svg]:duration-150"
          >
            <ArrowDown className="h-4 w-4" />
            {unreadCount > 0 && (
              <span className="absolute -top-1.5 -right-1.5 flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-[var(--brand-primary)] text-[var(--brand-primary-text)] text-[10px] font-semibold leading-none">
                {unreadCount > 99 ? "99+" : unreadCount}
              </span>
            )}
          </motion.button>
        )}
      </AnimatePresence>
    </div>
  );
}
