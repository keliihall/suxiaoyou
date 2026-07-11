"use client";

import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { API, queryKeys } from "@/lib/constants";
import { useChatStore } from "@/stores/chat-store";
import {
  getActiveStreamGeneration,
  getActiveStreamId,
  startStream,
} from "@/lib/session-stream-registry";
import {
  canCommitRemoteStreamAttach,
  needsRemoteStreamAttach,
  type RemoteAttachSnapshot,
} from "@/lib/stream-lifecycle";

/**
 * Poll for active generations in the current session.
 *
 * When another client (e.g. mobile) starts a generation in a session the PC
 * is viewing, the PC has no way to discover the stream_id — it only sets a
 * streamId when *it* initiates a prompt.
 *
 * This hook polls `GET /api/chat/active` every few seconds. When it finds an
 * active generation for the current session that the local stream registry
 * isn't already tracking, it attaches a stream and flips the bucket into
 * generating state.
 */
const POLL_INTERVAL = 5_000;
const STALE_REPOLL_DELAY = 100;

export function useRemoteGenerationSync(sessionId: string | undefined) {
  const queryClient = useQueryClient();
  const knownStreamIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!sessionId) return;

    let active = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let pollSequence = 0;

    const snapshot = (): RemoteAttachSnapshot => {
      const bucket = useChatStore.getState().sessions[sessionId];
      return {
        registryStreamId: getActiveStreamId(sessionId),
        registryGeneration: getActiveStreamGeneration(sessionId),
        bucketStreamId: bucket?.streamId ?? null,
        bucketGenerationStartedAt: bucket?.generationStartedAt ?? null,
      };
    };

    const poll = async () => {
      if (!active) return;
      const sequence = ++pollSequence;
      let nextDelay = POLL_INTERVAL;

      try {
        const jobs = await api.get<{ stream_id: string; session_id: string }[]>(
          API.CHAT.ACTIVE,
        );

        if (!active) return;

        const match = jobs.find((j) => j.session_id === sessionId);
        const chatState = useChatStore.getState();
        const bucket = chatState.sessions[sessionId];

        if (match) {
          const activeStreamId = getActiveStreamId(sessionId);
          if (!needsRemoteStreamAttach(match.stream_id, activeStreamId)) {
            // Update the hint only after the registry proves that this exact
            // stream is attached. A session-level active boolean can refer to
            // an older job that the backend has already replaced.
            knownStreamIdRef.current = match.stream_id;
          } else {
            const before = snapshot();
            await queryClient.invalidateQueries({
              queryKey: queryKeys.messages.list(sessionId),
            });
            if (!active) return;

            // The first /active response may have become stale while message
            // invalidation was in flight. Reconfirm backend authority, then
            // ensure neither the registry lease nor the chat bucket changed.
            const confirmedJobs = await api.get<{
              stream_id: string;
              session_id: string;
            }[]>(API.CHAT.ACTIVE);
            const confirmedMatch = confirmedJobs.find(
              (job) => job.session_id === sessionId,
            );
            const after = snapshot();
            if (!canCommitRemoteStreamAttach({
              pollSequence: sequence,
              currentPollSequence: pollSequence,
              expectedBackendStreamId: match.stream_id,
              confirmedBackendStreamId: confirmedMatch?.stream_id ?? null,
              before,
              after,
            })) {
              nextDelay = STALE_REPOLL_DELAY;
              return;
            }

            useChatStore.getState().startGeneration(sessionId, match.stream_id);
            await startStream(sessionId, match.stream_id);
            if (!active) return;
            if (getActiveStreamId(sessionId) === match.stream_id) {
              knownStreamIdRef.current = match.stream_id;
            }
          }
        } else {
          // No active generation server-side. If we were tracking one from a
          // remote client, refetch messages to pick up the final state.
          if (knownStreamIdRef.current) {
            knownStreamIdRef.current = null;
            if (!bucket?.isGenerating) {
              queryClient.invalidateQueries({
                queryKey: queryKeys.messages.list(sessionId),
              });
            }
          }
        }
      } catch {
        // ignore polling errors
      } finally {
        if (active && sequence === pollSequence) {
          timer = setTimeout(poll, nextDelay);
        }
      }
    };

    poll();

    return () => {
      active = false;
      pollSequence += 1;
      knownStreamIdRef.current = null;
      if (timer) clearTimeout(timer);
    };
  }, [sessionId, queryClient]);
}
