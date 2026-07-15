"use client";

import { useEffect, useState } from "react";
import { Target } from "lucide-react";
import { useTranslation } from "react-i18next";
import { GoalCard } from "./goal-card";
import { GoalStatusBadge, GoalStatusIcon, goalStatusKey } from "./goal-status";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { useGoalsReleased, useSessionGoal } from "@/hooks/use-session-goal";
import { useIsDesktop } from "@/hooks/use-platform";
import { useWorkspaceStore } from "@/stores/workspace-store";
import { cn } from "@/lib/utils";
import {
  OPEN_SESSION_GOAL_EVENT,
  type OpenSessionGoalEventDetail,
} from "@/lib/goal-ui";

export function GoalStatusControl({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation("chat");
  const goalsReleased = useGoalsReleased();
  const { goal } = useSessionGoal(sessionId, { enabled: goalsReleased });
  const isDesktop = useIsDesktop();
  const openWorkspace = useWorkspaceStore((state) => state.open);
  const [mobileOpen, setMobileOpen] = useState(false);

  useEffect(() => {
    const handleOpenRequest = (event: Event) => {
      const detail = (event as CustomEvent<OpenSessionGoalEventDetail>).detail;
      if (detail?.sessionId !== sessionId) return;
      if (isDesktop) openWorkspace();
      else setMobileOpen(true);
    };
    window.addEventListener(OPEN_SESSION_GOAL_EVENT, handleOpenRequest);
    return () => window.removeEventListener(OPEN_SESSION_GOAL_EVENT, handleOpenRequest);
  }, [isDesktop, openWorkspace, sessionId]);

  if (!goalsReleased) return null;
  // The desktop workspace already carries the discoverable empty state. Keep
  // the header quiet until a goal exists; mobile has no workspace panel, so it
  // always retains a reachable target button.
  if (isDesktop && !goal) return null;

  const label = goal ? t(goalStatusKey(goal)) : t("goalEmptyTitle");
  const handleOpen = () => {
    if (isDesktop) openWorkspace();
    else setMobileOpen(true);
  };

  return (
    <>
      <button
        type="button"
        onClick={handleOpen}
        className={cn(
          "inline-flex shrink-0 items-center justify-center rounded-full text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-secondary)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]",
          isDesktop ? "h-8 max-w-40 gap-1.5 px-2.5" : "h-11 w-11",
        )}
        aria-label={t("goalOpenAria", { status: label })}
        data-testid="goal-status-control"
      >
        {goal ? (
          isDesktop ? (
            <GoalStatusBadge goal={goal} compact className="max-w-full" />
          ) : (
            <GoalStatusIcon goal={goal} className="h-[18px] w-[18px]" />
          )
        ) : (
          <Target className="h-[18px] w-[18px]" />
        )}
      </button>

      {!isDesktop && (
        <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
          <SheetContent
            side="bottom"
            className="max-h-[85dvh] overflow-y-auto rounded-t-3xl bg-[var(--surface-chat)] px-4 pb-[max(env(safe-area-inset-bottom),16px)] pt-5"
          >
            <div className="mx-auto mb-4 h-1 w-10 rounded-full bg-[var(--text-quaternary)]" aria-hidden="true" />
            <h2 className="pr-8 text-base font-semibold text-[var(--text-primary)]">{t("goalTitle")}</h2>
            <p className="mt-1 text-xs text-[var(--text-tertiary)]">{t("goalMobileDescription")}</p>
            <GoalCard sessionId={sessionId} variant="mobile" className="mt-4" />
          </SheetContent>
        </Sheet>
      )}
    </>
  );
}
