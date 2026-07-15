import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const apiSource = readFileSync("src/lib/security-api.ts", "utf8");
const hookSource = readFileSync("src/hooks/use-security.ts", "utf8");
const typesSource = readFileSync("src/types/security.ts", "utf8");
const componentSource = readFileSync(
  "src/components/settings/security-tab.tsx",
  "utf8",
);
const layoutSource = readFileSync(
  "src/components/settings/settings-layout.tsx",
  "utf8",
);
const tabsSource = readFileSync(
  "src/components/settings/settings-tabs.ts",
  "utf8",
);

test("security API uses the v1 overview, audit, tool, and emergency-stop contracts", () => {
  assert.match(apiSource, /overview: "\/api\/security\/overview"/);
  assert.match(apiSource, /audit: \(limit: number\) => `\/api\/security\/audit\?limit=\$\{limit\}`/);
  assert.match(apiSource, /encodeURIComponent\(id\)/);
  assert.match(apiSource, /api\.put<SecurityOverview>\(SECURITY_API\.tool\(id\), \{ enabled \}\)/);
  assert.match(apiSource, /api\.post<SecurityOverview>\(SECURITY_API\.emergencyStop, \{ active \}\)/);
});

test("security queries refresh both overview and audit after mutations", () => {
  assert.match(hookSource, /setQueryData\(securityQueryKeys\.overview, overview\)/);
  assert.match(hookSource, /invalidateQueries\(\{ queryKey: securityQueryKeys\.audit \}\)/);
  assert.match(hookSource, /getSecurityAudit\(limit, \{ signal \}\)/);
  assert.match(hookSource, /refetchInterval: 30_000/g);
});

test("security center gates dangerous actions and never renders credential values", () => {
  assert.match(componentSource, /window\.confirm\(confirmation\)/);
  assert.match(componentSource, /window\.confirm\(t\("securityEnableToolConfirm"/);
  assert.match(componentSource, /connector\.credential_configured/);
  assert.match(componentSource, /provider\.configured/);
  assert.doesNotMatch(componentSource, /event\.details/);
  assert.doesNotMatch(componentSource, /credential_(?:value|token|secret)|api_key|password/i);
});

test("emergency-stop runtime warnings are surfaced instead of reporting success", () => {
  assert.match(typesSource, /warnings\?: string\[\]/);
  assert.match(componentSource, /result\.warnings\?\.length/);
  assert.match(componentSource, /toast\.warning/);
  assert.match(componentSource, /securityEmergencyWarning/);
});

test("security center is reachable from settings and has responsive source views", () => {
  assert.match(tabsSource, /id: "security"/);
  assert.match(layoutSource, /activeTab === "security" && <SecurityTab \/>/);
  assert.match(componentSource, /sm:grid-cols-3/);
  assert.match(componentSource, /md:grid-cols-2/);
  assert.match(componentSource, /Tools by source|securityToolsTitle/);
  assert.match(componentSource, /securityAuditTitle/);
});

test("security center keeps critical status visible and pages every growing list", () => {
  const overviewPosition = componentSource.indexOf("security-overview-title");
  const navigationPosition = componentSource.indexOf("securitySectionNavigation");

  assert.ok(overviewPosition >= 0);
  assert.ok(navigationPosition > overviewPosition);
  assert.match(componentSource, /type SecuritySectionId = "tools" \| "integrations" \| "audit"/);
  assert.match(componentSource, /role="tablist"/);
  assert.match(componentSource, /role="tab"/);
  assert.match(componentSource, /aria-selected=\{active\}/);
  assert.match(componentSource, /role="tabpanel"/);
  assert.match(componentSource, /ArrowRight/);
  assert.match(componentSource, /ArrowLeft/);
  assert.match(componentSource, /TOOLS_PAGE_SIZE = 6/);
  assert.match(componentSource, /INTEGRATIONS_PAGE_SIZE = 4/);
  assert.match(componentSource, /AUDIT_PAGE_SIZE = 10/);
  assert.match(componentSource, /useClientPagination\(sortedTools, TOOLS_PAGE_SIZE\)/);
  assert.match(componentSource, /useClientPagination\(connectors, INTEGRATIONS_PAGE_SIZE\)/);
  assert.match(componentSource, /useClientPagination\(providers, INTEGRATIONS_PAGE_SIZE\)/);
  assert.match(componentSource, /useClientPagination\(auditEvents, AUDIT_PAGE_SIZE\)/);
  assert.match(componentSource, /setPage\(\(current\) => Math\.min\(current, totalPages\)\)/);
  assert.match(componentSource, /securityPaginationPrevious/);
  assert.match(componentSource, /securityPaginationNext/);
  assert.match(componentSource, /securityPaginationStatus/);
  assert.match(componentSource, /aria-live="polite"/);
  assert.match(componentSource, /focusTargetId/);
  assert.match(componentSource, /scrollIntoView\(\{ block: "start" \}\)/);
  assert.match(componentSource, /target\?\.focus\(\{ preventScroll: true \}\)/);
  assert.match(componentSource, /auditSnapshot \?\? liveAuditEvents/);
  assert.match(componentSource, /setAuditSnapshot\(\[\.\.\.liveAuditEvents\]\)/);
  assert.match(componentSource, /connectorStatusLabel\(connector\.status\)/);
  assert.match(componentSource, /securityAuditDesc", \{ limit: 100 \}/);
});

test("security center copy is complete in English and Chinese", () => {
  const requiredKeys = [
    "tabSecurity",
    "securityOverviewTitle",
    "securitySectionNavigation",
    "securitySectionTools",
    "securitySectionIntegrations",
    "securitySectionAudit",
    "securityEmergencyStop",
    "securityEmergencyActivateConfirm",
    "securityEmergencyDeactivateConfirm",
    "securityEmergencyWarning",
    "securityToolsTitle",
    "securityEnableToolConfirm",
    "securityToolsPaginationLabel",
    "securityCredentialsBooleanDesc",
    "securityConnectorNeedsAuth",
    "securityConnectorNeedsApproval",
    "securityConnectorFailed",
    "securityConnectorsPaginationLabel",
    "securityProvidersPaginationLabel",
    "securityAuditTitle",
    "securityAuditEmpty",
    "securityAuditPaginationLabel",
    "securityPaginationPrevious",
    "securityPaginationNext",
    "securityPaginationStatus",
    "securityPaginationRange",
  ];

  for (const locale of ["en", "zh"]) {
    const messages = JSON.parse(
      readFileSync(`src/i18n/locales/${locale}/settings.json`, "utf8"),
    );
    for (const key of requiredKeys) {
      assert.equal(typeof messages[key], "string", `${locale}.${key}`);
      assert.ok(messages[key].trim().length > 0, `${locale}.${key}`);
    }
  }
});
