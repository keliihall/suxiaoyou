"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { Download, Loader2 } from "lucide-react";
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string>("");
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
          className: "docx-preview-wrapper",
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

  if (error) {
    return (
      <div className="flex-1 flex items-center justify-center p-4">
        <p className="text-sm text-[var(--color-destructive)]">{error}</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-[var(--border-default)] bg-[var(--surface-tertiary)] shrink-0">
        <span className="text-[11px] font-medium text-[var(--text-secondary)] uppercase tracking-wide">
          {fileName || "document.docx"}
        </span>
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
      <div className="flex-1 overflow-auto bg-white relative">
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
