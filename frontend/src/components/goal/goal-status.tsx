"use client";

import {
  AlertCircle,
  CheckCircle2,
  CirclePause,
  Gauge,
  Loader2,
  Target,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import {
  resolveGoalPresentationState,
  type GoalPresentationState,
} from "@/lib/goal-ui";
import type { SessionGoal } from "@/types/goal";

const GOAL_STATUS_KEYS: Record<GoalPresentationState, string> = {
  active: "goalStatusActive",
  running: "goalStatusRunning",
  pausing: "goalStatusPausing",
  waiting_user: "goalStatusWaitingUser",
  needs_review: "goalStatusNeedsReview",
  paused: "goalStatusPaused",
  blocked: "goalStatusBlocked",
  usage_limited: "goalStatusUsageLimited",
  budget_limited: "goalStatusBudgetLimited",
  complete: "goalStatusComplete",
};

export function goalStatusKey(goal: SessionGoal): string {
  return GOAL_STATUS_KEYS[resolveGoalPresentationState(goal)];
}

function GoalStatusIcon({ goal, className }: { goal: SessionGoal; className?: string }) {
  const state = resolveGoalPresentationState(goal);
  if (state === "running" || state === "pausing") {
    return <Loader2 className={cn("animate-spin", className)} aria-hidden="true" />;
  }
  if (state === "waiting_user" || state === "needs_review") {
    return <AlertCircle className={className} aria-hidden="true" />;
  }
  if (state === "complete") return <CheckCircle2 className={className} aria-hidden="true" />;
  if (state === "paused") return <CirclePause className={className} aria-hidden="true" />;
  if (state === "blocked") return <AlertCircle className={className} aria-hidden="true" />;
  if (state === "budget_limited" || state === "usage_limited") {
    return <Gauge className={className} aria-hidden="true" />;
  }
  return <Target className={className} aria-hidden="true" />;
}

export function GoalStatusBadge({
  goal,
  compact = false,
  className,
}: {
  goal: SessionGoal;
  compact?: boolean;
  className?: string;
}) {
  const { t } = useTranslation("chat");
  const state = resolveGoalPresentationState(goal);
  const attention = [
    "waiting_user",
    "needs_review",
    "blocked",
    "budget_limited",
    "usage_limited",
  ].includes(state);
  const complete = state === "complete";

  return (
    <span
      role="status"
      className={cn(
        "inline-flex min-w-0 items-center gap-1.5 font-medium",
        compact ? "text-[11px]" : "rounded-lg px-2 py-1 text-xs",
        attention
          ? cn(
              "text-[var(--color-warning)]",
              !compact && "bg-[var(--color-warning)]/8",
            )
          : complete
            ? cn(
                "text-[var(--tool-completed)]",
                !compact && "bg-[var(--tool-completed)]/8",
              )
            : cn(
                "text-[var(--text-secondary)]",
                !compact && "bg-[var(--surface-secondary)]",
              ),
        className,
      )}
    >
      <GoalStatusIcon goal={goal} className="h-3.5 w-3.5 shrink-0" />
      <span className="truncate">{t(GOAL_STATUS_KEYS[state])}</span>
    </span>
  );
}

export { GoalStatusIcon };
