"use client";

import { useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  ChevronDown,
  Code,
  Copy,
  Download,
  ExternalLink,
  Eye,
  FileArchive,
  FileSpreadsheet,
  FileText,
  FolderOpen,
  Globe,
  Image,
  Loader2,
  Presentation,
} from "lucide-react";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { API, IS_DESKTOP } from "@/lib/constants";
import { artifactTypeFromExtension, languageFromExtension } from "@/lib/artifacts";
import { base64ToBlob, base64ToUint8Array, downloadBlob } from "@/lib/browser-files";
import {
  getFileArtifactActionIds,
  type FileArtifactActionId,
} from "@/lib/file-artifact-actions";
import { isRemoteMode } from "@/lib/remote-connection";
import { usePlatform } from "@/hooks/use-platform";
import { useArtifactStore } from "@/stores/artifact-store";
import { useWorkspaceStore } from "@/stores/workspace-store";
import type { ArtifactType } from "@/types/artifact";
import type { ToolPart } from "@/types/message";

interface FileArtifactCardProps {
  data?: ToolPart;
  filePath?: string;
  title?: string;
  cardId?: string;
  compact?: boolean;
}

interface BinaryContentResponse {
  content_base64: string;
  name: string;
  mime_type: string;
  size: number;
}

const TYPE_CONFIG: Record<
  string,
  { icon: React.ComponentType<{ className?: string }>; label: string }
> = {
  html: { icon: Globe, label: "Page · HTML" },
  svg: { icon: Image, label: "Image · SVG" },
  markdown: { icon: FileText, label: "Document · MD" },
  docx: { icon: FileText, label: "Document · Word" },
  pdf: { icon: FileText, label: "Document · PDF" },
  pptx: { icon: Presentation, label: "Presentation · PPTX" },
  xlsx: { icon: FileSpreadsheet, label: "Spreadsheet · Excel" },
  csv: { icon: FileSpreadsheet, label: "Spreadsheet · CSV" },
  mermaid: { icon: Code, label: "Diagram · Mermaid" },
  react: { icon: Code, label: "Component · TSX" },
  code: { icon: Code, label: "Code" },
  file: { icon: FileArchive, label: "File" },
};

function basename(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

function titleWithoutExtension(name: string): string {
  return name.replace(/\.[^.]+$/, "");
}

function labelForFile(filePath: string, artifactType: ArtifactType | null): string {
  if (artifactType === "code") {
    const language = languageFromExtension(filePath);
    return language ? `Code · ${language.charAt(0).toUpperCase() + language.slice(1)}` : "Code";
  }
  return TYPE_CONFIG[artifactType ?? "file"]?.label ?? TYPE_CONFIG.file.label;
}

function artifactPanelType(filePath: string): ArtifactType {
  return artifactTypeFromExtension(filePath) ?? "file-preview";
}

export function FileArtifactCard({
  data,
  filePath: directFilePath,
  title: directTitle,
  cardId,
  compact = false,
}: FileArtifactCardProps) {
  const { t } = useTranslation("chat");
  const platform = usePlatform();
  const openArtifact = useArtifactStore((s) => s.openArtifact);
  const workspace = useWorkspaceStore((s) => s.activeWorkspacePath);
  const [busyAction, setBusyAction] = useState<FileArtifactActionId | null>(null);

  const input = (data?.state.input ?? {}) as Record<string, string | undefined>;
  const metadata = (data?.state.metadata ?? {}) as Record<string, string | undefined>;
  const filePath = directFilePath || metadata.file_path || input.file_path || "";
  const fileName = filePath ? basename(filePath) : "File";
  const title = directTitle || metadata.title || input.title || titleWithoutExtension(fileName);
  const isRunning = data?.state.status === "running" || data?.state.status === "pending";
  const isError = data?.state.status === "error";
  const isInteractive = Boolean(filePath) && !isRunning && !isError;
  const localDesktop = IS_DESKTOP && !isRemoteMode();
  const actionIds = getFileArtifactActionIds(localDesktop);

  const artifactType = useMemo(() => (filePath ? artifactTypeFromExtension(filePath) : null), [filePath]);
  const typeLabel = filePath ? labelForFile(filePath, artifactType) : "File";
  const config = TYPE_CONFIG[artifactType ?? "file"] ?? TYPE_CONFIG.file;
  const TypeIcon = config.icon;

  const handleOpen = useCallback(() => {
    if (!filePath || isRunning || isError) return;
    openArtifact({
      id: cardId || `present-${data?.call_id ?? filePath}`,
      type: artifactPanelType(filePath),
      title: title || fileName,
      content: "",
      language: languageFromExtension(filePath),
      filePath,
    });
  }, [cardId, data?.call_id, fileName, filePath, isError, isRunning, openArtifact, title]);

  const handleOpenDefault = useCallback(async () => {
    if (!filePath || !localDesktop || busyAction) return;
    setBusyAction("openDefault");
    try {
      await api.post(API.FILES.OPEN_SYSTEM, { path: filePath, workspace });
    } catch (error) {
      console.error("Failed to open file with the default application:", error);
      toast.error(t("fileOpenFailed"));
    } finally {
      setBusyAction(null);
    }
  }, [busyAction, filePath, localDesktop, t, workspace]);

  const handleReveal = useCallback(async () => {
    if (!filePath || !localDesktop || busyAction) return;
    setBusyAction("reveal");
    try {
      await api.post(API.FILES.REVEAL_SYSTEM, { path: filePath, workspace });
    } catch (error) {
      console.error("Failed to reveal file in the system file manager:", error);
      toast.error(t("fileRevealFailed"));
    } finally {
      setBusyAction(null);
    }
  }, [busyAction, filePath, localDesktop, t, workspace]);

  const handleCopyPath = useCallback(async () => {
    if (!filePath || !localDesktop || busyAction) return;
    setBusyAction("copyPath");
    try {
      await navigator.clipboard.writeText(filePath);
      toast.success(t("filePathCopied"));
    } catch (error) {
      console.error("Failed to copy file path:", error);
      toast.error(t("fileCopyPathFailed"));
    } finally {
      setBusyAction(null);
    }
  }, [busyAction, filePath, localDesktop, t]);

  const handleSaveCopy = useCallback(async () => {
    if (!filePath || busyAction) return;

    setBusyAction("saveCopy");
    try {
      const res = await api.post<BinaryContentResponse>(
        API.FILES.CONTENT_BINARY,
        { path: filePath, workspace },
        { timeoutMs: 120_000 },
      );

      if (localDesktop) {
        const { desktopAPI } = await import("@/lib/tauri-api");
        const saved = await desktopAPI.downloadAndSave({
          data: Array.from(base64ToUint8Array(res.content_base64)),
          defaultName: res.name || fileName,
        });
        if (saved) toast.success(t("fileSaved"));
      } else {
        downloadBlob(
          base64ToBlob(res.content_base64, res.mime_type),
          res.name || fileName,
        );
      }
    } catch (error) {
      console.error("Failed to save file:", error);
      toast.error(t("fileSaveFailed"));
    } finally {
      setBusyAction(null);
    }
  }, [busyAction, fileName, filePath, localDesktop, t, workspace]);

  const revealLabel = t(
    platform === "macos"
      ? "revealInFinder"
      : platform === "windows"
        ? "revealInExplorer"
        : "revealInFileManager",
  );

  const MenuItems = useCallback(
    ({
      Item,
      Separator,
    }: {
      Item: React.ComponentType<{
        onSelect?: (event: Event) => void;
        disabled?: boolean;
        children?: React.ReactNode;
      }>;
      Separator: React.ComponentType;
    }) => (
      <>
        <Item onSelect={handleOpen} disabled={!isInteractive}>
          <Eye />
          {t("previewFile")}
        </Item>
        {actionIds.includes("openDefault") && (
          <Item onSelect={() => void handleOpenDefault()} disabled={!isInteractive || busyAction !== null}>
            <ExternalLink />
            {t("openWithDefaultApp")}
          </Item>
        )}
        {actionIds.includes("reveal") && (
          <Item onSelect={() => void handleReveal()} disabled={!isInteractive || busyAction !== null}>
            <FolderOpen />
            {revealLabel}
          </Item>
        )}
        {actionIds.includes("copyPath") && (
          <Item onSelect={() => void handleCopyPath()} disabled={!isInteractive || busyAction !== null}>
            <Copy />
            {t("copyFilePath")}
          </Item>
        )}
        <Separator />
        <Item onSelect={() => void handleSaveCopy()} disabled={!isInteractive || busyAction !== null}>
          {busyAction === "saveCopy" ? <Loader2 className="animate-spin" /> : <Download />}
          {localDesktop ? t("saveAs") : t("download")}
        </Item>
      </>
    ),
    [
      actionIds,
      busyAction,
      handleCopyPath,
      handleOpen,
      handleOpenDefault,
      handleReveal,
      handleSaveCopy,
      isInteractive,
      localDesktop,
      revealLabel,
      t,
    ],
  );

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          data-testid="file-artifact-card"
          className={cn(
            "group flex w-full items-center gap-2 rounded-xl border px-2 py-2 text-left",
            "bg-[var(--surface-secondary)] transition-all duration-150",
            isInteractive && "hover:-translate-y-0.5 hover:bg-[var(--surface-tertiary)] hover:shadow-[var(--shadow-md)]",
            isError ? "border-[var(--color-destructive)]/30" : "border-[var(--border-default)]",
            compact && "min-h-[5.25rem]",
          )}
        >
          <button
            type="button"
            onClick={handleOpen}
            disabled={!isInteractive}
            aria-label={`${t("previewFile")} ${title || fileName}`}
            className={cn(
              "flex min-w-0 flex-1 items-center gap-3 rounded-lg px-2 py-1 text-left",
              "focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-primary)]",
              isInteractive && "cursor-pointer",
            )}
          >
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[var(--surface-tertiary)]">
              {isRunning ? (
                <Loader2 className="h-4 w-4 animate-spin text-[var(--text-tertiary)]" />
              ) : (
                <TypeIcon className="h-4 w-4 text-[var(--brand-primary)]" />
              )}
            </span>

            <span className="min-w-0 flex-1">
              <span
                className={cn(
                  "block truncate text-sm font-medium text-[var(--text-primary)]",
                  isRunning && "shimmer-text",
                )}
                title={title || fileName}
              >
                {title || fileName}
              </span>
              <span
                className="mt-0.5 block truncate text-xs text-[var(--text-tertiary)]"
                title={fileName}
              >
                {typeLabel}
              </span>
            </span>
          </button>

          {isInteractive && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  disabled={busyAction !== null}
                  aria-busy={busyAction !== null}
                  aria-label={localDesktop ? t("openWith") : t("fileActions")}
                  className={cn(
                    "flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg px-2.5 py-1.5 text-xs font-medium",
                    "bg-[var(--surface-tertiary)] text-[var(--text-secondary)] transition-colors",
                    "hover:bg-[var(--surface-primary)] hover:text-[var(--text-primary)]",
                    "focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-primary)]",
                    "disabled:cursor-wait disabled:opacity-60",
                  )}
                >
                  {busyAction ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : localDesktop ? (
                    <ExternalLink className="h-3.5 w-3.5" />
                  ) : (
                    <Download className="h-3.5 w-3.5" />
                  )}
                  <span>{localDesktop ? t("openWith") : t("fileActions")}</span>
                  <ChevronDown className="h-3.5 w-3.5" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-56">
                <MenuItems Item={DropdownMenuItem} Separator={DropdownMenuSeparator} />
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent className="w-56">
        <MenuItems Item={ContextMenuItem} Separator={ContextMenuSeparator} />
      </ContextMenuContent>
    </ContextMenu>
  );
}
