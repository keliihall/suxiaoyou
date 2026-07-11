"use client";

import { type ReactNode, useCallback, useEffect, useState } from "react";
import { MotionConfig } from "framer-motion";
import { ThemeProvider } from "./theme-provider";
import { QueryProvider } from "./query-provider";
import { StreamRegistryHydration } from "./stream-registry-hydration";
import { ErrorBoundary } from "@/components/ui/error-boundary";
import { Toaster } from "sonner";
import { AppearanceInjector } from "@/components/layout/appearance-injector";
import { LocalizedContextMenuGuard } from "@/components/providers/localized-context-menu-guard";
import { BackendStatusScreen } from "@/components/desktop/backend-status-screen";
import { TitleBar } from "@/components/desktop/title-bar";
import { getClientLanguagePreference } from "@/i18n/config";
import { useBackendLifecycle } from "@/hooks/use-backend-lifecycle";
import { IS_DESKTOP } from "@/lib/constants";
import type { BackendStatus } from "@/lib/backend-lifecycle";
import { useTranslation } from "react-i18next";

function LanguageSync({ onReady }: { onReady: () => void }) {
  const { i18n } = useTranslation();

  useEffect(() => {
    let mounted = true;
    const handler = (lng: string) => {
      document.documentElement.lang = lng;
    };
    i18n.on("languageChanged", handler);

    const applyLanguage = async () => {
      try {
        const preferredLanguage = getClientLanguagePreference();
        if (i18n.language !== preferredLanguage) {
          await i18n.changeLanguage(preferredLanguage);
        }
      } catch (error) {
        // Language loading must never leave the entire application blank.
        console.error("[LanguageSync] Failed to apply language", error);
      } finally {
        if (!mounted) return;
        document.documentElement.lang = i18n.resolvedLanguage || i18n.language || "en";
        onReady();
      }
    };
    void applyLanguage();

    return () => {
      mounted = false;
      i18n.off("languageChanged", handler);
    };
  }, [i18n, onReady]);

  return null;
}

export function AppProviders({ children }: { children: ReactNode }) {
  const backend = useBackendLifecycle();
  const [languageReady, setLanguageReady] = useState(false);
  const [hasEverBeenReady, setHasEverBeenReady] = useState(false);
  const [runtimeReady, setRuntimeReady] = useState(false);
  const handleLanguageReady = useCallback(() => setLanguageReady(true), []);

  useEffect(() => setRuntimeReady(true), []);

  useEffect(() => {
    if (backend.status.phase === "ready") setHasEverBeenReady(true);
  }, [backend.status.phase]);

  const shouldMountApplication =
    languageReady && (hasEverBeenReady || backend.status.phase === "ready");
  const visibleStatus: BackendStatus =
    !languageReady && backend.status.phase === "ready"
      ? { phase: "initializing", revision: backend.status.revision }
      : backend.status;
  const showDesktopLifecycle = runtimeReady && IS_DESKTOP;
  const recoveryActive =
    showDesktopLifecycle && visibleStatus.phase !== "ready";

  return (
    <MotionConfig reducedMotion="user">
      <ThemeProvider>
        <LanguageSync onReady={handleLanguageReady} />
        {showDesktopLifecycle && <TitleBar recoveryActive={recoveryActive} />}
        {shouldMountApplication && (
          <div
            className="h-full"
            inert={recoveryActive ? true : undefined}
            aria-hidden={recoveryActive ? true : undefined}
          >
            <QueryProvider>
              <AppearanceInjector />
              <StreamRegistryHydration />
              <ErrorBoundary>{children}</ErrorBoundary>
              <LocalizedContextMenuGuard />
              <Toaster
                position="top-right"
                richColors
                closeButton
                toastOptions={{
                  style: {
                    background: "var(--surface-secondary)",
                    color: "var(--text-primary)",
                    border: "1px solid var(--border-default)",
                  },
                }}
              />
            </QueryProvider>
          </div>
        )}
        {showDesktopLifecycle && (
          <BackendStatusScreen
            status={visibleStatus}
            relaunching={backend.relaunching}
            actionError={backend.actionError}
            onRelaunch={backend.relaunch}
            onOpenLogs={backend.openLogs}
          />
        )}
      </ThemeProvider>
    </MotionConfig>
  );
}
