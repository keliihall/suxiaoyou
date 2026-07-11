"use client";

import { AlertTriangle, CheckCircle2, Loader2, RefreshCw, Square } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import {
  isInteractionRecoveryActionable,
  type InteractionResponseState,
} from "@/lib/interaction-response";

interface InteractionAcknowledgementProps {
  state: Exclude<InteractionResponseState, "idle">;
  decision?: string | null;
  source?: string | null;
  onRecover?: () => void;
  onStop?: () => void;
}

function decisionKey(decision?: string | null): string {
  if (!decision) return "interactionResolved";
  const keys: Record<string, string> = {
    allowed: "interactionAllowed",
    denied: "interactionDenied",
    answered: "interactionAnswered",
    cancelled: "interactionCancelled",
    accept: "interactionPlanAccepted",
    revise: "interactionPlanRevised",
    stop: "interactionPlanStopped",
  };
  return keys[decision] ?? "interactionResolved";
}

export function InteractionAcknowledgement({
  state,
  decision,
  source,
  onRecover,
  onStop,
}: InteractionAcknowledgementProps) {
  const { t } = useTranslation("chat");
  const isBusy = state === "submitting"
    || state === "continuing"
    || state === "recovering";
  const needsRecovery = isInteractionRecoveryActionable(state);
  let label = t(decisionKey(decision));
  if (state === "submitting") label = t("interactionSubmitting");
  if (state === "recovering") label = t("interactionRecovering");
  if (state === "continuing") label = t("interactionContinuing");
  if (needsRecovery) label = t("interactionRecoveryNeeded");

  return (
    <div className="px-4 pb-3" role="status" aria-live="polite">
      <div className="mx-auto max-w-3xl xl:max-w-4xl">
        <div className="rounded-xl border border-[var(--border-default)] bg-[var(--surface-secondary)] px-4 py-3">
          <div className="flex items-center gap-2.5 text-sm text-[var(--text-secondary)]">
            {isBusy ? (
              <Loader2 className="h-4 w-4 animate-spin text-[var(--brand-primary)]" />
            ) : needsRecovery ? (
              <AlertTriangle className="h-4 w-4 text-[var(--color-warning)]" />
            ) : (
              <CheckCircle2 className="h-4 w-4 text-[var(--color-success)]" />
            )}
            <span className="font-medium text-[var(--text-primary)]">{label}</span>
            {source === "remote" && (
              <span className="text-xs text-[var(--text-tertiary)]">
                {t("interactionRemoteSource")}
              </span>
            )}
            {needsRecovery && (
              <div className="ml-auto flex shrink-0 items-center gap-1.5">
                {onRecover && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-7 gap-1 text-xs"
                    onClick={onRecover}
                  >
                    <RefreshCw className="h-3 w-3" />
                    {t("interactionRecoverAction")}
                  </Button>
                )}
                {onStop && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-7 gap-1 text-xs"
                    onClick={onStop}
                  >
                    <Square className="h-3 w-3" />
                    {t("interactionStopAction")}
                  </Button>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
