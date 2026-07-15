"use client";

import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import {
  getVisibleToolMetadata,
  type VisibleToolMetadataKey,
} from "@/lib/tool-metadata";

interface ToolMetadataSummaryProps {
  metadata: Record<string, unknown> | null | undefined;
  className?: string;
}

const LABEL_KEYS: Record<VisibleToolMetadataKey, string> = {
  provider: "toolMetadataProvider",
  model: "toolMetadataModel",
  estimatedCost: "toolMetadataEstimatedCost",
  actualCost: "toolMetadataActualCost",
  costNotice: "toolMetadataCostNotice",
};

export function ToolMetadataSummary({ metadata, className }: ToolMetadataSummaryProps) {
  const { t, i18n } = useTranslation("chat");
  const items = getVisibleToolMetadata(metadata, i18n.language);
  if (items.length === 0) return null;

  return (
    <span
      data-testid="tool-metadata-summary"
      className={cn("flex min-w-0 flex-wrap gap-1.5", className)}
    >
      {items.map((item) => (
        <span
          key={item.key}
          data-metadata-key={item.key}
          title={item.title}
          className={cn(
            "inline-flex max-w-full items-center gap-1 rounded-md border border-[var(--border-default)]",
            "bg-[var(--surface-tertiary)] px-1.5 py-0.5 text-[10px] text-[var(--text-secondary)]",
            item.warning && "border-[var(--color-warning)]/30 text-[var(--color-warning)]",
          )}
        >
          <span className="text-[var(--text-tertiary)]">{t(LABEL_KEYS[item.key])}</span>
          <span className="truncate">{item.value}</span>
        </span>
      ))}
    </span>
  );
}
