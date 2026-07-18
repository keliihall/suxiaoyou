"use client";

import { useMemo, useState } from "react";
import {
  X,
  CheckCircle2,
  CircleAlert,
  CircleMinus,
  ChevronDown,
  Loader2,
  RefreshCw,
  FileText,
  Play,
  Search,
  Pencil,
  FolderSearch,
  Globe,
  HelpCircle,
  ListTodo,
  Layers,
  FileDiff,
  Plug,
} from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { SuxiaoyouLogo } from "@/components/ui/suxiaoyou-logo";
import { IS_DESKTOP, TITLE_BAR_HEIGHT } from "@/lib/constants";
import { useIsDesktop, useIsMacOS } from "@/hooks/use-platform";
import {
  Sheet,
  SheetContent,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import * as VisuallyHidden from "@radix-ui/react-visually-hidden";
import { useActivityStore, computeDuration, type ChainItem } from "@/stores/activity-store";
import { ACTIVITY_PANEL_WIDTH } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { extractSourcesFromTool } from "@/lib/sources";
import {
  getToolDisplayTitle,
  localizeVisibleProcessText,
  translatePersistedToolOutput,
} from "@/lib/activity-labels";
import { formatElapsedDuration, formatElapsedMilliseconds } from "@/lib/duration";
import type { ToolPart, StepFinishPart } from "@/types/message";
import { ToolMetadataSummary } from "@/components/parts/tool-metadata-summary";
import {
  hasRunningActivityTools,
  isActivityComplete,
  isActivityFailed,
} from "@/lib/activity-state";

// -- Helpers --

const TOOL_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  read: FileText,
  write: FileText,
  edit: Pencil,
  multiedit: Pencil,
  apply_patch: FileDiff,
  bash: Play,
  glob: FolderSearch,
  grep: Search,
  web_fetch: Globe,
  web_search: Globe,
  question: HelpCircle,
  todo: ListTodo,
  task: Layers,
};

function getElapsed(tool: ToolPart, language: string): string {
  if (!tool.state.time_start || !tool.state.time_end) return "";
  const ms =
    new Date(tool.state.time_end).getTime() -
    new Date(tool.state.time_start).getTime();
  return formatElapsedMilliseconds(ms, language);
}

// -- Timeline item types --

type TimelineItem =
  | { kind: "thinking-group"; texts: string[] }
  | { kind: "tool"; tool: ToolPart }
  | { kind: "adjustment-group"; tools: ToolPart[] };

function buildTimelineItems(
  chain: ChainItem[],
  collapseRecoveredIssues: boolean,
): TimelineItem[] {
  const items: TimelineItem[] = [];
  let thinkingBuf: string[] = [];
  let issueBuf: ToolPart[] = [];

  const flushThinking = () => {
    if (thinkingBuf.length > 0) {
      items.push({ kind: "thinking-group", texts: [...thinkingBuf] });
      thinkingBuf = [];
    }
  };

  const flushIssues = () => {
    if (issueBuf.length >= 2) {
      items.push({ kind: "adjustment-group", tools: [...issueBuf] });
    } else if (issueBuf.length === 1) {
      items.push({ kind: "tool", tool: issueBuf[0] });
    }
    issueBuf = [];
  };

  for (const c of chain) {
    if (c.type === "reasoning") {
      flushIssues();
      thinkingBuf.push(c.text);
    } else {
      flushThinking();
      if (collapseRecoveredIssues && c.data.state.status === "error") {
        issueBuf.push(c.data);
      } else {
        flushIssues();
        items.push({ kind: "tool", tool: c.data });
      }
    }
  }
  flushThinking();
  flushIssues();

  return items;
}

// -- Sub-components --

/** A thinking group in the chain — shows reasoning bullets expanded by default */
function ThinkingGroup({ texts }: { texts: string[] }) {
  const { t, i18n } = useTranslation("chat");
  const combined = localizeVisibleProcessText(texts.filter(Boolean).join("\n"), i18n.language);
  const thoughts = combined
    .split(/\n/)
    .map((s) => s.trim())
    .filter(Boolean);

  const VISIBLE_COUNT = 5;
  const [showAll, setShowAll] = useState(false);
  const hasMore = thoughts.length > VISIBLE_COUNT;
  const visibleThoughts = showAll ? thoughts : thoughts.slice(0, VISIBLE_COUNT);
  const isEmpty = thoughts.length === 0;

  return (
    <div className="relative pl-7">
      {/* Timeline dot */}
      <div className="absolute left-0 top-0.5 flex items-center justify-center">
        <SuxiaoyouLogo size={14} className="text-[var(--text-secondary)]" />
      </div>

      <p className="text-[13px] font-semibold text-[var(--text-primary)]">
        {t("thinking")}
      </p>

      <div className="mt-1.5 space-y-1">
        {isEmpty ? (
          <p className="text-xs text-[var(--text-tertiary)] italic">
            {t("analyzingRequest")}
          </p>
        ) : (
          visibleThoughts.map((thought, i) => (
            <div key={i} className="flex items-start gap-2">
              <span className="mt-1.5 h-1 w-1 rounded-full bg-[var(--text-tertiary)] shrink-0" />
              <p className="text-xs text-[var(--text-secondary)] leading-relaxed">
                {thought}
              </p>
            </div>
          ))
        )}

        {hasMore && (
          <button
            type="button"
            onClick={() => setShowAll(!showAll)}
            className="text-[11px] text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors ml-3"
          >
            {showAll ? t("showLess") : t("showMore", { count: thoughts.length - VISIBLE_COUNT })}
          </button>
        )}
      </div>
    </div>
  );
}

/** Individual tool row in the timeline */
function ToolRow({
  tool,
  terminal = false,
  activityFailed = false,
  nested = false,
}: {
  tool: ToolPart;
  terminal?: boolean;
  activityFailed?: boolean;
  nested?: boolean;
}) {
  const { t, i18n } = useTranslation("chat");
  const [isOpen, setIsOpen] = useState(false);
  const ToolIcon = TOOL_ICONS[tool.tool] ?? Plug;
  const wasLeftRunning =
    tool.state.status === "running" || tool.state.status === "pending";
  const isRunning = wasLeftRunning && !terminal;
  const isIssue = tool.state.status === "error";
  const isUnreported = terminal && wasLeftRunning;
  const issueLabel = isIssue
    ? terminal
      ? activityFailed
        ? t("toolAttemptIncomplete")
        : t("toolAttemptRecovered")
      : t("toolAttemptNeedsAdjustment")
    : isUnreported
      ? t("toolResultNotReturned")
      : "";
  const presentationStatus = isRunning
    ? "running"
    : isIssue
      ? terminal && !activityFailed
        ? "adjusted"
        : "needs-adjustment"
      : isUnreported
        ? "unreported"
        : "completed";
  const elapsed = getElapsed(tool, i18n.language);
  const title = getToolDisplayTitle(tool, t, i18n.language);
  const output = translatePersistedToolOutput(
    tool.tool,
    tool.state.output,
    i18n.language,
  );

  // Source badges for web tools
  const sources = useMemo(() => {
    if (tool.tool !== "web_search" && tool.tool !== "web_fetch") return [];
    return extractSourcesFromTool(tool);
  }, [tool]);

  const MAX_VISIBLE_SOURCES = 3;
  const visibleSources = sources.slice(0, MAX_VISIBLE_SOURCES);
  const moreCount = sources.length - MAX_VISIBLE_SOURCES;

  return (
    <div
      className={cn("relative", nested ? "pl-6" : "pl-7")}
      data-activity-status={presentationStatus}
    >
      {/* Timeline dot */}
      <div className="absolute left-0 top-1 flex items-center justify-center">
        {isRunning ? (
          <Loader2 aria-hidden="true" className="h-3.5 w-3.5 animate-spin text-[var(--text-tertiary)]" />
        ) : isIssue ? (
          <CircleAlert aria-hidden="true" className="h-3.5 w-3.5 text-[var(--tool-adjusted)]" />
        ) : isUnreported ? (
          <CircleMinus aria-hidden="true" className="h-3.5 w-3.5 text-[var(--text-tertiary)]" />
        ) : (
          <CheckCircle2 aria-hidden="true" className="h-3.5 w-3.5 text-[var(--tool-completed)]" />
        )}
      </div>

      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        aria-expanded={isOpen}
        className="w-full text-left group flex items-center gap-2"
      >
        <ToolIcon className="h-3.5 w-3.5 text-[var(--text-tertiary)] shrink-0" />
        <span className="flex-1 text-xs text-[var(--text-secondary)] truncate group-hover:text-[var(--text-primary)] transition-colors">
          {title}
        </span>
        {issueLabel && (
          <span
            className={cn(
              "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] leading-none",
              isIssue
                ? "bg-[var(--tool-adjusted)]/10 text-[var(--tool-adjusted)]"
                : "bg-[var(--surface-secondary)] text-[var(--text-tertiary)]",
            )}
          >
            {issueLabel}
          </span>
        )}
        {elapsed && (
          <span className="text-[10px] text-[var(--text-tertiary)] shrink-0">
            {elapsed}
          </span>
        )}
        <ChevronDown
          className={cn(
            "h-3 w-3 text-[var(--text-tertiary)] transition-transform duration-200 shrink-0",
            isOpen && "rotate-180",
          )}
        />
      </button>

      <ToolMetadataSummary
        metadata={tool.state.metadata}
        className="ml-5 mt-1.5"
      />

      {/* Source badges for web tools */}
      {visibleSources.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mt-1.5 ml-5">
          {visibleSources.map((source) => (
            <span
              key={source.url}
              className="inline-flex items-center gap-1 rounded-full bg-[var(--surface-secondary)] border border-[var(--border-default)] px-2 py-0.5 text-[10px] text-[var(--text-tertiary)]"
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={source.favicon} alt="" className="h-3 w-3 rounded-sm" />
              <span className="truncate max-w-[100px]">{source.domain}</span>
            </span>
          ))}
          {moreCount > 0 && (
            <span className="text-[10px] text-[var(--text-tertiary)] self-center">
              {t("moreItems", { count: moreCount })}
            </span>
          )}
        </div>
      )}

      {/* Expandable detail (input/output) */}
      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: "easeInOut" }}
            className="overflow-hidden"
          >
            <div className="ml-5 mt-1.5 mb-1 rounded-lg border border-[var(--border-default)] bg-[var(--surface-secondary)] overflow-hidden">
              {Object.keys(tool.state.input).length > 0 && (
                <div className="border-b border-[var(--border-default)]">
                  <p className="px-3 py-1 text-[10px] font-semibold text-[var(--text-tertiary)] uppercase tracking-wider bg-[var(--surface-tertiary)]">
                    {t("input")}
                  </p>
                  <pre className="p-2 text-[11px] text-[var(--text-secondary)] overflow-x-auto font-mono leading-relaxed max-h-[150px] overflow-y-auto">
                    {JSON.stringify(tool.state.input, null, 2)}
                  </pre>
                </div>
              )}
              {output && (
                <div>
                  <p className={cn(
                    "px-3 py-1 text-[10px] font-semibold uppercase tracking-wider bg-[var(--surface-tertiary)]",
                    isIssue
                      ? "text-[var(--tool-adjusted)]"
                      : isUnreported
                        ? "text-[var(--text-tertiary)]"
                        : "text-[var(--tool-completed)]",
                  )}>
                    {isIssue || isUnreported ? t("details") : t("output")}
                  </p>
                  <pre className="p-2 text-[11px] text-[var(--text-secondary)] overflow-x-auto font-mono leading-relaxed max-h-[200px] overflow-y-auto">
                    {output.length > 3000
                      ? output.slice(0, 3000) + "\n" + t("truncated")
                      : output}
                  </pre>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/** Consecutive recoverable misses are one route adjustment, not many failures. */
function AdjustmentGroup({ tools }: { tools: ToolPart[] }) {
  const { t } = useTranslation("chat");
  const [isOpen, setIsOpen] = useState(false);

  return (
    <div className="relative pl-7" data-activity-status="adjusted">
      <div className="absolute left-0 top-1 flex items-center justify-center">
        <RefreshCw
          aria-hidden="true"
          className="h-3.5 w-3.5 text-[var(--tool-adjusted)]"
        />
      </div>

      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        aria-expanded={isOpen}
        className="group flex w-full items-center gap-2 text-left"
      >
        <span className="flex-1 text-xs font-medium text-[var(--text-secondary)] transition-colors group-hover:text-[var(--text-primary)]">
          {t("activityAdjustmentGroup", { count: tools.length })}
        </span>
        <ChevronDown
          aria-hidden="true"
          className={cn(
            "h-3 w-3 shrink-0 text-[var(--text-tertiary)] transition-transform duration-200",
            isOpen && "rotate-180",
          )}
        />
      </button>
      <p className="mt-1 text-[11px] leading-relaxed text-[var(--text-tertiary)]">
        {t("activityAdjustmentGroupDesc")}
      </p>

      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: "easeInOut" }}
            className="overflow-hidden"
          >
            <div className="mt-3 space-y-3 border-l border-[var(--border-default)] pl-3">
              {tools.map((tool) => (
                <ToolRow
                  key={tool.call_id}
                  tool={tool}
                  terminal
                  nested
                />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// -- Main panel content --

function ActivityPanelContent() {
  const { t, i18n } = useTranslation("chat");
  const activeData = useActivityStore((s) => s.activeData);
  const close = useActivityStore((s) => s.close);
  const isComplete = activeData ? isActivityComplete(activeData) : false;
  const activityFailed = isComplete && activeData
    ? isActivityFailed(activeData)
    : false;

  const timelineItems = useMemo(
    () => activeData?.chain
      ? buildTimelineItems(activeData.chain, isComplete && !activityFailed)
      : [],
    [activeData, activityFailed, isComplete],
  );

  if (!activeData) return null;

  // Aggregate metrics from step-finish parts
  const stepFinishes = activeData.stepParts.filter(
    (p): p is StepFinishPart => p.type === "step-finish",
  );
  const totalTokens = stepFinishes.reduce((acc, sf) => {
    const tokens = sf.tokens || {};
    return {
      input: acc.input + (tokens.input || 0),
      output: acc.output + (tokens.output || 0),
      reasoning: acc.reasoning + (tokens.reasoning || 0),
      cacheRead: acc.cacheRead + (tokens.cache_read || 0),
    };
  }, { input: 0, output: 0, reasoning: 0, cacheRead: 0 });
  const totalContextTokens = totalTokens.input
    + totalTokens.output
    + totalTokens.reasoning
    + totalTokens.cacheRead;
  const totalCost = stepFinishes.reduce((acc, sf) => acc + (sf.cost || 0), 0);
  const hasMetrics = totalContextTokens > 0 || totalCost > 0;

  // Compute total duration
  const duration = computeDuration(activeData);
  const durationLabel = duration != null && duration > 0
    ? formatElapsedDuration(duration, i18n.language)
    : "";
  const hasRunningTools = hasRunningActivityTools(activeData);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between h-12 px-4 shrink-0">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-[var(--text-primary)]">
            {t("activity")}
          </h2>
          {durationLabel && (
            <span className="text-[11px] text-[var(--text-tertiary)]">
              · {durationLabel}
            </span>
          )}
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={close}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>

      {/* Scrollable chain timeline */}
      <div className="flex-1 overflow-y-auto px-4 py-4 scrollbar-auto">
        <div className="relative space-y-4">
          {timelineItems.map((item, i) => {
            if (item.kind === "thinking-group") {
              return (
                <ThinkingGroup key={`thinking-${i}`} texts={item.texts} />
              );
            }
            if (item.kind === "adjustment-group") {
              return (
                <AdjustmentGroup
                  key={`adjustment-${item.tools[0]?.call_id ?? i}`}
                  tools={item.tools}
                />
              );
            }
            return (
              <ToolRow
                key={`tool-${item.tool.call_id}-${i}`}
                tool={item.tool}
                terminal={isComplete}
                activityFailed={activityFailed}
              />
            );
          })}

          {activityFailed ? (
            <div className="relative pl-7" role="status" data-activity-status="failed">
              <div className="absolute left-0 top-0.5 flex items-center justify-center">
                <CircleAlert
                  aria-hidden="true"
                  className="h-3.5 w-3.5 text-[var(--color-destructive)]"
                />
              </div>
              {durationLabel && (
                <p className="text-[11px] text-[var(--text-tertiary)]">
                  {t("thoughtFor", { duration: durationLabel })}
                </p>
              )}
              <p className="text-[13px] font-medium text-[var(--color-destructive)]">
                {t("activityNotCompleted")}
              </p>
            </div>
          ) : isComplete ? (
            <div className="relative pl-7" data-activity-status="completed">
              <div className="absolute left-0 top-0.5 flex items-center justify-center">
                <CheckCircle2 aria-hidden="true" className="h-3.5 w-3.5 text-[var(--tool-completed)]" />
              </div>
              {durationLabel && (
                <p className="text-[11px] text-[var(--text-tertiary)]">
                  {t("thoughtFor", { duration: durationLabel })}
                </p>
              )}
              <p className="text-[13px] font-medium text-[var(--text-secondary)]">{t("done")}</p>
            </div>
          ) : (
            <div className="relative pl-7">
              <div className="absolute left-0 top-0.5 flex items-center justify-center">
                <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--text-tertiary)]" />
              </div>
              <p className="text-[13px] font-medium text-[var(--text-secondary)]">
                {activeData.isAwaitingConfirmation
                  ? t("stageWaitingForConfirmation")
                  : hasRunningTools
                    ? t("stageWorkingWithTools")
                    : t("stageFinalizing")}
              </p>
            </div>
          )}
        </div>

        {/* Metrics */}
        {hasMetrics && (
          <div className="pt-4 mt-6">
            <h3 className="text-xs font-medium text-[var(--text-tertiary)] uppercase tracking-wider mb-2">
              {t("currentResponseMetrics")}
            </h3>
            <p className="mb-2 text-[10px] leading-relaxed text-[var(--text-quaternary)]">
              {t("currentResponseMetricsScope")}
            </p>
            <div className="grid grid-cols-2 gap-2 text-xs">
              {totalContextTokens > 0 && (
                <div className="col-span-2 flex justify-between rounded-lg bg-[var(--surface-secondary)] px-3 py-2">
                  <span className="text-[var(--text-tertiary)]">
                    {t("totalContextTokens")}
                  </span>
                  <span className="text-[var(--text-secondary)] font-mono">
                    {totalContextTokens.toLocaleString(i18n.language)}
                  </span>
                </div>
              )}
              {totalContextTokens > 0 && (
                <div className="flex justify-between rounded-lg bg-[var(--surface-secondary)] px-3 py-2">
                  <span className="text-[var(--text-tertiary)]">{t("uncachedInput")}</span>
                  <span className="text-[var(--text-secondary)] font-mono">
                    {totalTokens.input.toLocaleString(i18n.language)}
                  </span>
                </div>
              )}
              {totalContextTokens > 0 && (
                <div className="flex justify-between rounded-lg bg-[var(--surface-secondary)] px-3 py-2">
                  <span className="text-[var(--text-tertiary)]">{t("output")}</span>
                  <span className="text-[var(--text-secondary)] font-mono">
                    {totalTokens.output.toLocaleString(i18n.language)}
                  </span>
                </div>
              )}
              {totalContextTokens > 0 && (
                <div className="flex justify-between rounded-lg bg-[var(--surface-secondary)] px-3 py-2">
                  <span className="text-[var(--text-tertiary)]">{t("reasoning")}</span>
                  <span className="text-[var(--text-secondary)] font-mono">
                    {totalTokens.reasoning.toLocaleString(i18n.language)}
                  </span>
                </div>
              )}
              {totalContextTokens > 0 && (
                <div className="flex justify-between rounded-lg bg-[var(--surface-secondary)] px-3 py-2">
                  <span className="text-[var(--text-tertiary)]">{t("cacheRead")}</span>
                  <span className="text-[var(--text-secondary)] font-mono">
                    {totalTokens.cacheRead.toLocaleString(i18n.language)}
                  </span>
                </div>
              )}
              {totalCost > 0 && (
                <div className="flex justify-between rounded-lg bg-[var(--surface-secondary)] px-3 py-2 col-span-2">
                  <span className="text-[var(--text-tertiary)]">{t("cost")}</span>
                  <span className="text-[var(--text-secondary)] font-mono">
                    ${totalCost.toFixed(totalCost < 0.01 ? 4 : 2)}
                  </span>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export function ActivityPanel() {
  const { t } = useTranslation("chat");
  const isOpen = useActivityStore((s) => s.isOpen);
  const close = useActivityStore((s) => s.close);
  const isDesktop = useIsDesktop();
  const isMac = useIsMacOS();
  const topOffset = IS_DESKTOP && !isMac ? TITLE_BAR_HEIGHT : 0;

  // Desktop: fixed right panel with smooth mount/unmount
  if (isDesktop) {
    return (
      <motion.aside
        className="fixed inset-y-0 right-0 z-[35] flex flex-col bg-[var(--surface-primary)] overflow-hidden"
        style={{ width: ACTIVITY_PANEL_WIDTH, top: topOffset }}
        initial={{ x: "100%" }}
        animate={{ x: 0 }}
        exit={{ x: "100%" }}
        transition={{ type: "spring", damping: 30, stiffness: 300 }}
      >
        <ActivityPanelContent />
      </motion.aside>
    );
  }

  // Mobile: Sheet overlay from right
  return (
    <Sheet open={isOpen} onOpenChange={(open) => !open && close()}>
      <SheetContent side="right" className="w-[85vw] sm:max-w-[380px] p-0">
        <VisuallyHidden.Root asChild>
          <SheetTitle>{t("activity")}</SheetTitle>
        </VisuallyHidden.Root>
        <VisuallyHidden.Root asChild>
          <SheetDescription>{t("activityDesc")}</SheetDescription>
        </VisuallyHidden.Root>
        <ActivityPanelContent />
      </SheetContent>
    </Sheet>
  );
}
