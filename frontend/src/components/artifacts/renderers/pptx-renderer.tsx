"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Download, FileWarning, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api, apiErrorMessage } from "@/lib/api";
import { base64ToArrayBuffer, downloadBlob } from "@/lib/browser-files";
import { API } from "@/lib/constants";
import { useWorkspaceStore } from "@/stores/workspace-store";

interface PptxRendererProps {
  filePath?: string;
}

/**
 * Safe PPTX fallback used until a redistributable slide renderer is selected.
 *
 * The desktop app can still read PPTX content through the backend and lets the
 * user download/open the original presentation. Rendering arbitrary Office
 * XML in the webview is intentionally left to a separately reviewed library.
 */
export function PptxRenderer({ filePath }: PptxRendererProps) {
  const { t } = useTranslation("chat");
  const workspace = useWorkspaceStore((state) => state.activeWorkspacePath);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fileName, setFileName] = useState("presentation.pptx");
  const blobRef = useRef<Blob | null>(null);

  useEffect(() => {
    if (!filePath) {
      setError(t("pptxMissingPath"));
      setLoading(false);
      return;
    }

    let cancelled = false;
    void (async () => {
      try {
        setLoading(true);
        setError(null);
        const response = await api.post<{
          content_base64: string;
          name: string;
        }>(API.FILES.CONTENT_BINARY, { path: filePath, workspace });
        if (cancelled) return;

        const buffer = base64ToArrayBuffer(response.content_base64);
        blobRef.current = new Blob([buffer], {
          type: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        });
        setFileName(response.name || "presentation.pptx");
      } catch (cause) {
        if (!cancelled) {
          setError(apiErrorMessage(cause, t("pptxLoadFailed")));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [filePath, t, workspace]);

  const handleDownload = useCallback(() => {
    if (blobRef.current) downloadBlob(blobRef.current, fileName);
  }, [fileName]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-[var(--border-default)] bg-[var(--surface-tertiary)] px-3 py-2">
        <span className="truncate text-[11px] font-medium uppercase tracking-wide text-[var(--text-secondary)]">
          {fileName}
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

      <div className="flex flex-1 items-center justify-center p-6">
        {loading ? (
          <Loader2 className="h-5 w-5 animate-spin text-[var(--text-tertiary)]" />
        ) : error ? (
          <p className="text-sm text-[var(--color-destructive)]">{error}</p>
        ) : (
          <div className="max-w-md text-center">
            <FileWarning className="mx-auto mb-3 h-8 w-8 text-[var(--text-tertiary)]" />
            <p className="text-sm font-medium text-[var(--text-primary)]">
              {t("pptxPreviewUnavailable")}
            </p>
            <p className="mt-1 text-xs leading-5 text-[var(--text-secondary)]">
              {t("pptxOpenExternally")}
            </p>
            <Button className="mt-4" onClick={handleDownload} disabled={!blobRef.current}>
              <Download className="mr-2 h-4 w-4" />
              {t("download")}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
