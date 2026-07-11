"use client";

import { useState, useRef, useEffect, useCallback, memo } from "react";
import { useTranslation } from 'react-i18next';
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Archive, EllipsisVertical, Loader2, MessageCircle, Pin, PinOff } from "lucide-react";
import { cn, formatRelativeTime } from "@/lib/utils";
import { api } from "@/lib/api";
import { API, IS_DESKTOP, queryKeys } from "@/lib/constants";
import { isRemoteMode } from "@/lib/remote-connection";
import { getChatRoute } from "@/lib/routes";
import { useDebouncedPrefetch } from "@/hooks/use-debounced-prefetch";
import { usePlatform } from "@/hooks/use-platform";
import { useChatSession } from "@/stores/chat-store";
import type { PaginatedMessages } from "@/types/message";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
  ContextMenuSeparator,
} from "@/components/ui/context-menu";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import type { SessionResponse } from "@/types/session";

interface SessionItemProps {
  session: SessionResponse;
  isActive?: boolean;
  onDelete: (id: string, title: string) => void;
  onRename: (id: string, newTitle: string) => void;
  onExportPdf?: (id: string, title: string) => void;
  onExportMarkdown?: (id: string, title: string) => void;
  onTogglePin?: (id: string, pinned: boolean) => void;
  onArchive?: (id: string) => void;
  isEditing?: boolean;
  onEditStart?: (id: string) => void;
  onEditEnd?: () => void;
  snippet?: string;
  isFocused?: boolean;
}

export const SessionItem = memo(function SessionItem({
  session,
  isActive = false,
  onDelete,
  onRename,
  onExportPdf,
  onExportMarkdown,
  onTogglePin,
  onArchive,
  isEditing = false,
  onEditStart,
  onEditEnd,
  snippet,
  isFocused = false,
}: SessionItemProps) {
  const { t } = useTranslation('common');
  const platform = usePlatform();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { prefetch, cancel } = useDebouncedPrefetch(150);
  const [editValue, setEditValue] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [scrollVars, setScrollVars] = useState<React.CSSProperties | undefined>();
  const inputRef = useRef<HTMLInputElement>(null);
  const itemRef = useRef<HTMLDivElement>(null);
  const titleRef = useRef<HTMLParagraphElement>(null);
  const clickTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const rawTitle = session.title || t('newConversation');
  // Clean up ugly channel titles: "Channel: whatsapp:+1234567890" → "+1234567890"
  const title = rawTitle.startsWith("Channel: ")
    ? rawTitle.slice(9).replace(/^(feishu|weixin|wechat|dingtalk|wecom|qq):/, "")
    : rawTitle;
  const relativeTime = formatRelativeTime(session.time_updated);
  const channelBadge = session.slug ? getChannelBadge(session.slug) : null;
  // Live status badge: shows whenever this session has an in-flight stream
  // attached, including ones the user navigated away from.
  const liveBucket = useChatSession(session.id);
  const isLive = liveBucket.isGenerating || liveBucket.isCompacting;
  const hasExplicitDirectory =
    !!session.directory && session.directory !== ".";
  // Opening a path is a local desktop action. In remote mode the API points to
  // another machine, where launching Finder/Explorer would be both surprising
  // and useless to the current user.
  const openDirectory = IS_DESKTOP && !isRemoteMode()
    ? hasExplicitDirectory
      ? session.directory
      : "."
    : null;
  const deeplink = `suxiaoyou://chat?sessionId=${encodeURIComponent(session.id)}`;
  const pinLabel = session.is_pinned
    ? t('unpin', { defaultValue: 'Unpin' })
    : t('pin', { defaultValue: 'Pin' });
  const openDirectoryLabel = t(
    platform === "macos"
      ? "openInFinder"
      : platform === "windows"
        ? "openInExplorer"
        : "openInFileManager",
  );

  // Focus the item when it receives roving tabindex focus
  useEffect(() => {
    if (isFocused && !isEditing && itemRef.current) {
      itemRef.current.focus();
    }
  }, [isFocused, isEditing]);

  // Focus and select input when entering edit mode
  useEffect(() => {
    if (isEditing && inputRef.current) {
      setEditValue(title);
      requestAnimationFrame(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      });
    }
  }, [isEditing, title]);

  // Measure title overflow once on mount / title change
  useEffect(() => {
    const el = titleRef.current;
    if (!el) return;
    if (el.scrollWidth > el.clientWidth) {
      const dist = el.scrollWidth - el.clientWidth;
      setScrollVars({
        '--scroll-distance': `-${dist}px`,
        '--scroll-duration': `${dist / 50}s`,
      } as React.CSSProperties);
    } else {
      setScrollVars(undefined);
    }
  }, [title]);

  const handleSubmitRename = useCallback(() => {
    const trimmed = editValue.trim();
    if (trimmed && trimmed !== title) {
      onRename(session.id, trimmed);
    }
    onEditEnd?.();
  }, [editValue, title, session.id, onRename, onEditEnd]);

  const handleCancelRename = useCallback(() => {
    setEditValue(title);
    onEditEnd?.();
  }, [title, onEditEnd]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleSubmitRename();
      } else if (e.key === "Escape") {
        e.preventDefault();
        handleCancelRename();
      }
    },
    [handleSubmitRename, handleCancelRename],
  );

  // Defer single-click navigation briefly so a double-click (which opens
  // rename) doesn't also navigate into the session first. A double-click
  // cancels the pending navigation. Keyboard Enter stays immediate.
  const handleRowClick = useCallback(() => {
    if (isEditing) return;
    if (clickTimerRef.current) clearTimeout(clickTimerRef.current);
    clickTimerRef.current = setTimeout(() => {
      clickTimerRef.current = null;
      router.push(getChatRoute(session.id));
    }, 250);
  }, [isEditing, router, session.id]);

  const handleRowDoubleClick = useCallback(() => {
    if (clickTimerRef.current) {
      clearTimeout(clickTimerRef.current);
      clickTimerRef.current = null;
    }
    if (!isEditing) onEditStart?.(session.id);
  }, [isEditing, onEditStart, session.id]);

  // Cancel a pending navigation if the row unmounts mid-debounce.
  useEffect(
    () => () => {
      if (clickTimerRef.current) clearTimeout(clickTimerRef.current);
    },
    [],
  );

  const handleOpenDirectory = useCallback(async () => {
    if (!openDirectory) return;
    try {
      await api.post(API.FILES.OPEN_SYSTEM, { path: openDirectory });
    } catch (err) {
      console.error("Failed to open directory:", err);
      toast.error(
        t("openFolderFailed", { defaultValue: "Failed to open folder" }),
      );
    }
  }, [openDirectory, t]);

  const handleCopy = useCallback(async (value: string, labelKey: string) => {
    try {
      await navigator.clipboard.writeText(value);
      toast.success(t(labelKey, { defaultValue: "Copied" }));
    } catch (err) {
      console.error("Failed to copy:", err);
      toast.error(t("copyFailed", { defaultValue: "Copy failed" }));
    }
  }, [t]);

  // Shared action list, rendered into both the right-click ContextMenu and the
  // hover ••• DropdownMenu. Typed structurally so either menu family's Item /
  // Separator components satisfy it.
  const MenuItems = useCallback(
    ({
      Item,
      Separator,
    }: {
      Item: React.ComponentType<{
        onSelect?: (event: Event) => void;
        className?: string;
        children?: React.ReactNode;
      }>;
      Separator: React.ComponentType;
    }) => (
      <>
        <Item onSelect={() => onTogglePin?.(session.id, !session.is_pinned)}>
          {session.is_pinned
            ? t('unpin', { defaultValue: 'Unpin' })
            : t('pin', { defaultValue: 'Pin' })}
        </Item>
        <Item onSelect={() => onEditStart?.(session.id)}>
          {t('rename')}
        </Item>
        <Item onSelect={() => onArchive?.(session.id)}>
          {t('archiveChat')}
        </Item>
        <Separator />
        {openDirectory && (
          <Item onSelect={handleOpenDirectory}>
            {openDirectoryLabel}
          </Item>
        )}
        {hasExplicitDirectory && session.directory && (
          <Item
            onSelect={() =>
              handleCopy(session.directory!, "workingDirectoryCopied")
            }
          >
            {t('copyWorkingDirectory')}
          </Item>
        )}
        <Item onSelect={() => handleCopy(session.id, "sessionIdCopied")}>
          {t('copySessionId')}
        </Item>
        <Item onSelect={() => handleCopy(deeplink, "deeplinkCopied")}>
          {t('copyDeeplink')}
        </Item>
        <Separator />
        <Item onSelect={() => onExportPdf?.(session.id, title)}>
          {t('exportPdf')}
        </Item>
        <Item onSelect={() => onExportMarkdown?.(session.id, title)}>
          {t('exportMarkdown', { defaultValue: 'Export Markdown' })}
        </Item>
        <Separator />
        <Item
          onSelect={() => onDelete(session.id, title)}
          className="text-[var(--color-destructive)] focus:text-[var(--color-destructive)]"
        >
          {t('delete')}
        </Item>
      </>
    ),
    [
      deeplink,
      handleCopy,
      handleOpenDirectory,
      hasExplicitDirectory,
      openDirectory,
      openDirectoryLabel,
      onArchive,
      onDelete,
      onEditStart,
      onExportMarkdown,
      onExportPdf,
      onTogglePin,
      session.directory,
      session.id,
      session.is_pinned,
      t,
      title,
    ],
  );

  return (
    <ContextMenu onOpenChange={setMenuOpen}>
      <ContextMenuTrigger asChild>
        <div
          ref={itemRef}
          role="option"
          aria-selected={isActive}
          tabIndex={isFocused ? 0 : -1}
          onClick={handleRowClick}
          onDoubleClick={handleRowDoubleClick}
          onKeyDown={(e) => !isEditing && e.key === "Enter" && router.push(getChatRoute(session.id))}
          onMouseEnter={() => {
            prefetch(() => {
              const isCached = queryClient.getQueryData(queryKeys.messages.list(session.id));
              if (!isCached) {
                queryClient.prefetchInfiniteQuery({
                  queryKey: queryKeys.messages.list(session.id),
                  queryFn: () => api.get<PaginatedMessages>(API.MESSAGES.LIST(session.id, 50, -1)),
                  initialPageParam: -1,
                  staleTime: 60_000,
                });
              }
            });
          }}
          onMouseLeave={() => {
            cancel();
          }}
          className={cn(
            "group/session relative mx-3 flex cursor-pointer items-center gap-2 overflow-hidden rounded-lg text-sm transition-colors duration-150 ease-out",
            "pl-9 pr-2",
            snippet ? "min-h-11 py-1.5" : "h-7 py-1",
            isActive
              ? "bg-[var(--sidebar-active)] text-[var(--text-primary)] shadow-[var(--sidebar-active-shadow)]"
              : "text-[var(--text-primary)] hover:bg-[var(--sidebar-hover)] focus-within:bg-[var(--sidebar-active)] focus-within:shadow-[var(--sidebar-active-shadow)] data-[state=open]:bg-[var(--sidebar-active)] data-[state=open]:shadow-[var(--sidebar-active-shadow)]",
            (menuOpen || dropdownOpen) && "bg-[var(--sidebar-active)] shadow-[var(--sidebar-active-shadow)]",
            isEditing && "ring-1 ring-[var(--brand-primary)]",
          )}
        >
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              e.currentTarget.blur();
              onTogglePin?.(session.id, !session.is_pinned);
            }}
            className={cn(
              "absolute left-1.5 top-1/2 z-10 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md text-[var(--text-tertiary)] opacity-0 transition-opacity hover:bg-[var(--surface-tertiary)] hover:text-[var(--text-primary)] focus:outline-none focus-visible:ring-1 focus-visible:ring-[var(--brand-primary)] focus-visible:opacity-100 group-hover/session:opacity-100",
              isEditing && "hidden",
            )}
            aria-label={pinLabel}
            title={pinLabel}
          >
            {session.is_pinned ? <PinOff className="h-3.5 w-3.5" /> : <Pin className="h-3.5 w-3.5" />}
          </button>

          <div
            className={cn(
              "min-w-0 flex-1",
              !isEditing && "pr-16",
            )}
          >
            {isEditing ? (
              <input
                ref={inputRef}
                type="text"
                value={editValue}
                onChange={(e) => setEditValue(e.target.value)}
                onKeyDown={handleKeyDown}
                onBlur={handleSubmitRename}
                onClick={(e) => e.stopPropagation()}
                className="w-full border-b border-[var(--brand-primary)] bg-transparent py-0.5 text-sm text-[var(--text-primary)] outline-none"
              />
            ) : (
              <>
                <p
                  ref={titleRef}
                  className="flex items-center gap-1.5 overflow-hidden whitespace-nowrap text-sm leading-5"
                  style={scrollVars}
                >
                  {channelBadge && (
                    <MessageCircle className={cn("inline h-3 w-3 shrink-0", channelBadge.color)} />
                  )}
                  <span
                    className={cn(
                      "min-w-0 flex-1 overflow-hidden text-ellipsis",
                      scrollVars ? "inline-block group-hover/session:animate-scroll-text" : "",
                    )}
                  >
                    {title}
                  </span>
                  {isLive && (
                    <Loader2
                      aria-label={t('sessionIsGenerating', { defaultValue: 'Generating in background' })}
                      className="h-3 w-3 shrink-0 animate-spin text-[var(--brand-primary)]"
                    />
                  )}
                </p>
                {snippet && (
                  <p className="mt-0.5 truncate text-ui-2xs leading-4 text-[var(--text-tertiary)]">
                    …{snippet}…
                  </p>
                )}
              </>
            )}
          </div>

          {/* Right-side slot: relative time. Item actions live in the menus. */}
          {!isEditing && !menuOpen && !dropdownOpen && (
            <span
              aria-hidden
              className={cn(
                "absolute right-3 top-1/2 -translate-y-1/2 text-ui-2xs text-[var(--text-tertiary)] opacity-100 transition-opacity group-hover/session:opacity-0",
              )}
            >
              {relativeTime}
            </span>
          )}

          {!isEditing && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                e.currentTarget.blur();
                onArchive?.(session.id);
              }}
              className={cn(
                "absolute right-9 top-1/2 z-10 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md text-[var(--text-tertiary)] opacity-0 transition-opacity hover:bg-[var(--surface-tertiary)] hover:text-[var(--text-primary)] focus:outline-none focus-visible:ring-1 focus-visible:ring-[var(--brand-primary)] focus-visible:opacity-100 group-hover/session:opacity-100",
              )}
              aria-label={t('archiveChat')}
              title={t('archiveChat')}
            >
              <Archive className="h-3.5 w-3.5" />
            </button>
          )}

          {/* ••• overflow menu — discoverable, click/touch-friendly entry to
              rename and the rest of the actions (mirrors the right-click menu).
              Restores the trigger removed in f03f992. */}
          {!isEditing && (
            <DropdownMenu onOpenChange={setDropdownOpen}>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  onClick={(e) => e.stopPropagation()}
                  className={cn(
                    "absolute right-1.5 top-1/2 z-10 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md text-[var(--text-tertiary)] opacity-0 transition-opacity hover:bg-[var(--surface-tertiary)] hover:text-[var(--text-primary)] focus:outline-none focus-visible:ring-1 focus-visible:ring-[var(--brand-primary)] focus-visible:opacity-100 group-hover/session:opacity-100 data-[state=open]:opacity-100",
                  )}
                  aria-label={t('moreActions', { defaultValue: 'More actions' })}
                  title={t('moreActions', { defaultValue: 'More actions' })}
                >
                  <EllipsisVertical className="h-3.5 w-3.5" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-48">
                <MenuItems Item={DropdownMenuItem} Separator={DropdownMenuSeparator} />
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent className="w-48">
        <MenuItems Item={ContextMenuItem} Separator={ContextMenuSeparator} />
      </ContextMenuContent>
    </ContextMenu>
  );
});

/** Map session slug prefix to a channel badge. */
function getChannelBadge(slug: string): { label: string; color: string } | null {
  if (!slug) return null;
  const prefix = slug.split(":")[0];
  switch (prefix) {
    case "feishu":   return { label: "飞书", color: "text-blue-500" };
    case "wechat":
    case "weixin":   return { label: "微信", color: "text-green-500" };
    case "dingtalk": return { label: "钉钉", color: "text-sky-500" };
    case "wecom":    return { label: "企业微信", color: "text-blue-500" };
    case "qq":       return { label: "QQ", color: "text-sky-400" };
    default: return slug.includes(":") ? { label: prefix, color: "text-[var(--text-tertiary)]" } : null;
  }
}
