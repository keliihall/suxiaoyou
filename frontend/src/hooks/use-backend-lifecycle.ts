"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";
import {
  backendLifecycleReducer,
  INITIAL_DESKTOP_BACKEND_STATE,
  READY_WEB_BACKEND_STATE,
} from "@/lib/backend-lifecycle";
import { IS_DESKTOP, resetBackendToken, resetBackendUrl } from "@/lib/constants";
import { desktopAPI } from "@/lib/tauri-api";
import { errorToMessage } from "@/lib/errors";

export function useBackendLifecycle() {
  const [state, dispatch] = useReducer(
    backendLifecycleReducer,
    IS_DESKTOP ? INITIAL_DESKTOP_BACKEND_STATE : READY_WEB_BACKEND_STATE,
  );
  const mountedRef = useRef(true);
  const latestRevisionRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    if (!IS_DESKTOP) return;

    let unlisten: (() => void) | undefined;
    let disposed = false;

    const applyStatus = (status: Awaited<ReturnType<typeof desktopAPI.getBackendStatus>>) => {
      if (disposed) return;
      // Cache updates are side effects, so they must obey the same revision
      // ordering as the reducer. A stale bootstrap snapshot can arrive after
      // a newer event; applying its cache reset before the reducer rejects it
      // would leave a Ready UI pointing at no backend URL/token.
      if (status.revision <= latestRevisionRef.current) return;
      latestRevisionRef.current = status.revision;
      if (status.phase === "ready" && status.url) {
        // Populate the synchronous URL cache before the Ready render mounts
        // business queries and SSE consumers.
        resetBackendUrl(status.url);
      } else if (status.phase !== "ready") {
        resetBackendUrl();
        resetBackendToken();
      }
      dispatch({ type: "native-status", status });
    };

    const bootstrap = async () => {
      try {
        // Subscribe first, then read the snapshot. Revisions prevent a stale
        // snapshot response from overwriting a newer event.
        const stopListening = await desktopAPI.onBackendStatus(applyStatus);
        if (disposed) {
          stopListening();
          return;
        }
        unlisten = stopListening;
        const snapshot = await desktopAPI.getBackendStatus();
        applyStatus(snapshot);
      } catch (error) {
        if (disposed) return;
        dispatch({
          type: "bootstrap-failed",
          detail: errorToMessage(error, "Backend lifecycle is unavailable"),
        });
      }
    };

    void bootstrap();
    return () => {
      disposed = true;
      mountedRef.current = false;
      unlisten?.();
    };
  }, []);

  const relaunch = useCallback(async () => {
    if (state.status.phase !== "failed" || state.relaunching) return;
    dispatch({ type: "relaunch-requested" });
    try {
      await desktopAPI.relaunch();
    } catch (error) {
      if (!mountedRef.current) return;
      dispatch({
        type: "action-failed",
        detail: errorToMessage(error, "Failed to relaunch the application"),
      });
    }
  }, [state.relaunching, state.status.phase]);

  const openLogs = useCallback(async () => {
    dispatch({ type: "action-cleared" });
    try {
      await desktopAPI.openBackendLogs();
    } catch (error) {
      if (!mountedRef.current) return;
      dispatch({
        type: "action-failed",
        detail: errorToMessage(error, "Failed to open the log directory"),
      });
    }
  }, []);

  return { ...state, relaunch, openLogs };
}
