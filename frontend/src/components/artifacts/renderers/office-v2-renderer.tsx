"use client";

import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ChevronLeft, ChevronRight, Loader2, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { ApiError, api, apiFetch } from "@/lib/api";
import { API } from "@/lib/constants";
import { useChatStore } from "@/stores/chat-store";
import { useWorkspaceStore } from "@/stores/workspace-store";

interface OfficeV2RendererProps {
  filePath: string;
  fallback: ReactNode;
}

interface OfficePreviewContext {
  session_id: string;
  workspace_instance_id: string;
  renderer_available: boolean;
  renderer_id: string;
  renderer_version: string;
  font_digest: string;
  preview_quality: "authoritative" | "approximate";
  formula_values_recalculated: false;
}

interface OfficePreviewPage {
  page_number: number;
  filename: string;
  sha256: string;
  size_bytes: number;
  width_px: number;
  height_px: number;
  mime_type: "image/png";
}

interface OfficePreviewBinding {
  session_id: string;
  workspace_instance_id: string;
  relative_path: string;
  source_sha256: string;
  checkpoint_id: string | null;
  root_turn_id: string | null;
  preview_quality: "authoritative" | "approximate";
  formula_values_recalculated: false;
  manifest: {
    cache_key: string;
    renderer_id: string;
    renderer_version: string;
    font_digest: string;
    quality: "authoritative" | "approximate";
    pages: OfficePreviewPage[];
  };
}

interface OfficeValidationStatus {
  session_id: string;
  workspace_instance_id: string;
  relative_path: string;
  source_sha256: string;
  status: "authoritative_pass" | "stale" | "unvalidated" | "invalid";
  stale_reason: string | null;
}

function officeErrorCode(error: unknown): string | null {
  if (!(error instanceof ApiError) || !error.body || typeof error.body !== "object") return null;
  const code = (error.body as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

function canonicalOfficeRelativePath(filePath: string, workspace: string | null): string | null {
  const source = filePath.replaceAll("\\", "/");
  const root = workspace?.replaceAll("\\", "/").replace(/\/+$/, "") ?? null;
  const absolute = source.startsWith("/") || /^[A-Za-z]:\//.test(source) || source.startsWith("//");
  let relative = source;

  if (absolute) {
    if (!root) return null;
    const windowsPath = /^[A-Za-z]:\//.test(source) && /^[A-Za-z]:\//.test(root);
    const comparableSource = windowsPath ? source.toLocaleLowerCase("en-US") : source;
    const comparableRoot = windowsPath ? root.toLocaleLowerCase("en-US") : root;
    if (!comparableSource.startsWith(`${comparableRoot}/`)) return null;
    relative = source.slice(root.length + 1);
  }

  relative = relative.replace(/^\.\//, "").replace(/\/{2,}/g, "/");
  const parts = relative.split("/");
  if (!relative || parts.some((part) => !part || part === "." || part === "..")) return null;
  if (!/\.(docx|xlsx|pptx)$/i.test(relative)) return null;
  return parts.join("/");
}

function queryString(values: Record<string, string | number>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) query.set(key, String(value));
  return query.toString();
}

export function OfficeV2Renderer({ filePath, fallback }: OfficeV2RendererProps) {
  const { t } = useTranslation("chat");
  const sessionId = useChatStore((state) => state.focusedSessionId);
  const workspace = useWorkspaceStore((state) => state.activeWorkspacePath);
  const relativePath = useMemo(
    () => canonicalOfficeRelativePath(filePath, workspace),
    [filePath, workspace],
  );
  const [binding, setBinding] = useState<OfficePreviewBinding | null>(null);
  const [validation, setValidation] = useState<OfficeValidationStatus | null>(null);
  const [activePage, setActivePage] = useState(1);
  const [pageUrl, setPageUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [pageLoading, setPageLoading] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [useFallback, setUseFallback] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!sessionId || !relativePath) {
      setUseFallback(true);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setBinding(null);
    setValidation(null);
    setFailure(null);
    setUseFallback(false);
    setActivePage(1);

    void (async () => {
      try {
        const context = await api.get<OfficePreviewContext>(
          API.OFFICE_V2.CONTEXT(sessionId),
          { signal: controller.signal },
        );
        if (!context.renderer_available) {
          if (active) setUseFallback(true);
          return;
        }
        const result = await api.post<OfficePreviewBinding>(
          API.OFFICE_V2.RENDER,
          {
            session_id: sessionId,
            workspace_instance_id: context.workspace_instance_id,
            relative_path: relativePath,
          },
          { signal: controller.signal },
        );
        if (!result.manifest.pages.length) throw new Error("office_preview_has_no_pages");
        const validationQuery = queryString({
          session_id: sessionId,
          workspace_instance_id: context.workspace_instance_id,
          relative_path: relativePath,
        });
        const validationResult = await api
          .get<OfficeValidationStatus>(
            `${API.OFFICE_V2.VALIDATION}?${validationQuery}`,
            { signal: controller.signal },
          )
          .catch(() => null);
        if (active) {
          setBinding(result);
          setValidation(validationResult);
        }
      } catch (error) {
        if (!active || controller.signal.aborted) return;
        const code = officeErrorCode(error);
        if (
          code === "v11_office_v2_not_available" ||
          code === "office_renderer_unavailable" ||
          (error instanceof ApiError && (error.status === 403 || error.status === 404))
        ) {
          setUseFallback(true);
        } else {
          setFailure(code ?? "office_preview_failed");
        }
      } finally {
        if (active) setLoading(false);
      }
    })();

    return () => {
      active = false;
      controller.abort();
    };
  }, [attempt, relativePath, sessionId]);

  useEffect(() => {
    if (!binding) return;
    const controller = new AbortController();
    let active = true;
    setPageLoading(true);
    setFailure(null);
    setPageUrl((old) => {
      if (old) URL.revokeObjectURL(old);
      return null;
    });

    const query = queryString({
      session_id: binding.session_id,
      workspace_instance_id: binding.workspace_instance_id,
      relative_path: binding.relative_path,
      cache_key: binding.manifest.cache_key,
      page_number: activePage,
    });
    void apiFetch(`${API.OFFICE_V2.PAGE}?${query}`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          const payload = await response.json().catch(() => null) as { code?: string } | null;
          throw new Error(payload?.code ?? "office_preview_page_failed");
        }
        if (response.headers.get("content-type")?.split(";", 1)[0] !== "image/png") {
          throw new Error("office_preview_page_type_invalid");
        }
        const blob = await response.blob();
        if (blob.size > 128 * 1024 * 1024) throw new Error("office_preview_page_too_large");
        if (!active) return;
        setPageUrl(URL.createObjectURL(blob));
      })
      .catch((error: unknown) => {
        if (!active || controller.signal.aborted) return;
        setFailure(error instanceof Error ? error.message : "office_preview_page_failed");
      })
      .finally(() => {
        if (active) setPageLoading(false);
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [activePage, binding]);

  useEffect(() => () => {
    if (pageUrl) URL.revokeObjectURL(pageUrl);
  }, [pageUrl]);

  if (useFallback) {
    return (
      <div className="flex h-full min-h-0 w-full flex-1 flex-col overflow-hidden">
        <div className="flex items-center gap-2 border-b border-amber-300/50 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          <span>{t("officeApproximatePreviewNotice")}</span>
        </div>
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">{fallback}</div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex flex-1 items-center justify-center gap-2 text-sm text-[var(--text-tertiary)]">
        <Loader2 className="h-5 w-5 animate-spin" />
        <span>{t("officeRenderingPreview")}</span>
      </div>
    );
  }

  if (failure || !binding) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
        <AlertTriangle className="h-6 w-6 text-amber-500" />
        <p className="max-w-sm text-sm text-[var(--text-secondary)]">{t("officePreviewFailedSafely")}</p>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => setAttempt((value) => value + 1)}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            {t("retry")}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setUseFallback(true)}>
            {t("officeUseApproximatePreview")}
          </Button>
        </div>
      </div>
    );
  }

  const pageCount = binding.manifest.pages.length;
  const authoritative = binding.preview_quality === "authoritative";
  return (
    <div className="flex h-full min-h-0 w-full flex-1 flex-col overflow-hidden bg-[var(--surface-secondary)]">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-default)] bg-[var(--surface-primary)] px-3 py-2">
        <div className="flex min-w-0 items-center gap-2 text-[11px]">
          <span className={authoritative ? "font-semibold text-emerald-600" : "font-semibold text-amber-600"}>
            {authoritative ? t("officeAuthoritativePreview") : t("officeApproximateRenderer")}
          </span>
          <span className="truncate text-[var(--text-tertiary)]">
            {binding.manifest.renderer_id} {binding.manifest.renderer_version}
          </span>
          {binding.checkpoint_id && (
            <span className="rounded bg-[var(--surface-tertiary)] px-1.5 py-0.5 text-[var(--text-secondary)]">
              {t("officeCheckpointLinked")} · {binding.checkpoint_id.slice(0, 8)}
            </span>
          )}
          {validation?.status === "authoritative_pass" && (
            <span className="rounded bg-emerald-500/10 px-1.5 py-0.5 font-medium text-emerald-700 dark:text-emerald-300">
              {t("officeValidationCurrent")}
            </span>
          )}
          {validation?.status === "stale" && (
            <span className="rounded bg-amber-500/10 px-1.5 py-0.5 font-medium text-amber-700 dark:text-amber-300">
              {t("officeValidationStale")}
            </span>
          )}
          {validation?.status === "invalid" && (
            <span className="rounded bg-red-500/10 px-1.5 py-0.5 font-medium text-red-700 dark:text-red-300">
              {t("officeValidationInvalid")}
            </span>
          )}
          {binding.manifest.pages[0] && filePath.toLowerCase().endsWith(".xlsx") && (
            <span className="text-[var(--text-tertiary)]">{t("officeFormulaNotRecalculated")}</span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <Button aria-label={t("officePreviousPage")} variant="ghost" size="icon" className="h-7 w-7" disabled={activePage <= 1} onClick={() => setActivePage((page) => Math.max(1, page - 1))}>
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <span className="min-w-14 text-center text-xs text-[var(--text-secondary)]">{activePage} / {pageCount}</span>
          <Button aria-label={t("officeNextPage")} variant="ghost" size="icon" className="h-7 w-7" disabled={activePage >= pageCount} onClick={() => setActivePage((page) => Math.min(pageCount, page + 1))}>
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </div>
      <div className="relative flex min-h-0 flex-1 items-start justify-center overflow-auto p-4">
        {pageLoading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-[var(--surface-primary)]/70">
            <Loader2 className="h-5 w-5 animate-spin text-[var(--text-tertiary)]" />
          </div>
        )}
        {pageUrl && (
          // The backend revalidates the source SHA and complete private cache
          // entry before every authenticated blob fetch.
          // eslint-disable-next-line @next/next/no-img-element
          <img src={pageUrl} alt={t("officePreviewPageAlt", { page: activePage })} className="h-auto max-w-full bg-white shadow-sm ring-1 ring-black/10" />
        )}
      </div>
    </div>
  );
}
