"use client";

import { ArrowUp, Square } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";

interface ChatActionsProps {
  isBusy: boolean;
  canSend: boolean;
  onSend: () => void;
  onStop: () => void;
  sendLabel?: string;
  sendHint?: string;
}

export function ChatActions({ isBusy, canSend, onSend, onStop, sendLabel, sendHint }: ChatActionsProps) {
  const { t } = useTranslation("chat");
  const resolvedSendLabel = sendLabel ?? t("sendAction");
  const resolvedSendHint = sendHint ?? t("sendActionHint");

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex items-center gap-1">
        {isBusy && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                size="icon"
                variant="ghost"
                className="h-8 w-8 rounded-full text-[var(--text-secondary)] hover:bg-[var(--surface-secondary)] hover:text-[var(--text-primary)]"
                onClick={onStop}
                aria-label={t("stopAction")}
              >
                <Square className="h-3.5 w-3.5 fill-current" />
                <span className="sr-only">{t("stopAction")}</span>
              </Button>
            </TooltipTrigger>
            <TooltipContent>{t("stopAction")}</TooltipContent>
          </Tooltip>
        )}
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              size="icon"
              className="h-8 w-8 rounded-full bg-[var(--text-primary)] text-[var(--surface-primary)] hover:bg-[var(--text-primary)]/90 disabled:bg-[var(--text-tertiary)]/30 disabled:text-[var(--surface-primary)] disabled:opacity-100"
              onClick={onSend}
              disabled={!canSend}
              aria-label={resolvedSendLabel}
            >
              <ArrowUp className="h-[18px] w-[18px]" />
              <span className="sr-only">{resolvedSendLabel}</span>
            </Button>
          </TooltipTrigger>
          <TooltipContent>{resolvedSendHint}</TooltipContent>
        </Tooltip>
      </div>
    </TooltipProvider>
  );
}
