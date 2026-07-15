"use client";

import {
  useEffect,
  useMemo,
  useState,
  type ComponentType,
  type KeyboardEvent,
} from "react";
import {
  Activity,
  AlertOctagon,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Cpu,
  KeyRound,
  Loader2,
  Plug,
  Radio,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Wrench,
  Workflow,
  XCircle,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  useEmergencyStopToggle,
  useSecurityAudit,
  useSecurityOverview,
  useSecurityToolToggle,
} from "@/hooks/use-security";
import { apiErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { SecurityAuditEvent, SecurityTool } from "@/types/security";

const TOOLS_PAGE_SIZE = 6;
const INTEGRATIONS_PAGE_SIZE = 4;
const AUDIT_PAGE_SIZE = 10;

type SecuritySectionId = "tools" | "integrations" | "audit";

const SECURITY_SECTIONS: Array<{
  id: SecuritySectionId;
  labelKey: string;
  icon: ComponentType<{ className?: string }>;
}> = [
  { id: "tools", labelKey: "securitySectionTools", icon: Wrench },
  { id: "integrations", labelKey: "securitySectionIntegrations", icon: Plug },
  { id: "audit", labelKey: "securitySectionAudit", icon: Activity },
];

function formatTime(value: string | null, locale: string) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function shortId(value: string | null) {
  if (!value) return null;
  return value.length > 14 ? `${value.slice(0, 6)}…${value.slice(-5)}` : value;
}

function useClientPagination<T>(items: readonly T[], pageSize: number) {
  const [page, setPage] = useState(1);
  const total = items.length;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(page, totalPages);
  const startIndex = (safePage - 1) * pageSize;

  useEffect(() => {
    setPage((current) => Math.min(current, totalPages));
  }, [totalPages]);

  return {
    items: items.slice(startIndex, startIndex + pageSize),
    page: safePage,
    total,
    totalPages,
    start: total === 0 ? 0 : startIndex + 1,
    end: Math.min(startIndex + pageSize, total),
    setPage: (nextPage: number) =>
      setPage(Math.min(Math.max(1, nextPage), totalPages)),
  };
}

function PaginationControls({
  page,
  totalPages,
  start,
  end,
  total,
  label,
  focusTargetId,
  onPageChange,
}: {
  page: number;
  totalPages: number;
  start: number;
  end: number;
  total: number;
  label: string;
  focusTargetId: string;
  onPageChange: (page: number) => void;
}) {
  const { t } = useTranslation("settings");

  const changePage = (nextPage: number) => {
    onPageChange(nextPage);
    requestAnimationFrame(() => {
      const target = document.getElementById(focusTargetId);
      target?.focus({ preventScroll: true });
      target?.scrollIntoView({ block: "start" });
    });
  };

  if (total === 0) return null;

  return (
    <div className="flex flex-col gap-2 border-t border-[var(--border-default)] bg-[var(--surface-secondary)]/40 px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between sm:px-4">
      <p
        className="text-center text-[11px] text-[var(--text-tertiary)] sm:text-left"
        aria-live="polite"
      >
        {t("securityPaginationRange", { start, end, total })}
      </p>
      {totalPages > 1 && (
        <nav
          className="flex items-center justify-center gap-2"
          aria-label={label}
        >
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 min-w-8 px-2 sm:px-3"
            onClick={() => changePage(page - 1)}
            disabled={page === 1}
            aria-label={t("securityPaginationPrevious")}
          >
            <ChevronLeft className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">{t("securityPaginationPrevious")}</span>
          </Button>
          <span className="min-w-16 text-center text-xs font-medium text-[var(--text-secondary)]">
            {t("securityPaginationStatus", { page, totalPages })}
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 min-w-8 px-2 sm:px-3"
            onClick={() => changePage(page + 1)}
            disabled={page === totalPages}
            aria-label={t("securityPaginationNext")}
          >
            <span className="hidden sm:inline">{t("securityPaginationNext")}</span>
            <ChevronRight className="h-3.5 w-3.5" />
          </Button>
        </nav>
      )}
    </div>
  );
}

function Capabilities({ values }: { values: string[] }) {
  const { t } = useTranslation("settings");
  if (values.length === 0) {
    return (
      <span className="text-[11px] text-[var(--text-tertiary)]">
        {t("securityNoCapabilities")}
      </span>
    );
  }
  return (
    <div className="flex flex-wrap gap-1.5" aria-label={t("securityCapabilities")}>
      {values.map((capability) => (
        <Badge
          key={capability}
          variant="outline"
          className="max-w-full font-mono text-[10px] font-medium"
        >
          <span className="truncate">{capability}</span>
        </Badge>
      ))}
    </div>
  );
}

function BinaryStatus({
  active,
  activeLabel,
  inactiveLabel,
  warning = false,
}: {
  active: boolean;
  activeLabel: string;
  inactiveLabel: string;
  warning?: boolean;
}) {
  return (
    <Badge
      variant={active ? (warning ? "warning" : "success") : "secondary"}
      className="shrink-0"
    >
      {active ? activeLabel : inactiveLabel}
    </Badge>
  );
}

function SectionHeading({
  id,
  icon: Icon,
  title,
  description,
  count,
}: {
  id: string;
  icon: ComponentType<{ className?: string }>;
  title: string;
  description: string;
  count?: number;
}) {
  return (
    <div className="flex items-start gap-3">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[var(--surface-secondary)] text-[var(--text-secondary)]">
        <Icon className="h-4 w-4" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h2
            id={id}
            tabIndex={-1}
            className="text-ui-title-sm font-semibold text-[var(--text-primary)]"
          >
            {title}
          </h2>
          {typeof count === "number" && <Badge variant="secondary">{count}</Badge>}
        </div>
        <p className="mt-1 text-xs text-[var(--text-secondary)]">{description}</p>
      </div>
    </div>
  );
}

function SecurityLoading() {
  return (
    <div className="space-y-4" aria-busy="true">
      {["h-52", "h-12", "h-72"].map((height, index) => (
        <div
          key={index}
          className={cn(
            "animate-pulse rounded-xl border border-[var(--border-default)] bg-[var(--surface-secondary)]",
            height,
          )}
        />
      ))}
    </div>
  );
}

export function SecurityTab() {
  const { t, i18n } = useTranslation("settings");
  const [activeSection, setActiveSection] =
    useState<SecuritySectionId>("tools");
  const [auditSnapshot, setAuditSnapshot] =
    useState<SecurityAuditEvent[] | null>(null);
  const overviewQuery = useSecurityOverview();
  const auditQuery = useSecurityAudit(100);
  const toolToggle = useSecurityToolToggle();
  const emergencyToggle = useEmergencyStopToggle();

  const overview = overviewQuery.data;
  const sortedTools = useMemo(
    () =>
      [...(overview?.tools ?? [])].sort((left, right) =>
        `${left.source_kind}:${left.source_id}:${left.id}`.localeCompare(
          `${right.source_kind}:${right.source_id}:${right.id}`,
        ),
      ),
    [overview?.tools],
  );
  const connectors = overview?.connectors ?? [];
  const providers = overview?.providers ?? [];
  const liveAuditEvents = auditQuery.data?.events ?? [];
  const auditEvents = auditSnapshot ?? liveAuditEvents;
  const toolsPage = useClientPagination(sortedTools, TOOLS_PAGE_SIZE);
  const connectorsPage = useClientPagination(connectors, INTEGRATIONS_PAGE_SIZE);
  const providersPage = useClientPagination(providers, INTEGRATIONS_PAGE_SIZE);
  const auditPage = useClientPagination(auditEvents, AUDIT_PAGE_SIZE);

  const changeAuditPage = (nextPage: number) => {
    if (nextPage > 1 && auditSnapshot === null) {
      setAuditSnapshot([...liveAuditEvents]);
    } else if (nextPage === 1 && auditSnapshot !== null) {
      setAuditSnapshot(null);
    }
    auditPage.setPage(nextPage);
  };

  const sourceKindLabel = (kind: string) => {
    const known: Record<string, string> = {
      builtin: t("securitySourceBuiltin"),
      core: t("securitySourceBuiltin"),
      native: t("securitySourceBuiltin"),
      connector: t("securitySourceConnector"),
      mcp: t("securitySourceMcp"),
      plugin: t("securitySourcePlugin"),
    };
    return known[kind] ?? kind;
  };

  const connectorStatusLabel = (status: string) => {
    const known: Record<string, string> = {
      connected: t("securityConnected"),
      disconnected: t("securityDisconnected"),
      needs_auth: t("securityConnectorNeedsAuth"),
      needs_approval: t("securityConnectorNeedsApproval"),
      failed: t("securityConnectorFailed"),
      disabled: t("securityDisabled"),
    };
    return known[status] ?? t("securityDisconnected");
  };

  const refresh = () => {
    setAuditSnapshot(null);
    auditPage.setPage(1);
    void Promise.all([overviewQuery.refetch(), auditQuery.refetch()]);
  };

  const selectSectionByIndex = (index: number) => {
    const section = SECURITY_SECTIONS[index];
    if (!section) return;
    setActiveSection(section.id);
    requestAnimationFrame(() => {
      document.getElementById(`security-section-tab-${section.id}`)?.focus();
    });
  };

  const handleSectionKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (index + 1) % SECURITY_SECTIONS.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (index - 1 + SECURITY_SECTIONS.length) % SECURITY_SECTIONS.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = SECURITY_SECTIONS.length - 1;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    selectSectionByIndex(nextIndex);
  };

  const toggleEmergencyStop = async (active: boolean) => {
    const confirmation = active
      ? t("securityEmergencyActivateConfirm")
      : t("securityEmergencyDeactivateConfirm");
    if (!window.confirm(confirmation)) return;
    try {
      const result = await emergencyToggle.mutateAsync(active);
      if (result.warnings?.length) {
        toast.warning(
          t("securityEmergencyWarning", { warnings: result.warnings.join(", ") }),
        );
      } else {
        toast.success(
          active
            ? t("securityEmergencyActivated")
            : t("securityEmergencyDeactivated"),
        );
      }
    } catch (error) {
      toast.error(apiErrorMessage(error, t("securityUpdateFailed")));
    }
  };

  const toggleTool = async (tool: SecurityTool, enabled: boolean) => {
    if (enabled && !window.confirm(t("securityEnableToolConfirm", { tool: tool.id }))) {
      return;
    }
    try {
      await toolToggle.mutateAsync({ id: tool.id, enabled });
      toast.success(
        enabled
          ? t("securityToolEnabled", { tool: tool.id })
          : t("securityToolDisabled", { tool: tool.id }),
      );
    } catch (error) {
      toast.error(apiErrorMessage(error, t("securityUpdateFailed")));
    }
  };

  if (overviewQuery.isLoading && !overview) return <SecurityLoading />;

  if (!overview) {
    return (
      <div className="rounded-xl border border-[var(--color-destructive)]/30 bg-[var(--color-destructive)]/5 p-5">
        <div className="flex items-start gap-3">
          <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-[var(--color-destructive)]" />
          <div className="min-w-0 flex-1">
            <h2 className="text-sm font-semibold text-[var(--text-primary)]">
              {t("securityLoadFailed")}
            </h2>
            <p className="mt-1 break-words text-xs text-[var(--text-secondary)]">
              {apiErrorMessage(overviewQuery.error, t("securityLoadFailedDesc"))}
            </p>
            <Button className="mt-4" variant="outline" size="sm" onClick={refresh}>
              <RefreshCw className="h-3.5 w-3.5" />
              {t("securityRetry")}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  const updatedAt = formatTime(overview.state.updated_at, i18n.language);
  const refreshing = overviewQuery.isFetching || auditQuery.isFetching;

  return (
    <div className="space-y-6">
      <section aria-labelledby="security-overview-title">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h2
              id="security-overview-title"
              className="text-ui-title-sm font-semibold text-[var(--text-primary)]"
            >
              {t("securityOverviewTitle")}
            </h2>
            <p className="mt-1 max-w-2xl text-xs text-[var(--text-secondary)]">
              {t("securityOverviewDesc")}
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="self-start"
            onClick={refresh}
            disabled={refreshing}
          >
            <RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} />
            {t("securityRefresh")}
          </Button>
        </div>

        <div
          className={cn(
            "mt-4 rounded-xl border p-4",
            overview.state.emergency_stop
              ? "border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/5"
              : "border-[var(--border-default)] bg-[var(--surface-secondary)]/50",
          )}
        >
          <div className="flex items-start gap-3">
            <div
              className={cn(
                "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg",
                overview.state.emergency_stop
                  ? "bg-[var(--color-destructive)]/10 text-[var(--color-destructive)]"
                  : "bg-[var(--color-success)]/10 text-[var(--color-success)]",
              )}
            >
              {overview.state.emergency_stop ? (
                <AlertOctagon className="h-5 w-5" />
              ) : (
                <ShieldCheck className="h-5 w-5" />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-sm font-semibold text-[var(--text-primary)]">
                  {t("securityEmergencyStop")}
                </h3>
                <BinaryStatus
                  active={overview.state.emergency_stop}
                  activeLabel={t("securityEmergencyActive")}
                  inactiveLabel={t("securityEmergencyReady")}
                  warning
                />
              </div>
              <p className="mt-1 text-xs text-[var(--text-secondary)]">
                {overview.state.emergency_stop
                  ? t("securityEmergencyActiveDesc")
                  : t("securityEmergencyReadyDesc")}
              </p>
              {updatedAt && (
                <p className="mt-2 text-[11px] text-[var(--text-tertiary)]">
                  {t("securityLastChanged", { time: updatedAt })}
                </p>
              )}
            </div>
            <Switch
              checked={overview.state.emergency_stop}
              onCheckedChange={toggleEmergencyStop}
              disabled={emergencyToggle.isPending}
              aria-label={t("securityEmergencyToggleLabel")}
              className="mt-1"
            />
          </div>
        </div>

        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          <div className="rounded-lg border border-[var(--border-default)] p-3">
            <div className="flex items-center justify-between gap-2">
              <Workflow className="h-4 w-4 text-[var(--text-secondary)]" />
              <BinaryStatus
                active={overview.automations.runtime_running}
                activeLabel={t("securityRunning")}
                inactiveLabel={t("securityStopped")}
              />
            </div>
            <p className="mt-2 text-xs font-medium text-[var(--text-primary)]">
              {t("securityAutomations")}
            </p>
            <p className="mt-0.5 text-[11px] text-[var(--text-secondary)]">
              {t("securityEnabledAutomationCount", {
                count: overview.automations.enabled_count,
              })}
            </p>
          </div>
          <div className="rounded-lg border border-[var(--border-default)] p-3">
            <div className="flex items-center justify-between gap-2">
              <Radio className="h-4 w-4 text-[var(--text-secondary)]" />
              <BinaryStatus
                active={overview.release_gates.remote_access}
                activeLabel={t("securityExposed")}
                inactiveLabel={t("securityClosed")}
                warning
              />
            </div>
            <p className="mt-2 text-xs font-medium text-[var(--text-primary)]">
              {t("securityRemoteAccess")}
            </p>
            <p className="mt-0.5 text-[11px] text-[var(--text-secondary)]">
              {t("securityReleaseGateDesc")}
            </p>
          </div>
          <div className="rounded-lg border border-[var(--border-default)] p-3">
            <div className="flex items-center justify-between gap-2">
              <Plug className="h-4 w-4 text-[var(--text-secondary)]" />
              <BinaryStatus
                active={overview.release_gates.messaging_channels}
                activeLabel={t("securityExposed")}
                inactiveLabel={t("securityClosed")}
                warning
              />
            </div>
            <p className="mt-2 text-xs font-medium text-[var(--text-primary)]">
              {t("securityMessagingChannels")}
            </p>
            <p className="mt-0.5 text-[11px] text-[var(--text-secondary)]">
              {t("securityReleaseGateDesc")}
            </p>
          </div>
        </div>
      </section>

      <div
        role="tablist"
        className="grid grid-cols-3 gap-1 rounded-xl border border-[var(--border-default)] bg-[var(--surface-secondary)]/60 p-1"
        aria-label={t("securitySectionNavigation")}
      >
        {SECURITY_SECTIONS.map(({ id, labelKey, icon: Icon }, index) => {
          const active = activeSection === id;
          return (
            <button
              key={id}
              id={`security-section-tab-${id}`}
              type="button"
              onClick={() => setActiveSection(id)}
              onKeyDown={(event) => handleSectionKeyDown(event, index)}
              role="tab"
              aria-selected={active}
              aria-controls={`security-panel-${id}`}
              tabIndex={active ? 0 : -1}
              className={cn(
                "flex min-h-10 items-center justify-center gap-1.5 rounded-lg px-2 py-2 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]",
                active
                  ? "bg-[var(--surface-primary)] text-[var(--text-primary)] shadow-[var(--shadow-sm)]"
                  : "text-[var(--text-secondary)] hover:bg-[var(--surface-primary)]/70 hover:text-[var(--text-primary)]",
              )}
            >
              <Icon className="h-3.5 w-3.5 shrink-0" />
              <span className="truncate">{t(labelKey)}</span>
            </button>
          );
        })}
      </div>

      {activeSection === "tools" && (
        <section
          id="security-panel-tools"
          role="tabpanel"
          aria-labelledby="security-section-tab-tools security-tools-title"
          tabIndex={0}
          className="space-y-4"
        >
          <SectionHeading
            id="security-tools-title"
            icon={Wrench}
            title={t("securityToolsTitle")}
            description={t("securityToolsDesc")}
            count={toolsPage.total}
          />

          {toolsPage.total === 0 ? (
            <div className="rounded-lg border border-dashed border-[var(--border-default)] px-4 py-8 text-center text-xs text-[var(--text-secondary)]">
              {t("securityNoTools")}
            </div>
          ) : (
            <div className="overflow-hidden rounded-xl border border-[var(--border-default)]">
              <div className="divide-y divide-[var(--border-default)]">
                {toolsPage.items.map((tool) => {
                  const changing =
                    toolToggle.isPending && toolToggle.variables?.id === tool.id;
                  return (
                    <article
                      key={tool.id}
                      className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <p className="break-all font-mono text-xs font-semibold text-[var(--text-primary)]">
                            {tool.id}
                          </p>
                          <BinaryStatus
                            active={tool.enabled}
                            activeLabel={t("securityEnabled")}
                            inactiveLabel={t("securityDisabled")}
                          />
                          {tool.requires_approval && (
                            <Badge variant="warning">{t("securityApprovalRequired")}</Badge>
                          )}
                        </div>
                        <div className="mt-1.5 flex min-w-0 flex-wrap items-center gap-1.5 text-[11px] text-[var(--text-tertiary)]">
                          <Badge variant="secondary">
                            {sourceKindLabel(tool.source_kind)}
                          </Badge>
                          <span className="break-all font-mono">{tool.source_id}</span>
                        </div>
                        {tool.description && (
                          <p className="mt-2 text-xs text-[var(--text-secondary)]">
                            {tool.description}
                          </p>
                        )}
                        <div className="mt-2">
                          <Capabilities values={tool.capabilities} />
                        </div>
                      </div>
                      {tool.toggleable ? (
                        <div className="flex shrink-0 items-center justify-between gap-3 sm:justify-end">
                          <span className="text-xs text-[var(--text-secondary)] sm:hidden">
                            {tool.enabled ? t("securityEnabled") : t("securityDisabled")}
                          </span>
                          {changing ? (
                            <Loader2 className="h-4 w-4 animate-spin text-[var(--text-tertiary)]" />
                          ) : (
                            <Switch
                              checked={tool.enabled}
                              onCheckedChange={(enabled) => toggleTool(tool, enabled)}
                              disabled={toolToggle.isPending}
                              aria-label={t("securityToolToggleLabel", { tool: tool.id })}
                            />
                          )}
                        </div>
                      ) : (
                        <Badge variant="outline" className="self-start sm:self-auto">
                          {t("securityManagedBySource")}
                        </Badge>
                      )}
                    </article>
                  );
                })}
              </div>
              <PaginationControls
                page={toolsPage.page}
                totalPages={toolsPage.totalPages}
                start={toolsPage.start}
                end={toolsPage.end}
                total={toolsPage.total}
                label={t("securityToolsPaginationLabel")}
                focusTargetId="security-tools-title"
                onPageChange={toolsPage.setPage}
              />
            </div>
          )}
        </section>
      )}

      {activeSection === "integrations" && (
        <section
          id="security-panel-integrations"
          role="tabpanel"
          aria-labelledby="security-section-tab-integrations security-integrations-title"
          tabIndex={0}
          className="space-y-4"
        >
          <SectionHeading
            id="security-integrations-title"
            icon={Plug}
            title={t("securityIntegrationsTitle")}
            description={t("securityCredentialsBooleanDesc")}
          />

          <div className="grid gap-3 md:grid-cols-2">
            <div className="overflow-hidden rounded-xl border border-[var(--border-default)]">
              <div className="flex items-center gap-2 border-b border-[var(--border-default)] bg-[var(--surface-secondary)]/50 px-4 py-3">
                <Plug className="h-4 w-4 text-[var(--text-secondary)]" />
                <h3
                  id="security-connectors-title"
                  tabIndex={-1}
                  className="text-sm font-semibold text-[var(--text-primary)]"
                >
                  {t("securityConnectors")}
                </h3>
                <Badge variant="secondary" className="ml-auto">
                  {connectorsPage.total}
                </Badge>
              </div>
              {connectorsPage.total === 0 ? (
                <p className="px-4 py-8 text-center text-xs text-[var(--text-tertiary)]">
                  {t("securityNoConnectors")}
                </p>
              ) : (
                <div className="divide-y divide-[var(--border-default)]">
                  {connectorsPage.items.map((connector) => (
                    <article key={connector.id} className="p-4">
                      <div className="flex min-w-0 items-start gap-2">
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-xs font-semibold text-[var(--text-primary)]">
                            {connector.name}
                          </p>
                          <p className="mt-0.5 truncate font-mono text-[10px] text-[var(--text-tertiary)]">
                            {connector.id}
                          </p>
                        </div>
                        <BinaryStatus
                          active={connector.enabled && connector.connected}
                          activeLabel={t("securityConnected")}
                          inactiveLabel={
                            connector.enabled
                              ? connectorStatusLabel(connector.status)
                              : t("securityDisabled")
                          }
                        />
                      </div>
                      <div className="mt-2 flex items-center gap-1.5 text-[11px] text-[var(--text-secondary)]">
                        <KeyRound className="h-3 w-3" />
                        {connector.credential_configured
                          ? t("securityCredentialConfigured")
                          : t("securityCredentialMissing")}
                      </div>
                      <div className="mt-2">
                        <Capabilities values={connector.capabilities} />
                      </div>
                    </article>
                  ))}
                </div>
              )}
              <PaginationControls
                page={connectorsPage.page}
                totalPages={connectorsPage.totalPages}
                start={connectorsPage.start}
                end={connectorsPage.end}
                total={connectorsPage.total}
                label={t("securityConnectorsPaginationLabel")}
                focusTargetId="security-connectors-title"
                onPageChange={connectorsPage.setPage}
              />
            </div>

            <div className="overflow-hidden rounded-xl border border-[var(--border-default)]">
              <div className="flex items-center gap-2 border-b border-[var(--border-default)] bg-[var(--surface-secondary)]/50 px-4 py-3">
                <Cpu className="h-4 w-4 text-[var(--text-secondary)]" />
                <h3
                  id="security-providers-title"
                  tabIndex={-1}
                  className="text-sm font-semibold text-[var(--text-primary)]"
                >
                  {t("securityProviders")}
                </h3>
                <Badge variant="secondary" className="ml-auto">
                  {providersPage.total}
                </Badge>
              </div>
              {providersPage.total === 0 ? (
                <p className="px-4 py-8 text-center text-xs text-[var(--text-tertiary)]">
                  {t("securityNoProviders")}
                </p>
              ) : (
                <div className="divide-y divide-[var(--border-default)]">
                  {providersPage.items.map((provider) => (
                    <article key={provider.id} className="p-4">
                      <div className="flex min-w-0 items-start gap-2">
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-xs font-semibold text-[var(--text-primary)]">
                            {provider.name}
                          </p>
                          <p className="mt-0.5 truncate font-mono text-[10px] text-[var(--text-tertiary)]">
                            {provider.id}
                          </p>
                        </div>
                        <BinaryStatus
                          active={provider.enabled}
                          activeLabel={t("securityEnabled")}
                          inactiveLabel={t("securityDisabled")}
                        />
                      </div>
                      <div className="mt-2 flex items-center gap-1.5 text-[11px] text-[var(--text-secondary)]">
                        <KeyRound className="h-3 w-3" />
                        {provider.configured
                          ? t("securityCredentialConfigured")
                          : t("securityCredentialMissing")}
                      </div>
                      <div className="mt-2">
                        <Capabilities values={provider.capabilities} />
                      </div>
                    </article>
                  ))}
                </div>
              )}
              <PaginationControls
                page={providersPage.page}
                totalPages={providersPage.totalPages}
                start={providersPage.start}
                end={providersPage.end}
                total={providersPage.total}
                label={t("securityProvidersPaginationLabel")}
                focusTargetId="security-providers-title"
                onPageChange={providersPage.setPage}
              />
            </div>
          </div>
        </section>
      )}

      {activeSection === "audit" && (
        <section
          id="security-panel-audit"
          role="tabpanel"
          aria-labelledby="security-section-tab-audit security-audit-title"
          tabIndex={0}
          className="space-y-4"
        >
          <SectionHeading
            id="security-audit-title"
            icon={Activity}
            title={t("securityAuditTitle")}
            description={t("securityAuditDesc", { limit: 100 })}
            count={auditPage.total}
          />

          <div className="overflow-hidden rounded-xl border border-[var(--border-default)]">
            {auditQuery.isLoading ? (
              <div className="flex items-center justify-center gap-2 px-4 py-10 text-xs text-[var(--text-secondary)]">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t("securityAuditLoading")}
              </div>
            ) : auditQuery.isError ? (
              <div className="px-4 py-8 text-center">
                <p className="text-xs text-[var(--color-destructive)]">
                  {apiErrorMessage(auditQuery.error, t("securityAuditLoadFailed"))}
                </p>
                <Button
                  className="mt-3"
                  size="sm"
                  variant="outline"
                  onClick={() => void auditQuery.refetch()}
                >
                  {t("securityRetry")}
                </Button>
              </div>
            ) : auditPage.total === 0 ? (
              <div className="px-4 py-10 text-center">
                <Clock3 className="mx-auto h-5 w-5 text-[var(--text-tertiary)]" />
                <p className="mt-2 text-xs text-[var(--text-secondary)]">
                  {t("securityAuditEmpty")}
                </p>
              </div>
            ) : (
              <>
                <div className="divide-y divide-[var(--border-default)]">
                  {auditPage.items.map((event) => {
                    const eventTime = formatTime(event.time_created, i18n.language);
                    const successful = ["success", "allowed", "completed", "ok"].includes(
                      event.outcome.toLowerCase(),
                    );
                    return (
                      <article key={event.id} className="px-4 py-3">
                        <div className="flex items-start gap-3">
                          {successful ? (
                            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-success)]" />
                          ) : (
                            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-warning)]" />
                          )}
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                              <span className="break-all font-mono text-xs font-semibold text-[var(--text-primary)]">
                                {event.action}
                              </span>
                              <Badge variant="outline">{event.capability}</Badge>
                              <Badge variant={successful ? "success" : "warning"}>
                                {event.outcome}
                              </Badge>
                            </div>
                            <p className="mt-1 break-all text-[11px] text-[var(--text-secondary)]">
                              {sourceKindLabel(event.source_kind)} · {event.source_id} ·{" "}
                              {t("securityDecision", { decision: event.decision })}
                            </p>
                            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[10px] text-[var(--text-tertiary)]">
                              {eventTime && <span>{eventTime}</span>}
                              {event.session_id && (
                                <span>
                                  {t("securitySessionId")}: {shortId(event.session_id)}
                                </span>
                              )}
                              {event.call_id && (
                                <span>
                                  {t("securityCallId")}: {shortId(event.call_id)}
                                </span>
                              )}
                            </div>
                          </div>
                        </div>
                      </article>
                    );
                  })}
                </div>
                <PaginationControls
                  page={auditPage.page}
                  totalPages={auditPage.totalPages}
                  start={auditPage.start}
                  end={auditPage.end}
                  total={auditPage.total}
                  label={t("securityAuditPaginationLabel")}
                  focusTargetId="security-audit-title"
                  onPageChange={changeAuditPage}
                />
              </>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
