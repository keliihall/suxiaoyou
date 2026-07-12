"use client";

import { useState, useCallback } from "react";
import { useTranslation } from 'react-i18next';
import { useRouter } from "next/navigation";
import { Share2, Loader2, List, MoreHorizontal, PanelRightClose, PanelRightOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";
import { useSidebarStore } from "@/stores/sidebar-store";
import { useChatSession } from "@/stores/chat-store";
import { useWorkspaceStore } from "@/stores/workspace-store";
import { useActivityStore } from "@/stores/activity-store";
import { useArtifactStore } from "@/stores/artifact-store";
import { usePlanReviewStore } from "@/stores/plan-review-store";
import { useMessages } from "@/hooks/use-messages";
import { useIsMacOS } from "@/hooks/use-platform";
import {
  WINDOW_TOP_ICONS_WIDTH_MAC,
  WINDOW_TOP_ICONS_WIDTH_OTHER,
} from "@/components/layout/window-top-icons";
import { apiFetch } from "@/lib/api";
import { API, IS_DESKTOP } from "@/lib/constants";
import { isRemoteMode } from "@/lib/remote-connection";
import { downloadBlob } from "@/lib/browser-files";

interface ChatHeaderProps {
  sessionId?: string;
  title?: string | null;
}

export function ChatHeader({ sessionId, title }: ChatHeaderProps) {
  const { t } = useTranslation(['chat', 'common']);
  const router = useRouter();
  const isCollapsed = useSidebarStore((s) => s.isCollapsed);
  const { messages } = useMessages(sessionId);
  const isMac = useIsMacOS();
  const remote = isRemoteMode();
  // When sidebar is collapsed on desktop, the floating WindowTopIcons sit
  // over the left edge of the chat area — reserve space so our own content
  // doesn't hide beneath them. Remote mode has its own leading List button
  // inside the header and doesn't show WindowTopIcons.
  const reservesTopIconsSpace = IS_DESKTOP && isCollapsed && !remote;
  const leftPad = reservesTopIconsSpace
    ? isMac
      ? WINDOW_TOP_ICONS_WIDTH_MAC
      : WINDOW_TOP_ICONS_WIDTH_OTHER
    : 12;
  const macDragProps = IS_DESKTOP && isMac ? { "data-tauri-drag-region": "" } : {};
  const [pdfLoading, setPdfLoading] = useState(false);
  const workspaceIsOpen = useWorkspaceStore((s) => s.isOpen);
  const openWorkspace = useWorkspaceStore((s) => s.open);
  const closeWorkspace = useWorkspaceStore((s) => s.close);
  const activityIsOpen = useActivityStore((s) => s.isOpen);
  const artifactIsOpen = useArtifactStore((s) => s.isOpen);
  const planReviewIsOpen = usePlanReviewStore((s) => s.isOpen);
  // Workspace is only "actually visible" when no overlay panel covers it.
  const workspaceVisible =
    workspaceIsOpen && !activityIsOpen && !artifactIsOpen && !planReviewIsOpen;
  const handleToggleWorkspace = useCallback(() => {
    if (workspaceVisible) {
      closeWorkspace();
    } else {
      // openWorkspace() also closes any active overlay panel.
      openWorkspace();
    }
  }, [workspaceVisible, openWorkspace, closeWorkspace]);
  const session = useChatSession(sessionId ?? null);
  const isGenerating = session.isGenerating;
  const streamingParts = session.streamingParts;

  // Derive stream status label for remote mode
  const streamStatus = (() => {
    if (!remote || !isGenerating) return null;
    if (streamingParts.length === 0) return "Starting...";
    const lastPart = streamingParts[streamingParts.length - 1];
    if (lastPart.type === "tool" && lastPart.state.status === "running") return "Using tools...";
    return "Generating...";
  })();

  const handleExportPdf = useCallback(async () => {
    if (!sessionId) return;
    setPdfLoading(true);
    try {
      const res = await apiFetch(API.SESSIONS.EXPORT_PDF(sessionId), { timeoutMs: 120_000 });

      if (IS_DESKTOP) {
        if (!res.ok) {
          const errorText = await res.text();
          throw new Error(errorText || "Export failed");
        }
        // WebView2 does not support blob-URL downloads via <a>.click(),
        // so use a Tauri command with native save dialog instead.
        const { desktopAPI } = await import("@/lib/tauri-api");
        const bytes = Array.from(new Uint8Array(await res.arrayBuffer()));
        await desktopAPI.downloadAndSave({ data: bytes, defaultName: "conversation.pdf" });
      } else {
        if (!res.ok) {
          const errorText = await res.text();
          let errorDetail = errorText;
          try {
            const errorJson = JSON.parse(errorText);
            errorDetail = errorJson.detail || errorText;
          } catch {
            // Not JSON, use text as-is
          }
          console.error("PDF export failed:", {
            status: res.status,
            statusText: res.statusText,
            detail: errorDetail
          });
          throw new Error(`PDF export failed: ${errorDetail}`);
        }

        let filename = "conversation.pdf";
        const disposition = res.headers.get("Content-Disposition");
        if (disposition) {
          const utf8Match = disposition.match(/filename\*=UTF-8''(.+?)(?:;|$)/);
          if (utf8Match) {
            filename = decodeURIComponent(utf8Match[1]);
          } else {
            const asciiMatch = disposition.match(/filename="(.+?)"/);
            if (asciiMatch) filename = asciiMatch[1];
          }
        }
        downloadBlob(await res.blob(), filename);
      }
    } catch (err) {
      console.error("PDF export error:", err);
    } finally {
      setPdfLoading(false);
    }
  }, [sessionId]);

  return (
    <TooltipProvider delayDuration={200}>
      <header
        className="relative z-10 flex h-13 items-center gap-1 pr-3 backdrop-blur-sm"
        style={{ paddingLeft: leftPad }}
      >
        {/* Remote mode: task list button */}
        {remote && (
          <Button
            variant="ghost"
            size="icon"
            className="h-9 w-9"
            onClick={() => router.push("/m")}
            aria-label="Task list"
          >
            <List className="h-[18px] w-[18px]" />
          </Button>
        )}

        {/* Sidebar toggle + new chat live in the global WindowTopIcons bar
            (desktop non-remote) so they stay at the window's left edge across
            sidebar states. */}

        <div className="flex min-w-0 max-w-[min(360px,42vw)] items-center gap-1">
          <span
            className="truncate px-2 text-[13px] font-semibold text-[var(--text-primary)]"
            title={title || t("common:newChat")}
          >
            {title || t("common:newChat")}
          </span>
          {!remote && sessionId && messages && messages.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7 shrink-0 rounded-lg"
                  aria-label={t("chat:conversationMenu")}
                >
                  <MoreHorizontal className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="w-44">
                <DropdownMenuItem
                  onSelect={() => void handleExportPdf()}
                  disabled={pdfLoading}
                >
                  {pdfLoading ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Share2 className="h-4 w-4" />
                  )}
                  {t("chat:export")}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>

        <div
          {...macDragProps}
          className="min-w-6 flex-1 self-stretch"
          aria-hidden="true"
        />

        {/* Remote mode: stream status, or task list button */}
        {remote && streamStatus && (
          <span className="text-[12px] text-[var(--text-tertiary)] animate-pulse whitespace-nowrap">
            {streamStatus}
          </span>
        )}

        {!remote && sessionId && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-9 w-9"
                aria-label={workspaceVisible ? t("hideWorkspace") : t("showWorkspace")}
                onClick={handleToggleWorkspace}
              >
                {workspaceVisible ? (
                  <PanelRightClose className="h-[18px] w-[18px]" />
                ) : (
                  <PanelRightOpen className="h-[18px] w-[18px]" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              {workspaceVisible ? t("hideWorkspace") : t("showWorkspace")}
            </TooltipContent>
          </Tooltip>
        )}
      </header>
    </TooltipProvider>
  );
}
