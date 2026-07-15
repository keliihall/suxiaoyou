"use client";

import { CheckCircle2, ChevronRight, Wrench } from "lucide-react";
import { useTranslation } from "react-i18next";
import { SuxiaoyouLogo } from "@/components/ui/suxiaoyou-logo";
import { useActivityStore, type ActivityData } from "@/stores/activity-store";
import { formatElapsedDuration } from "@/lib/duration";
import { isActivityComplete } from "@/lib/activity-state";

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

  if (!hasReasoning && !hasTools) return null;

  const parts: string[] = [];
  if (isCompleted) {
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

  return (
    <button
      type="button"
      onClick={() => data.sourceKey && toggleForMessage(data.sourceKey, data)}
      className="flex items-center gap-2 text-xs text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors py-1.5 group"
    >
      {isCompleted ? (
        <CheckCircle2 className="h-3.5 w-3.5 text-[var(--tool-completed)]" />
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
