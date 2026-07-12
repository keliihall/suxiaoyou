"use client";

import { useCallback } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { useIsMacOS } from "@/hooks/use-platform";
import { IS_DESKTOP, TITLE_BAR_HEIGHT } from "@/lib/constants";
import { useSidebarStore } from "@/stores/sidebar-store";
import { SidebarResizeHandle } from "@/components/layout/sidebar-resize-handle";
import { SETTINGS_TABS, type SettingsTabId } from "./settings-tabs";

interface SettingsSidebarProps {
  returnHref?: string;
}

export function SettingsSidebar({
  returnHref = "/c/new",
}: SettingsSidebarProps) {
  const { t } = useTranslation(["settings"]);
  const router = useRouter();
  const searchParams = useSearchParams();
  const activeTab = (searchParams.get("tab") as SettingsTabId) || "general";
  const isMac = useIsMacOS();
  const sidebarWidth = useSidebarStore((s) => s.width);

  const navigateTab = useCallback(
    (tab: string) => {
      router.replace(`/settings?tab=${tab}`, { scroll: false });
    },
    [router],
  );

  const topOffset = IS_DESKTOP && !isMac ? TITLE_BAR_HEIGHT : 0;

  return (
    <aside
      aria-label="Settings sidebar"
      className="sidebar-glass fixed inset-y-0 left-0 z-30 flex flex-col overflow-hidden bg-[var(--sidebar-translucent-bg)] backdrop-blur-xl"
      style={{ width: sidebarWidth, top: topOffset }}
    >
      <SidebarResizeHandle />
      <div
        data-tauri-drag-region
        data-testid="settings-drag-region"
        aria-hidden="true"
        className="shrink-0"
        style={{ height: IS_DESKTOP && isMac ? 48 : IS_DESKTOP ? 0 : 12 }}
      />

      <div className="shrink-0 px-2 pb-2">
        <Link
          href={returnHref}
          data-testid="settings-back-to-app"
          className="flex min-h-11 w-full items-center gap-2 rounded-lg px-3 text-ui-body text-[var(--text-secondary)] outline-none transition-colors hover:bg-[var(--sidebar-hover)] hover:text-[var(--text-primary)] focus-visible:bg-[var(--sidebar-hover)] focus-visible:text-[var(--text-primary)] focus-visible:ring-2 focus-visible:ring-[var(--ring)] focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--sidebar-translucent-bg)]"
        >
          <ArrowLeft className="h-4 w-4 shrink-0" />
          <span className="truncate">{t("settings:backToApp")}</span>
        </Link>
      </div>

      <nav className="px-2 space-y-0.5 overflow-y-auto scrollbar-auto">
        {SETTINGS_TABS.map(({ id, icon: Icon, labelKey }) => (
          <button
            key={id}
            onClick={() => navigateTab(id)}
            className={cn(
              "flex w-full items-center gap-3 rounded-lg px-3 py-2 text-ui-body transition-colors",
              activeTab === id
                ? "bg-[var(--sidebar-hover)] text-[var(--text-primary)] font-medium"
                : "text-[var(--text-secondary)] hover:bg-[var(--sidebar-hover)] hover:text-[var(--text-primary)]",
            )}
          >
            <Icon className="h-[16px] w-[16px] shrink-0" />
            <span className="truncate">{t(`settings:${labelKey}`)}</span>
          </button>
        ))}
      </nav>
    </aside>
  );
}
