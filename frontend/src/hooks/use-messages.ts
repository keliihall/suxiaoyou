"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  keepPreviousData,
  useInfiniteQuery,
  useQuery,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import { API, queryKeys } from "@/lib/constants";
import { conversationHistoryWindowOffsets } from "@/lib/conversation-outline";
import type {
  ConversationTurnIndex,
  PaginatedMessages,
} from "@/types/message";

export const MESSAGE_PAGE_SIZE = 50;

interface HistoryWindow {
  sessionId: string;
  pages: PaginatedMessages[];
  createdAt: number;
}

function flattenPages(pages: PaginatedMessages[] | undefined) {
  const byId = new Map<string, PaginatedMessages["messages"][number]>();
  const order: string[] = [];
  for (const message of pages?.flatMap((page) => page.messages) ?? []) {
    if (!byId.has(message.id)) order.push(message.id);
    byId.set(message.id, message);
  }
  return order.map((id) => byId.get(id)!);
}

/**
 * Hook to fetch messages with reverse infinite scroll.
 *
 * Initial load fetches the latest page (offset=-1).
 * `fetchPreviousPage()` loads older messages.
 * Pages are stored oldest-first: pages[0] = oldest loaded, pages[last] = newest.
 */
export function useMessages(sessionId: string | undefined) {
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;
  const historyRequestRef = useRef(0);
  const historyLoadAbortRef = useRef<AbortController | null>(null);
  const [historyWindow, setHistoryWindow] = useState<HistoryWindow | null>(null);
  const [isFetchingHistoryPreviousPage, setIsFetchingHistoryPreviousPage] =
    useState(false);
  const [isFetchingHistoryNextPage, setIsFetchingHistoryNextPage] =
    useState(false);

  const query = useInfiniteQuery({
    queryKey: queryKeys.messages.list(sessionId!),
    queryFn: ({ pageParam }: { pageParam: number }) =>
      api.get<PaginatedMessages>(
        API.MESSAGES.LIST(sessionId!, MESSAGE_PAGE_SIZE, pageParam),
      ),
    initialPageParam: -1 as number,
    getPreviousPageParam: (firstPage: PaginatedMessages) => {
      if (firstPage.offset <= 0) return undefined;
      return Math.max(0, firstPage.offset - MESSAGE_PAGE_SIZE);
    },
    // The live cache always ends at the latest page. Directed history pages
    // live in an isolated local window below so stream reconciliation can
    // safely keep treating pages.at(-1) as authoritative latest state.
    getNextPageParam: (): undefined => undefined,
    enabled: !!sessionId,
    refetchOnWindowFocus: true,
    staleTime: 5_000, // Refetch if data is older than 5s (catches remote-generated sessions)
    // Poll every 10s to catch channel messages (WhatsApp, Discord, etc.)
    refetchInterval: 10_000,
    placeholderData: keepPreviousData,
  });

  const turnIndexQuery = useQuery({
    queryKey: queryKeys.messages.turnIndex(sessionId!),
    queryFn: () =>
      api.get<ConversationTurnIndex>(API.MESSAGES.TURN_INDEX(sessionId!)),
    enabled: !!sessionId,
    staleTime: 5_000,
    refetchOnWindowFocus: true,
    refetchInterval: 10_000,
  });

  const cancelHistoryNavigation = useCallback(() => {
    historyRequestRef.current += 1;
    historyLoadAbortRef.current?.abort();
    historyLoadAbortRef.current = null;
  }, []);

  useEffect(() => {
    cancelHistoryNavigation();
    setHistoryWindow(null);
    setIsFetchingHistoryPreviousPage(false);
    setIsFetchingHistoryNextPage(false);
  }, [cancelHistoryNavigation, sessionId]);

  useEffect(
    () => () => cancelHistoryNavigation(),
    [cancelHistoryNavigation],
  );

  const mainTotal = query.data?.pages.at(-1)?.total ?? 0;
  const isHistoryWindow =
    !!sessionId && historyWindow?.sessionId === sessionId;
  const activeHistoryPages =
    historyWindow && historyWindow.sessionId === sessionId
      ? historyWindow.pages
      : undefined;
  const knownTotal = Math.max(
    turnIndexQuery.data?.total_messages ?? 0,
    mainTotal,
    activeHistoryPages?.[0]?.total ?? 0,
  );

  /** Load a contiguous, isolated page window around an unloaded user turn. */
  const loadTurnWindow = useCallback(
    async (messageOffset: number): Promise<void> => {
      if (!sessionId) return;
      if (knownTotal <= 0) return;

      const offsets = conversationHistoryWindowOffsets(
        messageOffset,
        knownTotal,
        MESSAGE_PAGE_SIZE,
      );

      cancelHistoryNavigation();
      const requestId = historyRequestRef.current;
      const controller = new AbortController();
      historyLoadAbortRef.current = controller;
      try {
        const pages = await Promise.all(
          offsets.map((offset) =>
            api.get<PaginatedMessages>(
              API.MESSAGES.LIST(sessionId, MESSAGE_PAGE_SIZE, offset),
              { signal: controller.signal },
            ),
          ),
        );
        pages.sort((a, b) => a.offset - b.offset);
        if (
          requestId !== historyRequestRef.current
          || controller.signal.aborted
          || activeSessionRef.current !== sessionId
        ) {
          return;
        }
        setHistoryWindow({ sessionId, pages, createdAt: Date.now() });
      } finally {
        if (historyLoadAbortRef.current === controller) {
          historyLoadAbortRef.current = null;
        }
      }
    },
    [cancelHistoryNavigation, knownTotal, sessionId],
  );

  const exitHistoryWindow = useCallback(() => {
    cancelHistoryNavigation();
    setHistoryWindow(null);
    setIsFetchingHistoryPreviousPage(false);
    setIsFetchingHistoryNextPage(false);
  }, [cancelHistoryNavigation]);

  // If an edit rewrites the conversation while this window is open, prune
  // deleted messages in place. Do not expose the live view here: an early
  // target may be absent from that cache, and ChatView exits history only
  // after useChat atomically installs an authoritative latest page.
  useEffect(() => {
    if (!isHistoryWindow || !activeHistoryPages?.length || !historyWindow) return;
    const indexedTotal = turnIndexQuery.data?.total_messages;
    const windowTotal = activeHistoryPages[0].total;
    if (
      turnIndexQuery.dataUpdatedAt >= historyWindow.createdAt
      && indexedTotal !== undefined
      && indexedTotal < windowTotal
    ) {
      setHistoryWindow((current) => {
        if (!current || current.sessionId !== sessionId) return current;
        const pages = current.pages
          .map((page) => ({
            ...page,
            total: indexedTotal,
            messages: page.messages.filter(
              (_message, index) => page.offset + index < indexedTotal,
            ),
          }))
          .filter((page) => page.messages.length > 0);
        return { ...current, pages };
      });
    }
  }, [
    activeHistoryPages,
    isHistoryWindow,
    historyWindow,
    sessionId,
    turnIndexQuery.data?.total_messages,
    turnIndexQuery.dataUpdatedAt,
  ]);

  const fetchHistoryPreviousPage = useCallback(async (): Promise<void> => {
    if (!sessionId || !activeHistoryPages?.length || isFetchingHistoryPreviousPage) {
      return;
    }
    const firstOffset = activeHistoryPages[0].offset;
    if (firstOffset <= 0) return;
    const offset = Math.max(0, firstOffset - MESSAGE_PAGE_SIZE);
    setIsFetchingHistoryPreviousPage(true);
    try {
      const page = await api.get<PaginatedMessages>(
        API.MESSAGES.LIST(sessionId, MESSAGE_PAGE_SIZE, offset),
      );
      if (activeSessionRef.current !== sessionId) return;
      setHistoryWindow((current) => {
        if (current?.sessionId !== sessionId) return current;
        const pages = [page, ...current.pages.filter((item) => item.offset !== page.offset)]
          .sort((a, b) => a.offset - b.offset);
        return { sessionId, pages, createdAt: current.createdAt };
      });
    } finally {
      if (activeSessionRef.current === sessionId) {
        setIsFetchingHistoryPreviousPage(false);
      }
    }
  }, [
    activeHistoryPages,
    isFetchingHistoryPreviousPage,
    sessionId,
  ]);

  const fetchHistoryNextPage = useCallback(async (): Promise<void> => {
    if (!sessionId || !activeHistoryPages?.length || isFetchingHistoryNextPage) {
      return;
    }
    const lastPage = activeHistoryPages.at(-1)!;
    const offset = lastPage.offset + lastPage.messages.length;
    if (lastPage.messages.length === 0 || offset >= knownTotal) return;
    setIsFetchingHistoryNextPage(true);
    try {
      const page = await api.get<PaginatedMessages>(
        API.MESSAGES.LIST(sessionId, MESSAGE_PAGE_SIZE, offset),
      );
      if (activeSessionRef.current !== sessionId) return;
      setHistoryWindow((current) => {
        if (current?.sessionId !== sessionId) return current;
        const pages = [...current.pages.filter((item) => item.offset !== page.offset), page]
          .sort((a, b) => a.offset - b.offset);
        return { sessionId, pages, createdAt: current.createdAt };
      });
    } finally {
      if (activeSessionRef.current === sessionId) {
        setIsFetchingHistoryNextPage(false);
      }
    }
  }, [
    activeHistoryPages,
    isFetchingHistoryNextPage,
    knownTotal,
    sessionId,
  ]);

  // Flatten pages into a single chronological array. Reverse infinite scroll
  // can briefly overlap the latest page with older pages after refetches.
  const mainMessages = useMemo(
    () => flattenPages(query.data?.pages),
    [query.data],
  );
  const historyMessages = useMemo(
    () => flattenPages(activeHistoryPages),
    [activeHistoryPages],
  );
  const messages = isHistoryWindow ? historyMessages : mainMessages;

  const hasHistoryPreviousPage =
    !!activeHistoryPages?.length && activeHistoryPages[0].offset > 0;
  const lastHistoryPage = activeHistoryPages?.at(-1);
  const hasHistoryNextPage = !!lastHistoryPage
    && lastHistoryPage.messages.length > 0
    && lastHistoryPage.offset + lastHistoryPage.messages.length < knownTotal;
  const fetchMainPreviousPage = query.fetchPreviousPage;

  const fetchDisplayPreviousPage = useCallback(async (): Promise<void> => {
    if (isHistoryWindow) {
      await fetchHistoryPreviousPage();
      return;
    }
    await fetchMainPreviousPage();
  }, [fetchHistoryPreviousPage, fetchMainPreviousPage, isHistoryWindow]);

  return {
    ...query,
    messages,
    total: knownTotal,
    hasPreviousPage: isHistoryWindow
      ? hasHistoryPreviousPage
      : query.hasPreviousPage,
    isFetchingPreviousPage: isHistoryWindow
      ? isFetchingHistoryPreviousPage
      : query.isFetchingPreviousPage,
    fetchPreviousPage: fetchDisplayPreviousPage,
    hasNextPage: isHistoryWindow && hasHistoryNextPage,
    isFetchingNextPage: isHistoryWindow && isFetchingHistoryNextPage,
    fetchNextPage: fetchHistoryNextPage,
    isHistoryWindow,
    cancelHistoryNavigation,
    exitHistoryWindow,
    turnIndex: turnIndexQuery.data,
    isTurnIndexLoading: turnIndexQuery.isLoading,
    isTurnIndexError: turnIndexQuery.isError,
    refetchTurnIndex: turnIndexQuery.refetch,
    loadTurnWindow,
  };
}
