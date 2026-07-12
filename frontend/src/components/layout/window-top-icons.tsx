"use client";

import Link from "next/link";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { FolderPlus, Loader2, PanelLeft, Search, SquarePen } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
  TooltipProvider,
} from "@/components/ui/tooltip";
import { useSidebarStore } from "@/stores/sidebar-store";
import { useIsMacOS } from "@/hooks/use-platform";
import { IS_DESKTOP, TITLE_BAR_HEIGHT } from "@/lib/constants";
import { browseDirectory } from "@/lib/upload";

/**
 * Floating icon strip anchored to the top-left of the window.
 *
 * Codex-style: panel-left and new-chat buttons live in the same fixed
 * position regardless of sidebar state — when the sidebar is open they
 * sit over the sidebar's top padding; when collapsed they sit at the
 * left of the chat area. Either way their x coordinate doesn't move.
 *
 * Approximate footprint (used by ChatHeader to reserve left padding):
 *   macOS:         ≈ 221px  (91 left inset + 4 × 28 buttons + gaps)
 *   Windows/Linux: ≈ 166px  (12 left pad + 4 × 36 buttons + gaps)
 */
export const WINDOW_TOP_ICONS_WIDTH_MAC = 221;
export const WINDOW_TOP_ICONS_WIDTH_OTHER = 166;

export function WindowTopIcons() {
  const { t } = useTranslation("common");
  const router = useRouter();
  const [isPickingDirectory, setIsPickingDirectory] = useState(false);
  const isMac = useIsMacOS();
  const isCollapsed = useSidebarStore((s) => s.isCollapsed);
  const toggle = useSidebarStore((s) => s.toggle);
  const setSearchModalOpen = useSidebarStore((s) => s.setSearchModalOpen);

  const handleAddProject = async () => {
    if (isPickingDirectory) return;
    setIsPickingDirectory(true);
    try {
      const path = await browseDirectory(t("addProject"));
      if (path) router.push(`/c/new?directory=${encodeURIComponent(path)}`);
    } catch (error) {
      console.error("Failed to pick project directory:", error);
      toast.error(t("addProjectFailed"));
    } finally {
      setIsPickingDirectory(false);
    }
  };

  if (!IS_DESKTOP) return null;

  // Keep these aligned with the native macOS traffic lights configured in
  // desktop-tauri/src-tauri/tauri.conf.json.
  const leftPad = isMac ? 91 : 12;
  const topOffset = isMac ? 16 : TITLE_BAR_HEIGHT + 16;

  return (
    <TooltipProvider delayDuration={200}>
      <div
        data-tauri-drag-region
        className="fixed left-0 z-40 flex items-start gap-1.5 h-10"
        style={{ paddingLeft: leftPad, top: topOffset }}
      >
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 rounded-lg text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--sidebar-hover)]"
              onClick={toggle}
              aria-label={t(isCollapsed ? "openSidebar" : "toggleSidebar")}
            >
              <PanelLeft className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">
            {t(isCollapsed ? "openSidebar" : "toggleSidebar")}
          </TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 rounded-lg text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--sidebar-hover)]"
              onClick={() => void handleAddProject()}
              disabled={isPickingDirectory}
              aria-label={t("addProject")}
              data-testid="window-add-project"
            >
              {isPickingDirectory ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <FolderPlus className="h-4 w-4" />
              )}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">{t("addProject")}</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 rounded-lg text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--sidebar-hover)]"
              onClick={() => setSearchModalOpen(true)}
              aria-label={t("searchChats")}
            >
              <Search className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">{t("searchChats")}</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 rounded-lg text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--sidebar-hover)]"
              aria-label={t("newChat")}
              asChild
            >
              <Link href="/c/new">
                <SquarePen className="h-4 w-4" />
              </Link>
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">{t("newChat")}</TooltipContent>
        </Tooltip>
      </div>
    </TooltipProvider>
  );
}
