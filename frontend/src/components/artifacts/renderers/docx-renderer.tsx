"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { ChevronLeft, ChevronRight, Download, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { api, apiErrorMessage } from "@/lib/api";
import { API } from "@/lib/constants";
import { base64ToArrayBuffer, downloadBlob } from "@/lib/browser-files";
import { useWorkspaceStore } from "@/stores/workspace-store";

interface DocxRendererProps {
  filePath?: string;
}

export function DocxRenderer({ filePath }: DocxRendererProps) {
  const { t } = useTranslation("chat");
  const workspace = useWorkspaceStore((s) => s.activeWorkspacePath);
  const containerRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string>("");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageCount, setPageCount] = useState(0);
  const blobRef = useRef<Blob | null>(null);

  useEffect(() => {
    if (!filePath) {
      setError(t("noFilePathProvided"));
      setLoading(false);
      return;
    }

    let cancelled = false;

    (async () => {
      try {
        setLoading(true);
        setError(null);
        setCurrentPage(1);
        setPageCount(0);

        const res = await api.post<{
          content_base64: string;
          name: string;
        }>(API.FILES.CONTENT_BINARY, { path: filePath, workspace });

        if (cancelled) return;

        setFileName(res.name);
        const buffer = base64ToArrayBuffer(res.content_base64);
        blobRef.current = new Blob([buffer], {
          type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        });

        // Dynamically import docx-preview to avoid SSR issues
        const { renderAsync } = await import("docx-preview");

        if (cancelled || !containerRef.current) return;

        // Clear previous content
        containerRef.current.innerHTML = "";

        await renderAsync(buffer, containerRef.current, undefined, {
          className: "docx",
          inWrapper: true,
          ignoreWidth: false,
          ignoreHeight: true,
          ignoreFonts: false,
          breakPages: true,
          ignoreLastRenderedPageBreak: true,
          experimental: false,
          trimXmlDeclaration: true,
          useBase64URL: true,
        });
        if (!cancelled && containerRef.current) {
          const pages = containerRef.current.querySelectorAll<HTMLElement>(
            ".docx-wrapper > section.docx",
          );
          setPageCount(Math.max(1, pages.length));
          setCurrentPage(1);
        }
      } catch (err) {
        if (!cancelled) {
          setError(apiErrorMessage(err, t("failedRenderDocument")));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [filePath, workspace, t]);

  const handleDownload = useCallback(() => {
    if (!blobRef.current) return;
    downloadBlob(blobRef.current, fileName || "document.docx");
  }, [fileName]);

  const pageElements = useCallback(() => (
    containerRef.current
      ? Array.from(
          containerRef.current.querySelectorAll<HTMLElement>(
            ".docx-wrapper > section.docx",
          ),
        )
      : []
  ), []);

  const goToPage = useCallback((page: number) => {
    const scroller = scrollRef.current;
    const pages = pageElements();
    if (!scroller || pages.length === 0) return;
    const nextPage = Math.min(Math.max(1, page), pages.length);
    const target = pages[nextPage - 1];
    const targetTop = target.getBoundingClientRect().top
      - scroller.getBoundingClientRect().top
      + scroller.scrollTop;
    scroller.scrollTo({ top: Math.max(0, targetTop - 16), behavior: "smooth" });
    setCurrentPage(nextPage);
  }, [pageElements]);

  const syncCurrentPage = useCallback(() => {
    const scroller = scrollRef.current;
    const pages = pageElements();
    if (!scroller || pages.length === 0) return;
    const marker = scroller.getBoundingClientRect().top + Math.min(120, scroller.clientHeight * 0.25);
    let visiblePage = 1;
    pages.forEach((page, index) => {
      if (page.getBoundingClientRect().top <= marker) visiblePage = index + 1;
    });
    setCurrentPage((previous) => previous === visiblePage ? previous : visiblePage);
  }, [pageElements]);

  if (error) {
    return (
      <div className="flex-1 flex items-center justify-center p-4">
        <p className="text-sm text-[var(--color-destructive)]">{error}</p>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      {/* Toolbar */}
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--border-default)] bg-[var(--surface-tertiary)] px-3 py-2">
        <span className="min-w-0 flex-1 truncate text-[11px] font-medium uppercase tracking-wide text-[var(--text-secondary)]">
          {fileName || "document.docx"}
        </span>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            aria-label={t("docxPreviousPage")}
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            disabled={loading || currentPage <= 1}
            onClick={() => goToPage(currentPage - 1)}
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <span className="min-w-14 text-center text-xs text-[var(--text-secondary)]" aria-live="polite">
            {currentPage} / {Math.max(1, pageCount)}
          </span>
          <Button
            aria-label={t("docxNextPage")}
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            disabled={loading || currentPage >= pageCount}
            onClick={() => goToPage(currentPage + 1)}
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={handleDownload}
          disabled={!blobRef.current}
          title={t("download")}
        >
          <Download className="h-3.5 w-3.5" />
        </Button>
      </div>

      {/* Content */}
      <div ref={scrollRef} onScroll={syncCurrentPage} className="relative min-h-0 flex-1 overflow-auto bg-white">
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-[var(--surface-primary)]">
            <Loader2 className="h-5 w-5 animate-spin text-[var(--text-tertiary)]" />
          </div>
        )}
        <div ref={containerRef} className="docx-preview-container" />
      </div>
    </div>
  );
}
