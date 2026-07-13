"use client";

import { useState, useEffect, useRef, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChevronRight, CheckCircle2, XCircle, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { SuxiaoyouLogo } from "@/components/ui/suxiaoyou-logo";
import { motion, AnimatePresence } from "framer-motion";
import { cn } from "@/lib/utils";
import { getToolDisplayTitle, localizeVisibleProcessText } from "@/lib/activity-labels";
import { formatElapsedDuration, formatElapsedMilliseconds } from "@/lib/duration";
import type { ToolPart } from "@/types/message";

interface ReasoningPartProps {
  /** Combined reasoning texts from all reasoning parts in this message. */
  texts: string[];
  /** Tool parts to display inside the thinking section. */
  toolParts?: ToolPart[];
  isStreaming?: boolean;
  /** True only while the stream store has an unresolved user prompt. */
  isAwaitingConfirmation?: boolean;
  /** Callback when thinking duration changes (for ActivitySummary). */
  onDurationChange?: (seconds: number) => void;
  /** Stable task origin supplied by the per-session stream store. */
  startedAt?: number | null;
}

export function ReasoningPart({
  texts,
  toolParts = [],
  isStreaming,
  isAwaitingConfirmation = false,
  onDurationChange,
  startedAt,
}: ReasoningPartProps) {
  const { t, i18n } = useTranslation("chat");
  const [isOpen, setIsOpen] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startRef = useRef<number | null>(null);

  // Live timer — ticks every second while streaming
  useEffect(() => {
    if (isStreaming) {
      if (!intervalRef.current) {
        startRef.current = startedAt ?? startRef.current ?? Date.now();
        setElapsed(
          Math.max(0, Math.round((Date.now() - startRef.current) / 1000)),
        );
        intervalRef.current = setInterval(() => {
          const secs = Math.round((Date.now() - (startRef.current ?? Date.now())) / 1000);
          setElapsed(secs);
        }, 1000);
      }
    } else {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      // Notify parent of final duration
      if (startRef.current !== null) {
        const finalSecs = Math.round((Date.now() - startRef.current) / 1000);
        setElapsed(finalSecs);
        onDurationChange?.(finalSecs);
        startRef.current = null;
      }
    }
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [isStreaming, onDurationChange, startedAt]);

  // Determine label based on current activity
  const hasRunningTool = toolParts.some((t) => t.state.status === "running");
  const lastRunningTool = [...toolParts].reverse().find((t) => t.state.status === "running");

  let label: string;
  if (isStreaming) {
    const timer = elapsed > 0
      ? ` ${formatElapsedDuration(elapsed, i18n.language)}`
      : "";
    if (isAwaitingConfirmation) {
      label = `${t("stageWaitingForConfirmation")}${timer}`;
    } else if (hasRunningTool && lastRunningTool) {
      label = `${t("stageWorkingWithTools")}${timer}`;
    } else {
      label = `${t("stageThinking")}${timer}`;
    }
  } else {
    label = elapsed > 0
      ? t("thoughtFor", {
          duration: formatElapsedDuration(elapsed, i18n.language),
        })
      : t("reasoning");
  }

  const combinedText = localizeVisibleProcessText(
    texts.filter(Boolean).join("\n\n---\n\n"),
    i18n.language,
  );

  const mdComponents = useMemo(
    () => ({
      code: ({ children, ...props }: React.HTMLAttributes<HTMLElement>) => (
        <code
          className="rounded bg-[var(--surface-tertiary)] px-1 py-0.5 text-[0.85em] font-mono"
          {...props}
        >
          {children}
        </code>
      ),
    }),
    [],
  );

  if (!combinedText && toolParts.length === 0) return null;

  return (
    <div>
      {/* Trigger */}
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 text-xs text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors py-1"
      >
        <ChevronRight
          className={cn(
            "h-3 w-3 transition-transform duration-200",
            isOpen && "rotate-90",
          )}
        />
        <SuxiaoyouLogo size={14} className={cn(isStreaming && "shimmer-icon")} />
        <span className={cn(isStreaming && "shimmer-text")}>{label}</span>
      </button>

      {/* Collapsible content with framer-motion */}
      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{
              height: { type: "spring", stiffness: 350, damping: 30 },
              opacity: { duration: 0.25, delay: 0.05 },
            }}
            className="overflow-hidden"
          >
            <div className="border-l-2 border-[var(--border-heavy)] pl-4 ml-1 mt-1.5 mb-1 space-y-2">
              {/* Reasoning text */}
              {combinedText && (
                <div className="prose prose-sm max-w-none text-[var(--text-secondary)] leading-relaxed [&_p]:my-2 [&_h1]:text-sm [&_h2]:text-sm [&_h3]:text-sm [&_strong]:text-[var(--text-primary)] [&_hr]:border-[var(--border-default)] [&_hr]:my-3">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                    {combinedText}
                  </ReactMarkdown>
                </div>
              )}

              {/* Tool calls inside thinking */}
              {toolParts.length > 0 && (
                <div className="space-y-1">
                  {toolParts.map((tool) => (
                    <ToolLine key={tool.call_id} tool={tool} />
                  ))}
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/** Compact tool line inside the thinking section */
function ToolLine({ tool }: { tool: ToolPart }) {
  const { t, i18n } = useTranslation("chat");
  const isRunning = tool.state.status === "running" || tool.state.status === "pending";
  const isError = tool.state.status === "error";
  const label = getToolLabel(tool, t, i18n.language);

  let elapsed = "";
  if (tool.state.time_start && tool.state.time_end) {
    const ms =
      new Date(tool.state.time_end).getTime() -
      new Date(tool.state.time_start).getTime();
    elapsed = formatElapsedMilliseconds(ms, i18n.language);
  }

  return (
    <div className="flex items-center gap-1.5 text-xs text-[var(--text-tertiary)]">
      {isRunning ? (
        <Loader2 className="h-3 w-3 animate-spin" />
      ) : isError ? (
        <XCircle className="h-3 w-3 text-[var(--tool-error)]" />
      ) : (
        <CheckCircle2 className="h-3 w-3 text-[var(--tool-completed)]" />
      )}
      <span className={cn(isRunning && "shimmer-text")}>{label}</span>
      {elapsed && <span className="text-[10px]">{elapsed}</span>}
    </div>
  );
}

/** Generate a short label for a tool call */
function getToolLabel(tool: ToolPart, t: TFunction, language: string): string {
  return getToolDisplayTitle(tool, t, language);
}
