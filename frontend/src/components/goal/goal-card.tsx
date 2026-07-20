"use client";

import { useState } from "react";
import {
  AlertTriangle,
  Clock3,
  Gauge,
  Pencil,
  Play,
  RotateCw,
  Target,
  Trash2,
  Pause,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { GoalEditorDialog } from "./goal-editor-dialog";
import { GoalStatusBadge } from "./goal-status";
import {
  useGoalsReleased,
  useGoalTokenBudgetLimits,
  useSessionGoal,
  useSessionGoalUsage,
} from "@/hooks/use-session-goal";
import { formatElapsedDuration } from "@/lib/duration";
import {
  goalBlockerMessageKey,
  goalNeedsBudgetIncrease,
} from "@/lib/goal-ui";
import { cn } from "@/lib/utils";

interface GoalCardProps {
  sessionId: string | null | undefined;
  variant?: "panel" | "mobile";
  className?: string;
}

function formatTokenCount(value: number, language: string): string {
  return new Intl.NumberFormat(language, {
    maximumFractionDigits: 0,
  }).format(Math.max(0, value));
}

export function GoalCard({ sessionId, variant = "panel", className }: GoalCardProps) {
  const { t, i18n } = useTranslation("chat");
  const goalsReleased = useGoalsReleased();
  const goalTokenBudgetLimits = useGoalTokenBudgetLimits();
  const {
    goal,
    isLoading,
    isError,
    refetch,
    updateGoal,
    pauseGoal,
    resumeGoal,
    clearGoal,
    isUpdating,
    isPausing,
    isResuming,
    isClearing,
  } = useSessionGoal(sessionId, { enabled: goalsReleased });
  const { data: tokenUsage = null } = useSessionGoalUsage(
    sessionId,
    goal,
    { enabled: goalsReleased && goal?.token_budget != null },
  );
  const [editing, setEditing] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const language = i18n.resolvedLanguage || i18n.language || "en";
  const mobile = variant === "mobile";

  if (!goalsReleased || !sessionId) return null;

  const shellClass = cn(
    mobile
      ? "rounded-2xl border border-[var(--border-default)] bg-[var(--surface-primary)] p-4"
      : "overflow-hidden rounded-2xl border border-[var(--border-default)] bg-[var(--surface-primary)] p-4 shadow-[var(--shadow-sm)]",
    className,
  );

  if (isLoading) {
    return (
      <section className={shellClass} aria-label={t("goalTitle")} data-testid="goal-card-loading">
        <div className="h-4 w-20 animate-pulse rounded bg-[var(--surface-tertiary)]" />
        <div className="mt-3 h-12 animate-pulse rounded-xl bg-[var(--surface-tertiary)]" />
      </section>
    );
  }

  if (isError) {
    return (
      <section className={shellClass} aria-label={t("goalTitle")} data-testid="goal-card-error">
        <div className="flex items-start gap-2.5">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-warning)]" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-[var(--text-primary)]">{t("goalLoadFailed")}</p>
            <button
              type="button"
              onClick={() => void refetch()}
              className="mt-2 inline-flex items-center gap-1.5 text-xs font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
            >
              <RotateCw className="h-3.5 w-3.5" />
              {t("retry")}
            </button>
          </div>
        </div>
      </section>
    );
  }

  if (!goal) {
    return (
      <section className={shellClass} aria-label={t("goalTitle")} data-testid="goal-card-empty">
        <div className="flex items-start gap-3">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--surface-secondary)] text-[var(--text-tertiary)]">
            <Target className="h-4 w-4" />
          </span>
          <div className="min-w-0 flex-1">
            <h2 className="text-sm font-medium text-[var(--text-primary)]">{t("goalEmptyTitle")}</h2>
            <p className="mt-1 text-xs leading-relaxed text-[var(--text-tertiary)]">
              {t("goalEmptyDescription")}
            </p>
          </div>
        </div>
      </section>
    );
  }

  const tokenBudget = goal.token_budget ?? null;
  const displayedTokens = tokenUsage?.total_tokens ?? goal.tokens_used;
  const budgetPercent = tokenBudget === null
    ? 0
    : tokenBudget === 0
      ? 100
      : Math.min(100, Math.round((displayedTokens / tokenBudget) * 100));
  const busy = isUpdating || isPausing || isResuming || isClearing;
  const canPause = goal.status === "active" && goal.run_state !== "pausing";
  const canResume = goal.status !== "active"
    || goal.run_state === "interrupted"
    || goal.needs_review;
  const needsBudgetIncrease = goalNeedsBudgetIncrease(goal);
  const blockerMessageKey = goalBlockerMessageKey(goal);
  const blockerMessage = goal.status === "budget_limited"
    ? t("goalBudgetLimitedDescription")
    : blockerMessageKey
      ? t(blockerMessageKey)
      : goal.blocker_message;
  const mustPauseBeforeClear = [
    "reserved",
    "running",
    "waiting_user",
    "pausing",
  ].includes(goal.run_state);

  const runAction = async (action: "pause" | "resume") => {
    try {
      if (action === "pause") {
        await pauseGoal();
        toast.success(t("goalPaused"));
      } else {
        await resumeGoal();
        toast.success(t("goalResumed"));
      }
    } catch {
      toast.error(t(action === "pause" ? "goalPauseFailed" : "goalResumeFailed"));
    }
  };

  const handleClear = async () => {
    try {
      await clearGoal();
      setConfirmClear(false);
      toast.success(t("goalCleared"));
    } catch {
      toast.error(t("goalClearFailed"));
    }
  };

  return (
    <>
      <section className={shellClass} aria-label={t("goalTitle")} data-testid="goal-card">
        <div className="flex min-w-0 items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2 pt-1" data-testid="goal-run-status">
            <p className="shrink-0 text-[11px] font-medium uppercase tracking-[0.08em] text-[var(--text-quaternary)]">
              {t("goalTitle")}
            </p>
            <span className="h-3 w-px shrink-0 bg-[var(--border-default)]" aria-hidden="true" />
            <GoalStatusBadge goal={goal} compact className="max-w-full" />
          </div>
          <button
            type="button"
            onClick={() => setEditing(true)}
            disabled={busy}
            className={cn(
              "flex shrink-0 items-center justify-center rounded-full text-[var(--text-tertiary)] transition-colors hover:bg-[var(--surface-secondary)] hover:text-[var(--text-primary)] disabled:opacity-40",
              mobile ? "h-11 w-11" : "h-8 w-8",
            )}
            aria-label={t("goalEditAction")}
          >
            <Pencil className="h-4 w-4" />
          </button>
        </div>

        <p className="mt-3 whitespace-pre-wrap break-words text-sm font-medium leading-relaxed text-[var(--text-primary)]">
          {goal.objective}
        </p>

        {goal.definition_of_done && (
          <div className="mt-3 rounded-xl bg-[var(--surface-secondary)] px-3 py-2.5">
            <p className="text-[10px] font-medium uppercase tracking-[0.08em] text-[var(--text-quaternary)]">
              {t("goalDefinitionOfDone")}
            </p>
            <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-[var(--text-secondary)]">
              {goal.definition_of_done}
            </p>
          </div>
        )}

        {(blockerMessage || goal.needs_review) && (
          <div className="mt-3 flex items-start gap-2 rounded-xl border border-[var(--color-warning)]/25 bg-[var(--color-warning)]/8 px-3 py-2.5">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[var(--color-warning)]" />
            <p className="text-xs leading-relaxed text-[var(--text-secondary)]">
              {blockerMessage || t("goalNeedsReviewDescription")}
            </p>
          </div>
        )}

        {goal.completion_summary && goal.status === "complete" && (
          <p className="mt-3 text-xs leading-relaxed text-[var(--text-secondary)]">
            {goal.completion_summary}
          </p>
        )}

        <div className="mt-4 grid grid-cols-2 gap-2 text-[11px] text-[var(--text-tertiary)]">
          <span className="inline-flex items-center gap-1.5">
            <RotateCw className="h-3.5 w-3.5" />
            {t("goalContinuationCount", { count: goal.continuation_count })}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <Clock3 className="h-3.5 w-3.5" />
            {formatElapsedDuration(goal.time_used_seconds, language)}
          </span>
        </div>

        {tokenBudget !== null && (
          <div className="mt-3">
            <div className="flex items-start justify-between gap-2 text-[11px] text-[var(--text-tertiary)]">
              <span className="inline-flex min-w-0 items-start gap-1.5 leading-relaxed">
                <Gauge className="h-3.5 w-3.5" />
                {t("goalBudgetUsage")}
              </span>
              <span className="shrink-0 text-right tabular-nums">
                {t("goalTokenUsageWithBudget", {
                  used: formatTokenCount(displayedTokens, language),
                  budget: formatTokenCount(tokenBudget, language),
                })}
              </span>
            </div>
            <div
              className="mt-2 h-1 overflow-hidden rounded-full bg-[var(--surface-tertiary)]"
              role="progressbar"
              aria-label={t("goalBudgetUsage")}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={budgetPercent}
            >
              <div
                className={cn(
                  "h-full rounded-full transition-[width]",
                  budgetPercent >= 80 ? "bg-[var(--color-warning)]" : "bg-[var(--brand-primary)]",
                )}
                style={{ width: `${budgetPercent}%` }}
              />
            </div>
          </div>
        )}

        {needsBudgetIncrease && (
          <p className="mt-3 text-xs leading-relaxed text-[var(--color-warning)]">
            {t("goalIncreaseBudgetFirst")}
          </p>
        )}

        <div className={cn("mt-4 flex gap-2", mobile && "[&_button]:min-h-11")}>
          {canPause && (
            <Button
              variant="outline"
              size={mobile ? "default" : "sm"}
              className="flex-1"
              onClick={() => void runAction("pause")}
              disabled={busy}
            >
              <Pause className="h-4 w-4" />
              {isPausing ? t("goalPausing") : t("goalPauseAction")}
            </Button>
          )}
          {canResume && (
            <Button
              variant="outline"
              size={mobile ? "default" : "sm"}
              className="flex-1"
              onClick={() => void runAction("resume")}
              disabled={busy || needsBudgetIncrease}
              title={needsBudgetIncrease ? t("goalIncreaseBudgetFirst") : undefined}
            >
              <Play className="h-4 w-4" />
              {isResuming ? t("goalResuming") : t("goalResumeAction")}
            </Button>
          )}
          <Button
            variant="ghost"
            size={mobile ? "default" : "sm"}
            className={cn("text-[var(--text-tertiary)]", mobile && "w-11 px-0")}
            onClick={() => setConfirmClear(true)}
            disabled={busy || mustPauseBeforeClear}
            aria-label={t("goalClearAction")}
            title={mustPauseBeforeClear ? t("goalClearPauseFirst") : undefined}
          >
            <Trash2 className="h-4 w-4" />
            {!mobile && <span className="sr-only">{t("goalClearAction")}</span>}
          </Button>
        </div>
      </section>

      <GoalEditorDialog
        goal={goal}
        open={editing}
        onOpenChange={setEditing}
        onSave={updateGoal}
        maxTokenBudget={goalTokenBudgetLimits?.max_token_budget}
      />

      <Dialog open={confirmClear} onOpenChange={setConfirmClear}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{t("goalClearConfirmTitle")}</DialogTitle>
            <DialogDescription>{t("goalClearConfirmDescription")}</DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="outline" onClick={() => setConfirmClear(false)} disabled={isClearing}>
              {t("cancel")}
            </Button>
            <Button variant="destructive" onClick={() => void handleClear()} disabled={isClearing}>
              {isClearing ? t("goalClearing") : t("goalClearAction")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
