"use client";

import { useState, useEffect, useRef } from "react";
import { Code, Eye } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { useMermaid } from "@/hooks/use-mermaid";
import { TryFixButton } from "../try-fix-button";

interface MermaidRendererProps {
  content: string;
}

export function MermaidRenderer({ content }: MermaidRendererProps) {
  const { t } = useTranslation("chat");
  const [showSource, setShowSource] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const { renderMermaid, isReady } = useMermaid();

  useEffect(() => {
    if (!isReady || !containerRef.current || showSource) return;

    const render = async () => {
      try {
        setError(null);
        const { svg } = await renderMermaid(content);
        if (containerRef.current) {
          containerRef.current.innerHTML = svg;
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : t("failedRenderDiagram"));
      }
    };

    render();
  }, [content, isReady, renderMermaid, showSource, t]);

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--border-default)] bg-[var(--surface-tertiary)] shrink-0">
        <span className="text-[11px] font-medium text-[var(--text-secondary)] uppercase tracking-wide">
          {showSource ? t("source") : t("diagram")}
        </span>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={() => setShowSource(!showSource)}
          title={showSource ? t("showDiagram") : t("showSource")}
        >
          {showSource ? <Eye className="h-3.5 w-3.5" /> : <Code className="h-3.5 w-3.5" />}
        </Button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-auto p-4">
        {showSource ? (
          <pre className="text-[13px] leading-relaxed font-mono text-[var(--text-primary)]">
            {content}
          </pre>
        ) : error ? (
          <div className="text-sm text-[var(--color-destructive)] p-4">
            {t("diagramRenderingError", { error })}
            <TryFixButton error={error} artifactType={t("mermaidDiagram")} />
          </div>
        ) : !isReady ? (
          <div className="flex items-center justify-center h-full text-sm text-[var(--text-tertiary)]">
            {t("loadingDiagram")}
          </div>
        ) : (
          <div
            ref={containerRef}
            className="flex items-center justify-center min-h-full [&_svg]:max-w-full"
          />
        )}
      </div>
    </div>
  );
}
