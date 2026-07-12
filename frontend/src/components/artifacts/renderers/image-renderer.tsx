"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Download, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { api, apiErrorMessage } from "@/lib/api";
import { base64ToBlob, downloadBlob } from "@/lib/browser-files";
import { API } from "@/lib/constants";
import { useWorkspaceStore } from "@/stores/workspace-store";

const MAX_RASTER_IMAGE_PREVIEW_BYTES = 50 * 1024 * 1024;

const RASTER_IMAGE_MIME_TYPES: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  bmp: "image/bmp",
};

interface ImageRendererProps {
  filePath?: string;
}

interface BinaryImageResponse {
  content_base64: string;
  name: string;
  size: number;
}

function mimeTypeForPath(filePath: string): string | null {
  const extension = filePath.split(".").pop()?.toLowerCase() ?? "";
  return RASTER_IMAGE_MIME_TYPES[extension] ?? null;
}

function decodedBase64ByteLength(value: string): number {
  const padding = value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0;
  return Math.max(0, Math.floor((value.length * 3) / 4) - padding);
}

export function ImageRenderer({ filePath }: ImageRendererProps) {
  const { t } = useTranslation("chat");
  const workspace = useWorkspaceStore((state) => state.activeWorkspacePath);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
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

    const mimeType = mimeTypeForPath(filePath);
    if (!mimeType) {
      setError(t("failedLoadImage"));
      setLoading(false);
      return;
    }

    let cancelled = false;
    let objectUrl: string | null = null;

    void (async () => {
      try {
        setLoading(true);
        setError(null);
        setImageUrl(null);
        blobRef.current = null;

        const response = await api.post<BinaryImageResponse>(
          API.FILES.CONTENT_BINARY,
          { path: filePath, workspace },
        );
        if (cancelled) return;

        if (
          response.size > MAX_RASTER_IMAGE_PREVIEW_BYTES ||
          decodedBase64ByteLength(response.content_base64) > MAX_RASTER_IMAGE_PREVIEW_BYTES
        ) {
          throw new Error(t("imagePreviewTooLarge"));
        }

        const blob = base64ToBlob(response.content_base64, mimeType);
        if (blob.size > MAX_RASTER_IMAGE_PREVIEW_BYTES) {
          throw new Error(t("imagePreviewTooLarge"));
        }

        objectUrl = URL.createObjectURL(blob);
        blobRef.current = blob;
        setFileName(response.name || filePath.split(/[/\\]/).pop() || "image");
        setImageUrl(objectUrl);
      } catch (cause) {
        if (!cancelled) {
          setError(apiErrorMessage(cause, t("failedLoadImage")));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      blobRef.current = null;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [filePath, t, workspace]);

  const handleDownload = useCallback(() => {
    if (!blobRef.current) return;
    downloadBlob(blobRef.current, fileName || "image");
  }, [fileName]);

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
          {fileName || filePath?.split(/[/\\]/).pop() || "image"}
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

      <div className="relative flex flex-1 items-center justify-center overflow-auto bg-[var(--surface-secondary)] p-4">
        {loading && (
          <Loader2 className="h-5 w-5 animate-spin text-[var(--text-tertiary)]" />
        )}
        {imageUrl && (
          // Blob URLs deliberately bypass Next.js image optimization so animated
          // GIFs remain animated and no preview bytes leave the current device.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            data-testid="raster-image-preview"
            src={imageUrl}
            alt={fileName || t("imagePreview")}
            className="max-h-full max-w-full object-contain"
            onError={() => setError(t("failedLoadImage"))}
          />
        )}
      </div>
    </div>
  );
}
