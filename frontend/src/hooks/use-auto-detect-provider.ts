"use client";

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  useSettingsHasHydrated,
  useSettingsStore,
} from "@/stores/settings-store";
import { api } from "@/lib/api";
import { API, queryKeys } from "@/lib/constants";
import type { ProviderInfo, LocalProviderStatus } from "@/types/usage";

interface OllamaRuntimeStatus {
  binary_installed: boolean;
  running: boolean;
}

interface RapidMLXRuntimeStatus {
  running: boolean;
}

/**
 * Auto-detect and set activeProvider when it is null.
 * Should be called at the layout level so it runs regardless of which page the user visits.
 */
export function useAutoDetectProvider(): { hasProvider: boolean } {
  const activeProvider = useSettingsStore((s) => s.activeProvider);
  const setActiveProvider = useSettingsStore((s) => s.setActiveProvider);
  const settingsHydrated = useSettingsHasHydrated();

  const { data: providers } = useQuery({
    queryKey: queryKeys.providers,
    queryFn: () => api.get<ProviderInfo[]>(API.CONFIG.PROVIDERS),
  });

  const { data: localStatus } = useQuery({
    queryKey: queryKeys.localProvider,
    queryFn: () => api.get<LocalProviderStatus>(API.CONFIG.LOCAL_PROVIDER),
  });

  const { data: ollamaRuntimeStatus } = useQuery({
    queryKey: ["ollamaRuntime"],
    queryFn: () => api.get<OllamaRuntimeStatus>(API.OLLAMA.STATUS),
    refetchInterval: activeProvider === null ? 10_000 : false,
  });

  const { data: rapidMlxRuntimeStatus } = useQuery({
    queryKey: ["rapidMlxRuntime"],
    queryFn: () => api.get<RapidMLXRuntimeStatus>(API.RAPID_MLX.STATUS),
    refetchInterval: activeProvider === null ? 10_000 : false,
    retry: false,
  });

  const ollamaConnected = !!ollamaRuntimeStatus?.running;
  const rapidMlxConnected = !!rapidMlxRuntimeStatus?.running;
  const hasConfiguredByokProvider = (providers ?? []).some(
    (p) => p.is_configured && !p.id.startsWith("custom_"),
  );
  const hasCustomEndpoint = (providers ?? []).some(
    (p) => p.is_configured && p.id.startsWith("custom_"),
  );
  const customEndpointConnected =
    !!localStatus?.is_connected || hasCustomEndpoint;

  useEffect(() => {
    if (!settingsHydrated) return;
    if (activeProvider === "chatgpt") {
      setActiveProvider(null);
      return;
    }
    if (activeProvider !== null) return;
    if (customEndpointConnected) setActiveProvider("custom");
    else if (hasConfiguredByokProvider) setActiveProvider("byok");
    else if (rapidMlxConnected) setActiveProvider("rapid-mlx");
    else if (ollamaConnected) setActiveProvider("ollama");
  }, [
    activeProvider,
    hasConfiguredByokProvider,
    customEndpointConnected,
    rapidMlxConnected,
    ollamaConnected,
    setActiveProvider,
    settingsHydrated,
  ]);

  return { hasProvider: settingsHydrated && activeProvider !== null };
}
