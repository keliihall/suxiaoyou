"use client";

import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { ListTree, Loader2, Paperclip, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn, formatFullDateTime } from "@/lib/utils";
import { conversationOutlineKeyTarget } from "@/lib/conversation-outline";
import type { ConversationTurn } from "@/types/message";

export const CONVERSATION_OUTLINE_MIN_TURNS = 4;

interface ConversationOutlineProps {
  turns: ConversationTurn[];
  activeMessageId: string | null;
  locatingMessageId: string | null;
  locateErrorMessageId: string | null;
  contentIsTallEnough: boolean;
  onSelect: (turn: ConversationTurn) => void;
  onRetry: () => void;
}

function turnSummary(turn: ConversationTurn, fallback: string) {
  return turn.summary.trim() || turn.attachment_names[0] || fallback;
}

export function ConversationOutline({
  turns,
  activeMessageId,
  locatingMessageId,
  locateErrorMessageId,
  contentIsTallEnough,
  onSelect,
  onRetry,
}: ConversationOutlineProps) {
  const { t, i18n } = useTranslation("chat");
  const [mobileOpen, setMobileOpen] = useState(false);
  const railRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!activeMessageId) return;
    const marker = railRef.current?.querySelector<HTMLElement>(
      `[data-turn-marker="${CSS.escape(activeMessageId)}"]`,
    );
    marker?.scrollIntoView({ block: "nearest" });
  }, [activeMessageId]);

  if (turns.length < CONVERSATION_OUTLINE_MIN_TURNS || !contentIsTallEnough) {
    return null;
  }

  const focusAndSelect = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    const targetIndex = conversationOutlineKeyTarget(
      event.key,
      index,
      turns.length,
    );
    if (targetIndex === null) return;
    event.preventDefault();
    const nextTurn = turns[targetIndex];
    const container = event.currentTarget.closest<HTMLElement>(
      "[data-conversation-outline-list]",
    );
    container
      ?.querySelectorAll<HTMLButtonElement>("button[data-outline-turn]")
      .item(targetIndex)
      ?.focus();
    onSelect(nextTurn);
  };

  const markerLabel = (turn: ConversationTurn) =>
    t("conversationTurnLabel", {
      number: turn.ordinal,
      total: turns.length,
      summary: turnSummary(turn, t("conversationOutlineUntitled")),
    });

  return (
    <>
      <TooltipProvider delayDuration={120}>
        <nav
          ref={railRef}
          aria-label={t("conversationOutline")}
          className="absolute bottom-4 left-1 top-4 z-20 hidden w-7 flex-col items-center justify-center md:flex"
        >
          <div
            data-conversation-outline-list
            className={cn(
              "flex max-h-full w-full flex-col items-center overflow-y-auto overscroll-contain py-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
              locateErrorMessageId && "max-h-[calc(100%_-_1.75rem)]",
            )}
          >
            {turns.map((turn, index) => {
              const active = turn.message_id === activeMessageId;
              const locating = turn.message_id === locatingMessageId;
              return (
                <Tooltip key={turn.message_id}>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      data-outline-turn
                      data-turn-marker={turn.message_id}
                      aria-current={active ? "location" : undefined}
                      aria-label={markerLabel(turn)}
                      onClick={() => onSelect(turn)}
                      onKeyDown={(event) => focusAndSelect(event, index)}
                      className="group flex h-6 min-h-6 w-6 items-center justify-start rounded-sm pl-0.5 text-[var(--text-tertiary)] hover:text-[var(--text-primary)]"
                    >
                      {locating ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <span
                          aria-hidden="true"
                          className={cn(
                            "block h-px rounded-full bg-current transition-[width,color]",
                            active
                              ? "w-5 text-[var(--text-primary)]"
                              : "w-2.5 group-hover:w-4",
                          )}
                        />
                      )}
                    </button>
                  </TooltipTrigger>
                  <TooltipContent
                    side="right"
                    align="center"
                    sideOffset={8}
                    className="max-w-80 border border-[var(--border-default)] bg-[var(--surface-tertiary)] px-3 py-2"
                  >
                    <p className="font-medium text-[var(--text-primary)]">
                      {t("conversationTurnNumber", { number: turn.ordinal })}
                    </p>
                    <p className="mt-0.5 line-clamp-3 text-[var(--text-secondary)]">
                      {turnSummary(turn, t("conversationOutlineUntitled"))}
                    </p>
                    <p className="mt-1 text-[var(--text-tertiary)]">
                      {formatFullDateTime(turn.time_created, i18n.language)}
                    </p>
                    {turn.attachment_names.length > 0 && (
                      <p className="mt-1 flex items-start gap-1 text-[var(--text-tertiary)]">
                        <Paperclip className="mt-0.5 h-3 w-3 shrink-0" />
                        <span className="break-all">
                          {turn.attachment_names.join(", ")}
                        </span>
                      </p>
                    )}
                  </TooltipContent>
                </Tooltip>
              );
            })}
          </div>
          {locateErrorMessageId && (
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={onRetry}
                  aria-label={t("conversationLocateRetry")}
                  className="mt-1 flex h-6 min-h-6 w-6 shrink-0 items-center justify-center rounded text-[var(--color-destructive)] hover:bg-[var(--surface-secondary)]"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                </button>
              </TooltipTrigger>
              <TooltipContent side="right">
                {t("conversationLocateFailed")}
              </TooltipContent>
            </Tooltip>
          )}
        </nav>
      </TooltipProvider>

      <div className="absolute left-2 top-2 z-20 md:hidden">
        <Popover open={mobileOpen} onOpenChange={setMobileOpen}>
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-label={t("conversationOutline")}
              className="h-8 bg-[var(--surface-primary)] px-2 shadow-[var(--shadow-sm)]"
            >
              <ListTree className="h-4 w-4" />
              <span className="hidden min-[420px]:inline">
                {t("conversationOutline")}
              </span>
            </Button>
          </PopoverTrigger>
          <PopoverContent
            align="start"
            side="bottom"
            sideOffset={6}
            className="w-[min(22rem,calc(100vw-1rem))] p-2"
          >
            <p className="px-2 py-1 text-xs font-medium text-[var(--text-secondary)]">
              {t("conversationOutlineCount", { count: turns.length })}
            </p>
            {locateErrorMessageId && (
              <button
                type="button"
                onClick={onRetry}
                className="mb-1 flex min-h-9 w-full items-center gap-2 rounded-md px-2 text-left text-xs text-[var(--color-destructive)] hover:bg-[var(--surface-secondary)]"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                {t("conversationLocateFailedRetry")}
              </button>
            )}
            <div
              data-conversation-outline-list
              className="max-h-[60vh] overflow-y-auto overscroll-contain"
            >
              {turns.map((turn, index) => {
                const active = turn.message_id === activeMessageId;
                const locating = turn.message_id === locatingMessageId;
                return (
                  <button
                    key={turn.message_id}
                    type="button"
                    data-outline-turn
                    aria-current={active ? "location" : undefined}
                    aria-label={markerLabel(turn)}
                    onClick={() => {
                      onSelect(turn);
                      setMobileOpen(false);
                    }}
                    onKeyDown={(event) => focusAndSelect(event, index)}
                    className={cn(
                      "flex min-h-11 w-full items-start gap-2 rounded-md px-2 py-2 text-left hover:bg-[var(--surface-secondary)]",
                      active && "bg-[var(--surface-secondary)]",
                    )}
                  >
                    <span className="flex h-5 w-7 shrink-0 items-center justify-end text-[11px] tabular-nums text-[var(--text-tertiary)]">
                      {locating ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        turn.ordinal
                      )}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="line-clamp-2 block text-xs text-[var(--text-primary)]">
                        {turnSummary(turn, t("conversationOutlineUntitled"))}
                      </span>
                      <span className="mt-0.5 block text-[11px] text-[var(--text-tertiary)]">
                        {formatFullDateTime(turn.time_created, i18n.language)}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          </PopoverContent>
        </Popover>
      </div>
    </>
  );
}
