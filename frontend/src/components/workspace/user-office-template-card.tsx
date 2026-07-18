"use client";

import {
  type FormEvent,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Copy,
  Eye,
  FileStack,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, api, apiErrorMessage, apiFetch } from "@/lib/api";
import { API } from "@/lib/constants";
import { cn } from "@/lib/utils";

const MAX_TEMPLATE_BYTES = 75 * 1024 * 1024;
const MAX_PREVIEW_BYTES = 128 * 1024 * 1024;
const ALLOWED_TEMPLATE_SUFFIXES = new Set(["docx", "xlsx", "pptx"]);
const EXAMPLE_PLACEHOLDER_SCHEMA = JSON.stringify(
  [
    {
      name: "title",
      type: "text",
      required: true,
      min_chars: 1,
      max_chars: 120,
      description: "Document title",
    },
  ],
  null,
  2,
);

interface RuntimeContext {
  workspace_instance_id: string;
}

interface UserTemplatePlaceholder {
  name: string;
  type: "text";
  required: true;
  min_chars: number;
  max_chars: number;
  description?: string;
}

interface UserOfficeTemplate {
  template_ref: string;
  revision: number;
  state_version: number;
  display_name: string;
  format: "docx" | "xlsx" | "pptx";
  source: {
    sha256: string;
    size_bytes: number;
    manifest_sha256: string;
  };
  placeholder_schema: UserTemplatePlaceholder[];
  allowed_operations: string[];
  status: "needs_confirmation" | "needs_review" | "approved";
  can_approve: boolean;
  can_instantiate: boolean;
  render_evidence: {
    quality: "authoritative" | "approximate";
    renderer_id: string;
    renderer_version: string;
    font_digest: string;
    parameters_version: string;
    parameters_sha256: string;
    cache_key: string;
    manifest_sha256: string;
    page_count: number;
  };
  beta: true;
}

interface UserTemplateListResponse {
  templates: UserOfficeTemplate[];
  beta: true;
}

interface UserTemplateMutationResponse {
  template: UserOfficeTemplate;
  idempotent?: boolean;
}

interface PreviewSelection {
  templateRef: string;
  revision: number;
  pageNumber: number;
}

function isFeatureUnavailable(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

async function responseBody(response: Response): Promise<unknown> {
  const raw = await response.text();
  if (!raw) return null;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return raw;
  }
}

async function requireJson<T>(response: Response): Promise<T> {
  const body = await responseBody(response);
  if (!response.ok) {
    throw new ApiError(response.status, response.statusText, body);
  }
  return body as T;
}

function actionKey(action: string, template: UserOfficeTemplate): string {
  return `${action}:${template.template_ref}:${template.revision}`;
}

function templateSuffix(file: File): string {
  return file.name.split(".").pop()?.toLocaleLowerCase("en-US") ?? "";
}

export function UserOfficeTemplateCard({ sessionId }: { sessionId: string | null }) {
  const { t } = useTranslation("chat");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const nameId = useId();
  const fileId = useId();
  const schemaId = useId();

  const [context, setContext] = useState<RuntimeContext | null>(null);
  const [templates, setTemplates] = useState<UserOfficeTemplate[]>([]);
  const [collapsed, setCollapsed] = useState(true);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(false);
  const [displayName, setDisplayName] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [placeholderSchema, setPlaceholderSchema] = useState(
    EXAMPLE_PLACEHOLDER_SCHEMA,
  );
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [previewSelection, setPreviewSelection] =
    useState<PreviewSelection | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewFailure, setPreviewFailure] = useState<string | null>(null);
  const [previewAttempt, setPreviewAttempt] = useState(0);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      if (!sessionId) return;
      setLoading(true);
      setUnavailable(false);
      setFailure(null);
      try {
        const nextContext = await api.get<RuntimeContext>(
          API.RUNTIME.CONTEXT(sessionId),
          { signal },
        );
        const result = await api.get<UserTemplateListResponse>(
          API.OFFICE_V2.USER_TEMPLATES.LIST(
            sessionId,
            nextContext.workspace_instance_id,
          ),
          { signal },
        );
        if (signal?.aborted) return;
        setContext(nextContext);
        setTemplates(result.templates);
      } catch (error) {
        if (signal?.aborted) return;
        setContext(null);
        setTemplates([]);
        if (isFeatureUnavailable(error)) {
          setUnavailable(true);
          setFailure(null);
        } else {
          setFailure(apiErrorMessage(error, t("userOfficeTemplateLoadFailed")));
        }
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [sessionId, t],
  );

  useEffect(() => {
    setContext(null);
    setTemplates([]);
    setUnavailable(false);
    setFailure(null);
    setPreviewSelection(null);
    setShowImport(false);
    if (!sessionId) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, sessionId]);

  const selectedPreviewTemplate = useMemo(() => {
    if (!previewSelection) return null;
    return (
      templates.find(
        (template) =>
          template.template_ref === previewSelection.templateRef &&
          template.revision === previewSelection.revision,
      ) ?? null
    );
  }, [previewSelection, templates]);

  useEffect(() => {
    setPreviewUrl(null);
    setPreviewFailure(null);
    if (!sessionId || !context || !previewSelection || !selectedPreviewTemplate) {
      setPreviewLoading(false);
      return;
    }

    const controller = new AbortController();
    let active = true;
    setPreviewLoading(true);
    void apiFetch(
      API.OFFICE_V2.USER_TEMPLATES.PAGE(
        selectedPreviewTemplate.template_ref,
        sessionId,
        context.workspace_instance_id,
        selectedPreviewTemplate.revision,
        selectedPreviewTemplate.state_version,
        previewSelection.pageNumber,
      ),
      { signal: controller.signal },
    )
      .then(async (response) => {
        if (!response.ok) {
          const body = await responseBody(response);
          throw new ApiError(response.status, response.statusText, body);
        }
        const contentType = response.headers.get("content-type")?.split(";", 1)[0];
        if (contentType !== "image/png") {
          throw new Error("user_office_template_preview_type_invalid");
        }
        const blob = await response.blob();
        if (blob.size === 0 || blob.size > MAX_PREVIEW_BYTES) {
          throw new Error("user_office_template_preview_size_invalid");
        }
        if (!active) return;
        setPreviewUrl(URL.createObjectURL(blob));
      })
      .catch((error: unknown) => {
        if (!active || controller.signal.aborted) return;
        setPreviewFailure(
          apiErrorMessage(error, t("userOfficeTemplatePreviewFailed")),
        );
      })
      .finally(() => {
        if (active) setPreviewLoading(false);
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [
    context,
    previewAttempt,
    previewSelection,
    selectedPreviewTemplate,
    sessionId,
    t,
  ]);

  useEffect(
    () => () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    },
    [previewUrl],
  );

  const upsertTemplate = useCallback((next: UserOfficeTemplate) => {
    setTemplates((current) => {
      const found = current.some(
        (template) =>
          template.template_ref === next.template_ref &&
          template.revision === next.revision,
      );
      if (!found) return [next, ...current];
      return current.map((template) =>
        template.template_ref === next.template_ref &&
        template.revision === next.revision
          ? next
          : template,
      );
    });
  }, []);

  const chooseFile = useCallback(
    (file: File | null) => {
      if (!file) {
        setSelectedFile(null);
        return;
      }
      if (!ALLOWED_TEMPLATE_SUFFIXES.has(templateSuffix(file))) {
        setSelectedFile(null);
        if (fileInputRef.current) fileInputRef.current.value = "";
        toast.error(t("userOfficeTemplateFileTypeInvalid"));
        return;
      }
      if (file.size > MAX_TEMPLATE_BYTES) {
        setSelectedFile(null);
        if (fileInputRef.current) fileInputRef.current.value = "";
        toast.error(t("userOfficeTemplateFileTooLarge"));
        return;
      }
      setSelectedFile(file);
    },
    [t],
  );

  const importTemplate = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!sessionId || !context || !selectedFile || !displayName.trim()) return;
      if (
        !ALLOWED_TEMPLATE_SUFFIXES.has(templateSuffix(selectedFile)) ||
        selectedFile.size > MAX_TEMPLATE_BYTES
      ) {
        chooseFile(selectedFile);
        return;
      }

      setBusyAction("import");
      const form = new FormData();
      form.append("file", selectedFile, selectedFile.name);
      form.append("session_id", sessionId);
      form.append("workspace_instance_id", context.workspace_instance_id);
      form.append("client_request_id", `desktop-${crypto.randomUUID()}`);
      form.append("display_name", displayName.trim());
      form.append("placeholder_schema", placeholderSchema);
      try {
        const response = await apiFetch(API.OFFICE_V2.USER_TEMPLATES.IMPORT, {
          method: "POST",
          body: form,
        });
        const result = await requireJson<UserTemplateMutationResponse>(response);
        upsertTemplate(result.template);
        setDisplayName("");
        setSelectedFile(null);
        if (fileInputRef.current) fileInputRef.current.value = "";
        setShowImport(false);
        toast.success(t("userOfficeTemplateImported"));
      } catch (error) {
        toast.error(apiErrorMessage(error, t("userOfficeTemplateImportFailed")));
      } finally {
        setBusyAction(null);
      }
    },
    [
      chooseFile,
      context,
      displayName,
      placeholderSchema,
      selectedFile,
      sessionId,
      t,
      upsertTemplate,
    ],
  );

  const approveTemplate = useCallback(
    async (template: UserOfficeTemplate) => {
      if (!sessionId || !context || !template.can_approve) return;
      if (
        !window.confirm(
          t("userOfficeTemplateApproveConfirm", { name: template.display_name }),
        )
      ) {
        return;
      }
      const key = actionKey("approve", template);
      setBusyAction(key);
      try {
        const result = await api.post<UserTemplateMutationResponse>(
          API.OFFICE_V2.USER_TEMPLATES.APPROVE(template.template_ref),
          {
            session_id: sessionId,
            workspace_instance_id: context.workspace_instance_id,
            revision: template.revision,
            expected_state_version: template.state_version,
            expected_source_sha256: template.source.sha256,
            expected_render_cache_key: template.render_evidence.cache_key,
          },
        );
        upsertTemplate(result.template);
        toast.success(t("userOfficeTemplateApproved"));
      } catch (error) {
        toast.error(apiErrorMessage(error, t("userOfficeTemplateApproveFailed")));
      } finally {
        setBusyAction(null);
      }
    },
    [context, sessionId, t, upsertTemplate],
  );

  const deleteTemplate = useCallback(
    async (template: UserOfficeTemplate) => {
      if (!sessionId || !context) return;
      if (
        !window.confirm(
          t("userOfficeTemplateDeleteConfirm", { name: template.display_name }),
        )
      ) {
        return;
      }
      const key = actionKey("delete", template);
      setBusyAction(key);
      try {
        await api.deleteWithBody<UserTemplateMutationResponse>(
          API.OFFICE_V2.USER_TEMPLATES.DELETE(template.template_ref),
          {
            session_id: sessionId,
            workspace_instance_id: context.workspace_instance_id,
            revision: template.revision,
            expected_state_version: template.state_version,
          },
        );
        setTemplates((current) =>
          current.filter(
            (item) =>
              item.template_ref !== template.template_ref ||
              item.revision !== template.revision,
          ),
        );
        setPreviewSelection((current) =>
          current?.templateRef === template.template_ref &&
          current.revision === template.revision
            ? null
            : current,
        );
        toast.success(t("userOfficeTemplateDeleted"));
      } catch (error) {
        toast.error(apiErrorMessage(error, t("userOfficeTemplateDeleteFailed")));
      } finally {
        setBusyAction(null);
      }
    },
    [context, sessionId, t],
  );

  const copyReference = useCallback(
    async (templateRef: string) => {
      try {
        await navigator.clipboard.writeText(templateRef);
        toast.success(t("userOfficeTemplateReferenceCopied"));
      } catch {
        toast.error(t("userOfficeTemplateReferenceCopyFailed"));
      }
    },
    [t],
  );

  const statusLabel = useCallback(
    (status: UserOfficeTemplate["status"]) => {
      if (status === "needs_confirmation") {
        return t("userOfficeTemplateStatusNeedsConfirmation");
      }
      if (status === "needs_review") {
        return t("userOfficeTemplateStatusNeedsReview");
      }
      return t("userOfficeTemplateStatusApproved");
    },
    [t],
  );

  if (!sessionId || (unavailable && !loading) || (!context && loading)) return null;
  if (failure && !context) {
    return (
      <section
        aria-label={t("userOfficeTemplateTitle")}
        className="rounded-3xl border border-amber-500/30 bg-amber-500/5 p-4"
      >
        <p className="flex items-center gap-2 text-[13px] font-medium text-[var(--text-primary)]">
          <AlertTriangle className="h-4 w-4 text-amber-500" />
          {t("userOfficeTemplateTitle")}
          <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-500">
            Beta
          </span>
        </p>
        <p role="alert" className="mt-2 break-words text-[11px] text-[var(--text-secondary)]">
          {failure}
        </p>
        <Button
          className="mt-3"
          size="sm"
          variant="outline"
          disabled={loading}
          onClick={() => void load()}
        >
          {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          {t("retry")}
        </Button>
      </section>
    );
  }
  if (!context) return null;

  return (
    <section
      data-testid="user-office-template-card"
      aria-label={t("userOfficeTemplateTitle")}
      aria-busy={busyAction !== null}
      className="overflow-hidden rounded-3xl border border-amber-500/20 bg-amber-500/[0.035] shadow-[0_0_0_1px_rgba(245,158,11,0.04)_inset] backdrop-blur-sm"
    >
      <button
        type="button"
        aria-expanded={!collapsed}
        className="flex w-full items-start justify-between px-4 py-4 text-left transition-colors hover:bg-amber-500/[0.035]"
        onClick={() => setCollapsed((value) => !value)}
      >
        <span className="flex min-w-0 flex-1 items-start gap-3">
          <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-2xl border border-amber-500/20 bg-amber-500/10">
            <FileStack className="h-4 w-4 text-amber-500" />
          </span>
          <span className="min-w-0">
            <span className="flex flex-wrap items-center gap-2 text-[13px] font-medium text-[var(--text-primary)]">
              {t("userOfficeTemplateTitle")}
              <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-500">
                Beta
              </span>
            </span>
            <span className="mt-1 block text-[11px] text-[var(--text-tertiary)]">
              {t("userOfficeTemplateCount", { count: templates.length })}
            </span>
          </span>
        </span>
        <ChevronDown
          className={cn(
            "mt-1 h-4 w-4 text-[var(--text-tertiary)] transition-transform",
            collapsed && "-rotate-90",
          )}
        />
      </button>

      {!collapsed && (
        <div className="space-y-3 border-t border-amber-500/10 px-4 py-3">
          <div className="rounded-xl border border-amber-500/15 bg-amber-500/[0.04] p-2.5 text-[11px] text-[var(--text-secondary)]">
            {t("userOfficeTemplateBetaNotice")}
          </div>

          <Button
            type="button"
            size="sm"
            variant="outline"
            className="w-full"
            disabled={busyAction !== null}
            onClick={() => setShowImport((value) => !value)}
          >
            <Plus />
            {showImport
              ? t("userOfficeTemplateCancelImport")
              : t("userOfficeTemplateAdd")}
          </Button>

          {showImport && (
            <form
              aria-label={t("userOfficeTemplateImportForm")}
              className="space-y-3 rounded-xl border border-white/8 bg-white/[0.025] p-3"
              onSubmit={(event) => void importTemplate(event)}
            >
              <div className="space-y-1.5">
                <label
                  className="text-[11px] font-medium text-[var(--text-secondary)]"
                  htmlFor={nameId}
                >
                  {t("userOfficeTemplateName")}
                </label>
                <Input
                  id={nameId}
                  maxLength={160}
                  value={displayName}
                  disabled={busyAction !== null}
                  placeholder={t("userOfficeTemplateNamePlaceholder")}
                  onChange={(event) => setDisplayName(event.target.value)}
                />
              </div>

              <div className="space-y-1.5">
                <label
                  className="text-[11px] font-medium text-[var(--text-secondary)]"
                  htmlFor={fileId}
                >
                  {t("userOfficeTemplateFile")}
                </label>
                <input
                  ref={fileInputRef}
                  id={fileId}
                  type="file"
                  accept=".docx,.xlsx,.pptx"
                  disabled={busyAction !== null}
                  className="block w-full rounded-lg border border-[var(--border-default)] px-2 py-2 text-[11px] text-[var(--text-secondary)] file:mr-2 file:rounded-md file:border-0 file:bg-[var(--surface-secondary)] file:px-2 file:py-1 file:text-[11px] file:text-[var(--text-primary)]"
                  onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
                />
                <p className="text-[10px] text-[var(--text-quaternary)]">
                  {t("userOfficeTemplateFileHelp")}
                </p>
              </div>

              <div className="space-y-1.5">
                <label
                  className="text-[11px] font-medium text-[var(--text-secondary)]"
                  htmlFor={schemaId}
                >
                  {t("userOfficeTemplateSchema")}
                </label>
                <textarea
                  id={schemaId}
                  rows={11}
                  spellCheck={false}
                  value={placeholderSchema}
                  disabled={busyAction !== null}
                  className="w-full resize-y rounded-lg border border-[var(--border-default)] bg-transparent px-2.5 py-2 font-mono text-[10px] leading-relaxed text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--ring)] disabled:opacity-50"
                  onChange={(event) => setPlaceholderSchema(event.target.value)}
                />
                <p className="text-[10px] leading-relaxed text-[var(--text-quaternary)]">
                  {t("userOfficeTemplateSchemaHelp")}
                </p>
              </div>

              <Button
                type="submit"
                size="sm"
                className="w-full"
                disabled={
                  busyAction !== null ||
                  !selectedFile ||
                  !displayName.trim() ||
                  !placeholderSchema.trim()
                }
              >
                {busyAction === "import" ? (
                  <Loader2 className="animate-spin" />
                ) : (
                  <Upload />
                )}
                {t("userOfficeTemplateImportAction")}
              </Button>
            </form>
          )}

          {templates.length === 0 ? (
            <p className="py-2 text-center text-[11px] text-[var(--text-quaternary)]">
              {t("userOfficeTemplateEmpty")}
            </p>
          ) : (
            <ul className="space-y-3">
              {templates.map((template) => {
                const approveKey = actionKey("approve", template);
                const deleteKey = actionKey("delete", template);
                const isPreviewSelected =
                  previewSelection?.templateRef === template.template_ref &&
                  previewSelection.revision === template.revision;
                const pageCount = Math.max(1, template.render_evidence.page_count);
                return (
                  <li
                    key={`${template.template_ref}:${template.revision}`}
                    data-testid={`user-office-template-${template.template_ref}`}
                    className="space-y-3 rounded-xl border border-white/8 bg-white/[0.025] p-3"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="truncate text-xs font-medium text-[var(--text-primary)]">
                          {template.display_name}
                        </p>
                        <div className="mt-1.5 flex flex-wrap gap-1.5 text-[9px] font-medium uppercase tracking-wide">
                          <span className="rounded-full border border-white/10 px-1.5 py-0.5 text-[var(--text-tertiary)]">
                            {template.format}
                          </span>
                          <span className="rounded-full border border-white/10 px-1.5 py-0.5 text-[var(--text-tertiary)]">
                            {statusLabel(template.status)}
                          </span>
                          <span
                            className={cn(
                              "rounded-full border px-1.5 py-0.5",
                              template.render_evidence.quality === "authoritative"
                                ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-500"
                                : "border-amber-500/25 bg-amber-500/10 text-amber-500",
                            )}
                          >
                            {template.render_evidence.quality === "authoritative"
                              ? t("userOfficeTemplateAuthoritative")
                              : t("userOfficeTemplateApproximate")}
                          </span>
                          <span className="rounded-full border border-white/10 px-1.5 py-0.5 text-[var(--text-tertiary)]">
                            {t("userOfficeTemplateStateVersion", {
                              version: template.state_version,
                            })}
                          </span>
                        </div>
                      </div>
                    </div>

                    <div>
                      <p className="text-[10px] font-medium text-[var(--text-tertiary)]">
                        {t("userOfficeTemplatePlaceholders")}
                      </p>
                      <div className="mt-1.5 flex max-h-24 flex-wrap gap-1 overflow-y-auto">
                        {template.placeholder_schema.map((placeholder) => (
                          <span
                            key={placeholder.name}
                            className="rounded-md bg-white/[0.045] px-1.5 py-0.5 font-mono text-[9px] text-[var(--text-secondary)]"
                          >
                            {placeholder.name}
                          </span>
                        ))}
                      </div>
                    </div>

                    <div className="rounded-lg border border-white/6 bg-black/[0.04] p-2">
                      <p className="text-[9px] uppercase tracking-wide text-[var(--text-quaternary)]">
                        {t("userOfficeTemplateReference")}
                      </p>
                      <div className="mt-1 flex items-start gap-2">
                        <code className="min-w-0 flex-1 break-all text-[10px] text-[var(--text-secondary)]">
                          {template.template_ref}
                        </code>
                        <button
                          type="button"
                          aria-label={t("userOfficeTemplateCopyReference")}
                          className="rounded p-1 text-[var(--text-tertiary)] transition-colors hover:bg-white/[0.05] hover:text-[var(--text-primary)]"
                          onClick={() => void copyReference(template.template_ref)}
                        >
                          <Copy className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>

                    <p
                      className={cn(
                        "flex items-start gap-1.5 text-[10px]",
                        template.can_instantiate
                          ? "text-emerald-500"
                          : "text-amber-500",
                      )}
                    >
                      {template.can_instantiate ? (
                        <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0" />
                      ) : (
                        <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                      )}
                      {template.can_instantiate
                        ? t("userOfficeTemplateAvailable")
                        : t("userOfficeTemplateUnavailable")}
                    </p>

                    <div className="flex flex-wrap gap-1.5">
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={busyAction !== null}
                        onClick={() =>
                          setPreviewSelection({
                            templateRef: template.template_ref,
                            revision: template.revision,
                            pageNumber: 1,
                          })
                        }
                      >
                        <Eye />
                        {t("userOfficeTemplatePreview")}
                      </Button>
                      {template.can_approve && (
                        <Button
                          type="button"
                          size="sm"
                          disabled={busyAction !== null}
                          onClick={() => void approveTemplate(template)}
                        >
                          {busyAction === approveKey ? (
                            <Loader2 className="animate-spin" />
                          ) : (
                            <CheckCircle2 />
                          )}
                          {t("userOfficeTemplateApprove")}
                        </Button>
                      )}
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="text-red-500 hover:text-red-500"
                        disabled={busyAction !== null}
                        onClick={() => void deleteTemplate(template)}
                      >
                        {busyAction === deleteKey ? (
                          <Loader2 className="animate-spin" />
                        ) : (
                          <Trash2 />
                        )}
                        {t("userOfficeTemplateDelete")}
                      </Button>
                    </div>

                    {isPreviewSelected && (
                      <div
                        aria-live="polite"
                        className="overflow-hidden rounded-xl border border-white/8 bg-black/[0.04]"
                      >
                        <div className="flex min-h-32 items-center justify-center p-2">
                          {previewLoading ? (
                            <span className="flex items-center gap-2 text-[11px] text-[var(--text-tertiary)]">
                              <Loader2 className="h-4 w-4 animate-spin" />
                              {t("userOfficeTemplatePreviewLoading")}
                            </span>
                          ) : previewFailure ? (
                            <div className="p-3 text-center">
                              <p role="alert" className="text-[10px] text-amber-500">
                                {previewFailure}
                              </p>
                              <Button
                                type="button"
                                className="mt-2"
                                size="sm"
                                variant="outline"
                                onClick={() => setPreviewAttempt((value) => value + 1)}
                              >
                                <RefreshCw />
                                {t("retry")}
                              </Button>
                            </div>
                          ) : previewUrl ? (
                            // This object URL is private to the current authenticated response
                            // and is revoked whenever the page, record, or component changes.
                            // eslint-disable-next-line @next/next/no-img-element
                            <img
                              src={previewUrl}
                              alt={t("userOfficeTemplatePreviewAlt", {
                                name: template.display_name,
                                page: previewSelection.pageNumber,
                              })}
                              className="max-h-64 max-w-full rounded bg-white object-contain"
                            />
                          ) : null}
                        </div>
                        {pageCount > 1 && (
                          <div className="flex items-center justify-between border-t border-white/8 px-2 py-1.5">
                            <button
                              type="button"
                              aria-label={t("userOfficeTemplatePreviousPage")}
                              disabled={previewSelection.pageNumber <= 1 || previewLoading}
                              className="rounded p-1 text-[var(--text-tertiary)] hover:bg-white/[0.05] disabled:opacity-30"
                              onClick={() =>
                                setPreviewSelection((current) =>
                                  current
                                    ? {
                                        ...current,
                                        pageNumber: Math.max(1, current.pageNumber - 1),
                                      }
                                    : current,
                                )
                              }
                            >
                              <ChevronLeft className="h-3.5 w-3.5" />
                            </button>
                            <span className="text-[10px] text-[var(--text-tertiary)]">
                              {t("userOfficeTemplatePageCount", {
                                page: previewSelection.pageNumber,
                                count: pageCount,
                              })}
                            </span>
                            <button
                              type="button"
                              aria-label={t("userOfficeTemplateNextPage")}
                              disabled={
                                previewSelection.pageNumber >= pageCount || previewLoading
                              }
                              className="rounded p-1 text-[var(--text-tertiary)] hover:bg-white/[0.05] disabled:opacity-30"
                              onClick={() =>
                                setPreviewSelection((current) =>
                                  current
                                    ? {
                                        ...current,
                                        pageNumber: Math.min(
                                          pageCount,
                                          current.pageNumber + 1,
                                        ),
                                      }
                                    : current,
                                )
                              }
                            >
                              <ChevronRight className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
