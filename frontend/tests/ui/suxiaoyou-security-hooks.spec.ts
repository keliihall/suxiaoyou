import { expect, test, type Page, type Route } from "@playwright/test";

import { mock苏小有Api, seed苏小有Storage } from "./fixtures/suxiaoyou-api";

async function mockHookSecurityApi(page: Page) {
  let approved = true;
  const revokeBodies: unknown[] = [];

  const fulfill = (route: Route, body: unknown) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });

  await page.route("**/api/security/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/security/overview") {
      return fulfill(route, {
        state: {
          emergency_stop: false,
          disabled_tools: [],
          updated_at: null,
          degraded_reason: null,
        },
        warnings: [],
        source_profiles: [],
        tools: [],
        connectors: [],
        providers: [],
        automations: { enabled_count: 0, runtime_running: false },
        release_gates: {
          remote_access: false,
          messaging_channels: false,
          goals: false,
          autonomous_goals: false,
          v11_checkpoints: false,
          v11_rewind: false,
          v11_hooks: true,
          v11_acp: false,
          v11_worktrees: false,
          v11_validation_agent: false,
          v11_office_v2: false,
        },
      });
    }
    if (url.pathname === "/api/security/audit") {
      return fulfill(route, { events: [] });
    }
    if (url.pathname === "/api/security/hooks" && request.method() === "GET") {
      expect(url.searchParams.get("session_id")).toBe("session-alpha");
      return fulfill(route, {
        session_id: "session-alpha",
        trust_store_available: true,
        hooks: [
          {
            hook_id: "preflight-format",
            event: "PreToolUse",
            source: "project",
            failure_policy: "required",
            timeout_seconds: 8,
            fingerprint: "a".repeat(64),
            approval_state: approved ? "approved" : "required",
          },
        ],
      });
    }
    if (
      url.pathname === "/api/security/hooks/revoke" &&
      request.method() === "POST"
    ) {
      const body = request.postDataJSON();
      revokeBodies.push(body);
      approved = false;
      return fulfill(route, {
        session_id: "session-alpha",
        hook_id: "preflight-format",
        revoked: true,
      });
    }
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Unhandled security test route" }),
    });
  });

  return { revokeBodies };
}

test.beforeEach(async ({ page, isMobile }) => {
  test.skip(!!isMobile, "The desktop Settings sidebar is hidden on mobile");
  await seed苏小有Storage(page, { force: true });
  await mock苏小有Api(page);
});

test("Security Center retains the originating task and revokes only a redacted Hook identity", async ({
  page,
}) => {
  const state = await mockHookSecurityApi(page);

  await page.goto("/c/session-alpha");
  await expect(page.getByText("Summarize the quarterly plan")).toBeVisible();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Security Center", exact: true }).click();
  await page.getByRole("tab", { name: "Connections", exact: true }).click();

  await expect(page.getByText("Project Hook trust")).toBeVisible();
  await expect(page.getByText("preflight-format", { exact: true })).toBeVisible();
  await expect(page.getByText("Exact fingerprint approved")).toBeVisible();
  await expect(page.getByText(/fingerprint: aaaaaa…aaaaa/)).toBeVisible();
  await expect(page.getByText(/review-probe|\.suxiaoyou|\/Users\//i)).toHaveCount(0);

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Revoke trust", exact: true }).click();

  await expect.poll(() => state.revokeBodies).toEqual([
    { session_id: "session-alpha", hook_id: "preflight-format" },
  ]);
  await expect(page.getByText("Approval required")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Revoke trust", exact: true }),
  ).toBeDisabled();
});
