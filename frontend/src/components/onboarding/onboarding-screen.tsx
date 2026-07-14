"use client";

import { useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { useTranslation } from "react-i18next";
import { useState } from "react";
import { ArrowRight, FolderOpen, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AnimatedSuxiaoyouLogo } from "@/components/layout/splash-screen";
import { useSettingsStore } from "@/stores/settings-store";
import { browseDirectory } from "@/lib/upload";

export function OnboardingScreen() {
  const router = useRouter();
  const { t } = useTranslation("common");
  const completeOnboarding = useSettingsStore((s) => s.completeOnboarding);
  const workspaceDirectory = useSettingsStore((s) => s.workspaceDirectory);
  const setWorkspaceDirectory = useSettingsStore(
    (s) => s.setWorkspaceDirectory,
  );
  const [selectingWorkspace, setSelectingWorkspace] = useState(false);
  const [workspaceError, setWorkspaceError] = useState(false);
  const hasWorkspace = Boolean(
    workspaceDirectory?.trim() && workspaceDirectory.trim() !== ".",
  );

  const selectWorkspace = async () => {
    setSelectingWorkspace(true);
    setWorkspaceError(false);
    try {
      const path = await browseDirectory(t("onboardingSelectWorkspace"));
      if (path && path !== ".") {
        setWorkspaceDirectory(path);
      }
    } catch (error) {
      console.error("Failed to select the first workspace:", error);
      setWorkspaceError(true);
    } finally {
      setSelectingWorkspace(false);
    }
  };

  const openProviderSetup = () => {
    if (!hasWorkspace) return;
    completeOnboarding();
    router.push("/settings?tab=providers");
  };

  const startNow = () => {
    if (!hasWorkspace) return;
    completeOnboarding();
  };

  return (
    <div className="fixed inset-0 z-[9998] flex items-center justify-center overflow-y-auto bg-[var(--surface-primary)] py-6">
      <motion.div
        className="w-full max-w-sm px-6"
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: "easeOut" }}
      >
        <div className="flex flex-col items-center text-center">
          <AnimatedSuxiaoyouLogo size={80} />

          <h1 className="mt-8 text-2xl font-semibold text-[var(--text-primary)] tracking-tight">
            {t("onboardingWelcome")}
          </h1>
          <p className="mt-2 max-w-xs text-sm text-[var(--text-secondary)]">
            {t("onboardingDescription")}
          </p>

          <div className="mt-8 w-full space-y-3">
            <div className="rounded-xl border border-[var(--border-secondary)] bg-[var(--surface-secondary)] p-3 text-left">
              <p className="text-sm font-medium text-[var(--text-primary)]">
                {t("onboardingWorkspaceTitle")}
              </p>
              <p className="mt-1 text-xs text-[var(--text-secondary)]">
                {t("onboardingWorkspaceDescription")}
              </p>
              <Button
                variant="outline"
                className="mt-3 w-full justify-start"
                onClick={() => void selectWorkspace()}
                disabled={selectingWorkspace}
              >
                {selectingWorkspace ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <FolderOpen className="mr-2 h-4 w-4" />
                )}
                <span className="truncate">
                  {workspaceDirectory || t("onboardingSelectWorkspace")}
                </span>
              </Button>
              {workspaceError && (
                <p role="alert" className="mt-2 text-xs text-red-500">
                  {t("onboardingWorkspaceError")}
                </p>
              )}
            </div>

            <Button
              className="w-full"
              onClick={openProviderSetup}
              disabled={!hasWorkspace}
            >
              {t("onboardingConfigureProvider")}
              <ArrowRight className="ml-2 h-4 w-4" />
            </Button>
            <Button
              variant="outline"
              className="w-full"
              onClick={startNow}
              disabled={!hasWorkspace}
            >
              {t("onboardingStartNow")}
            </Button>
          </div>
        </div>
      </motion.div>
    </div>
  );
}
