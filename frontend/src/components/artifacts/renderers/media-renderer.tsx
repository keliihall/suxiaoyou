"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Download, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { api, apiErrorMessage } from "@/lib/api";
import { base64ToBlob, downloadBlob } from "@/lib/browser-files";
import { API } from "@/lib/constants";
import { useWorkspaceStore } from "@/stores/workspace-store";

const MAX_MEDIA_PREVIEW_BYTES = 50 * 1024 * 1024;

interface MediaRendererProps {
  filePath?: string;
  kind: "audio" | "video";
}

interface BinaryMediaResponse {
  content_base64: string;
  mime_type: string;
  name: string;
  size: number;
}

function decodedBase64ByteLength(value: string): number {
  const padding = value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0;
  return Math.max(0, Math.floor((value.length * 3) / 4) - padding);
}

export function MediaRenderer({ filePath, kind }: MediaRendererProps) {
  const { t } = useTranslation("chat");
  const workspace = useWorkspaceStore((state) => state.activeWorkspacePath);
  const [mediaUrl, setMediaUrl] = useState<string | null>(null);
  const [fileName, setFileName] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const blobRef = useRef<Blob | null>(null);

  useEffect(() => {
    if (!filePath) {
      setError(t("noFilePathProvided"));
      setLoading(false);
      return;
    }

    let cancelled = false;
    let objectUrl: string | null = null;

    void (async () => {
      try {
        setLoading(true);
        setError(null);
        setMediaUrl(null);
        blobRef.current = null;

        const response = await api.post<BinaryMediaResponse>(
          API.FILES.CONTENT_BINARY,
          { path: filePath, workspace },
        );
        if (cancelled) return;

        if (
          response.size > MAX_MEDIA_PREVIEW_BYTES ||
          decodedBase64ByteLength(response.content_base64) > MAX_MEDIA_PREVIEW_BYTES
        ) {
          throw new Error(t("mediaPreviewTooLarge"));
        }
        if (!response.mime_type.startsWith(`${kind}/`)) {
          throw new Error(t("failedLoadMedia"));
        }

        const blob = base64ToBlob(response.content_base64, response.mime_type);
        if (blob.size > MAX_MEDIA_PREVIEW_BYTES) {
          throw new Error(t("mediaPreviewTooLarge"));
        }

        objectUrl = URL.createObjectURL(blob);
        blobRef.current = blob;
        setFileName(response.name || filePath.split(/[/\\]/).pop() || kind);
        setMediaUrl(objectUrl);
      } catch (cause) {
        if (!cancelled) setError(apiErrorMessage(cause, t("failedLoadMedia")));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      blobRef.current = null;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [filePath, kind, t, workspace]);

  const handleDownload = useCallback(() => {
    if (!blobRef.current) return;
    downloadBlob(blobRef.current, fileName || kind);
  }, [fileName, kind]);

  if (error) {
    return (
      <div className="flex h-full items-center justify-center p-4">
        <p className="text-sm text-[var(--color-destructive)]">{error}</p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-[var(--border-default)] bg-[var(--surface-tertiary)] px-3 py-2">
        <span className="truncate text-[11px] font-medium text-[var(--text-secondary)]">
          {fileName || filePath?.split(/[/\\]/).pop() || kind}
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

      <div className="relative flex flex-1 items-center justify-center overflow-auto bg-[var(--surface-secondary)] p-6">
        {loading && <Loader2 className="h-5 w-5 animate-spin text-[var(--text-tertiary)]" />}
        {mediaUrl && kind === "audio" && (
          <audio
            data-testid="audio-file-preview"
            src={mediaUrl}
            controls
            className="w-full max-w-2xl"
            aria-label={fileName || t("mediaPreview")}
            onError={() => setError(t("failedLoadMedia"))}
          />
        )}
        {mediaUrl && kind === "video" && (
          <video
            data-testid="video-file-preview"
            src={mediaUrl}
            controls
            className="max-h-full max-w-full"
            aria-label={fileName || t("mediaPreview")}
            onError={() => setError(t("failedLoadMedia"))}
          />
        )}
      </div>
    </div>
  );
}
