"use client";

import { Suspense, useEffect } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ChatView } from "@/components/chat/chat-view";
import { api } from "@/lib/api";
import { API } from "@/lib/constants";
import { isRemoteMode } from "@/lib/remote-connection";
import { useChatStore } from "@/stores/chat-store";
import {
  isKnownTerminalStream,
  isStreamActive,
  startStream,
} from "@/lib/session-stream-registry";

function TaskClientInner({ sessionId }: { sessionId: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  useEffect(() => {
    if (!isRemoteMode()) {
      router.replace("/m/settings");
    }
  }, [router]);

  const resolvedId = sessionId === "_"
    ? searchParams.get("sessionId") ?? ""
    : sessionId;

  // If navigated with a stream_id (e.g., from new task page), seed the
  // chatStore and attach the stream immediately so we don't wait for the
  // periodic /chat/active poll to discover it.
  const streamIdParam = searchParams.get("stream_id");
  useEffect(() => {
    if (!resolvedId || !streamIdParam) return;

    let cancelled = false;
    const consumeStreamId = () => {
      const next = new URLSearchParams(searchParams.toString());
      next.delete("stream_id");
      const query = next.toString();
      router.replace(query ? `${pathname}?${query}` : pathname);
    };

    if (isKnownTerminalStream(resolvedId, streamIdParam)) {
      consumeStreamId();
      return;
    }

    void api
      .get<Array<{ stream_id: string; session_id: string }>>(API.CHAT.ACTIVE)
      .then((jobs) => {
        if (
          cancelled ||
          !jobs.some(
            (job) =>
              job.session_id === resolvedId &&
              job.stream_id === streamIdParam,
          ) ||
          isKnownTerminalStream(resolvedId, streamIdParam)
        ) {
          return;
        }
        const chatState = useChatStore.getState();
        const bucket = chatState.sessions[resolvedId];
        if (bucket?.streamId !== streamIdParam) {
          chatState.startGeneration(resolvedId, streamIdParam);
        }
        if (!isStreamActive(resolvedId)) {
          void startStream(resolvedId, streamIdParam);
        }
      })
      .catch(() => {
        // The periodic remote-generation sync remains the authority if this
        // one-time navigation hint cannot be verified.
      })
      .finally(() => {
        if (!cancelled) consumeStreamId();
      });

    return () => {
      cancelled = true;
    };
  }, [pathname, resolvedId, router, searchParams, streamIdParam]);

  if (!resolvedId) return null;

  // No custom header — ChatHeader inside ChatView handles everything.
  // In remote mode, ChatHeader shows back button instead of sidebar toggle.
  return (
    <div className="flex flex-col h-full pt-[env(safe-area-inset-top)]">
      <ChatView sessionId={resolvedId} />
    </div>
  );
}

export function MobileTaskClient({ sessionId }: { sessionId: string }) {
  return (
    <Suspense fallback={null}>
      <TaskClientInner sessionId={sessionId} />
    </Suspense>
  );
}
