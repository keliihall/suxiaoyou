"use client";

import { CheckCircle2, ChevronRight, CircleAlert, Wrench } from "lucide-react";
import { useTranslation } from "react-i18next";
import { SuxiaoyouLogo } from "@/components/ui/suxiaoyou-logo";
import { useActivityStore, type ActivityData } from "@/stores/activity-store";
import { formatElapsedDuration } from "@/lib/duration";
import { isActivityComplete, isActivityFailed } from "@/lib/activity-state";

interface ActivitySummaryProps {
  data: ActivityData;
}

export function ActivitySummary({ data }: ActivitySummaryProps) {
  const { t, i18n } = useTranslation("chat");
  const toggleForMessage = useActivityStore((s) => s.toggleForMessage);
  const isActiveOpen = useActivityStore(
    (s) => s.isOpen && !!data.sourceKey && s.activeKey === data.sourceKey,
  );

  const hasReasoning = data.reasoningTexts.length > 0;
  const hasTools = data.toolParts.length > 0;
  const isCompleted = isActivityComplete(data);
  const activityFailed = isCompleted && isActivityFailed(data);
  const adjustmentCount = isCompleted && !activityFailed
    ? data.toolParts.filter((tool) => tool.state.status === "error").length
    : 0;

  if (!hasReasoning && !hasTools) return null;

  const parts: string[] = [];
  if (activityFailed) {
    parts.push(t("activityNotCompleted"));
  } else if (isCompleted) {
    parts.push(t("done"));
  } else if (hasReasoning) {
    parts.push(
      data.thinkingDuration != null
        ? t("thoughtFor", {
            duration: formatElapsedDuration(
              data.thinkingDuration,
              i18n.language,
            ),
          })
        : t("reasoning"),
    );
  }
  if (hasTools) {
    const count = data.toolParts.length;
    parts.push(t("toolCallCount", { count }));
  }
  if (adjustmentCount > 0) {
    parts.push(t("activityAdjustmentCount", { count: adjustmentCount }));
  }

  return (
    <button
      type="button"
      onClick={() => data.sourceKey && toggleForMessage(data.sourceKey, data)}
      className="flex items-center gap-2 text-xs text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors py-1.5 group"
    >
      {activityFailed ? (
        <CircleAlert
          aria-hidden="true"
          className="h-3.5 w-3.5 text-[var(--color-destructive)]"
        />
      ) : isCompleted ? (
        <CheckCircle2 aria-hidden="true" className="h-3.5 w-3.5 text-[var(--tool-completed)]" />
      ) : hasReasoning ? (
        <SuxiaoyouLogo size={14} />
      ) : (
        <Wrench className="h-3.5 w-3.5" />
      )}
      <span>{parts.join(" · ")}</span>
      <ChevronRight
        className={`h-3 w-3 transition-transform duration-200 ${isActiveOpen ? "rotate-90" : ""}`}
      />
    </button>
  );
}
