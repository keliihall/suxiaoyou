import { expect, test, type Page, type Route } from "@playwright/test";

import { mock苏小有Api, seed苏小有Storage } from "./fixtures/suxiaoyou-api";

async function mockRuntimeApi(page: Page) {
  const previewBodies: unknown[] = [];
  let executeCalls = 0;

  const fulfill = (route: Route, body: unknown) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });

  await page.route("**/api/runtime/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/runtime/context") {
      expect(url.searchParams.get("session_id")).toBe("session-alpha");
      return fulfill(route, {
        session_id: "session-alpha",
        workspace_instance_id: "workspace-instance-alpha",
        workspace_kind: "direct",
        checkpoint_rewind_released: true,
        managed_worktrees_released: true,
        worktree_creation_available: false,
        worktree_creation_reason: "repository_not_supported",
        external_side_effects_reverted: false,
      });
    }
    if (url.pathname === "/api/runtime/checkpoints") {
      expect(url.searchParams.get("session_id")).toBe("session-alpha");
      expect(url.searchParams.get("workspace_instance_id")).toBe(
        "workspace-instance-alpha",
      );
      const validationSummaries = [
        { overall_status: "pass", completed_count: 1, failed_count: 0, cancelled_count: 0, verdict_counts: { pass: 1, fail: 0, needs_review: 0 } },
        { overall_status: "fail", completed_count: 1, failed_count: 0, cancelled_count: 0, verdict_counts: { pass: 0, fail: 1, needs_review: 0 } },
        { overall_status: "needs_review", completed_count: 1, failed_count: 0, cancelled_count: 0, verdict_counts: { pass: 0, fail: 0, needs_review: 1 } },
        { overall_status: "failed_closed", completed_count: 0, failed_count: 1, cancelled_count: 0, verdict_counts: { pass: 0, fail: 0, needs_review: 0 } },
        { overall_status: "cancelled", completed_count: 0, failed_count: 0, cancelled_count: 1, verdict_counts: { pass: 0, fail: 0, needs_review: 0 } },
        { overall_status: "invalid", completed_count: 0, failed_count: 0, cancelled_count: 0, verdict_counts: { pass: 0, fail: 0, needs_review: 0 } },
        { overall_status: "not_requested", completed_count: 0, failed_count: 0, cancelled_count: 0, verdict_counts: { pass: 0, fail: 0, needs_review: 0 } },
      ];
      return fulfill(route, {
        checkpoints: validationSummaries.map((validation, index) => ({
          checkpoint_id: index === 0 ? "checkpoint-alpha-3" : `checkpoint-${validation.overall_status}`,
          sequence: 3 + index,
          state: "finalized",
          pin_state: "pinned",
          has_irreversible_side_effects: index === 0,
          external_side_effects: index === 0 ? [{ capability: "network", outcome: "sent" }] : [],
          validation: { ...validation, count: 1 },
        })),
        external_side_effects_are_reverted: false,
      });
    }
    if (url.pathname === "/api/runtime/rewind/preview") {
      previewBodies.push(request.postDataJSON());
      return fulfill(route, {
        target_checkpoint_id: "checkpoint-alpha-3",
        paths: [],
        conflicts: [
          { relative_path: "report.docx", reason: "workspace changed after checkpoint" },
        ],
        blockers: [],
        can_execute: false,
        already_rewound: false,
        external_side_effects: [{ capability: "network", outcome: "sent" }],
        external_side_effects_will_be_reverted: false,
      });
    }
    if (url.pathname === "/api/runtime/rewind/execute") {
      executeCalls += 1;
      return fulfill(route, { completed: true });
    }
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Unhandled runtime test route" }),
    });
  });

  return {
    previewBodies,
    executeCalls: () => executeCalls,
  };
}

test.beforeEach(async ({ page, isMobile }) => {
  test.skip(!!isMobile, "The desktop workspace control panel is hidden on mobile");
  await seed苏小有Storage(page, { force: true });
  await mock苏小有Api(page);
});

test("runtime control binds rewind preview to the active workspace and refuses conflicts", async ({
  page,
}) => {
  const state = await mockRuntimeApi(page);

  await page.goto("/c/session-alpha");
  const runtime = page.getByRole("button", { name: /Versions & recovery/ });
  await expect(runtime).toContainText("7 recoverable versions");
  await runtime.click();

  await expect(page.getByText("Automatic protection is on")).toBeVisible();
  await expect(page.getByRole("list").getByText("Version 3", { exact: true })).toBeVisible();
  for (const label of [
    "Check passed",
    "Check failed",
    "Needs confirmation",
    "Safety check incomplete; stopped",
    "Check cancelled",
    "Check record invalid",
  ]) {
    await expect(page.getByText(label, { exact: true })).toBeVisible();
  }
  await expect(page.getByTestId("runtime-validation-not_requested")).toHaveCount(0);
  await expect(
    page.getByText("Sent email, cloud changes, and similar actions cannot be undone automatically."),
  ).toBeVisible();

  await page.getByRole("button", { name: "Restore here", exact: true }).first().click();
  await expect.poll(() => state.previewBodies).toEqual([
    {
      session_id: "session-alpha",
      workspace_instance_id: "workspace-instance-alpha",
      checkpoint_id: "checkpoint-alpha-3",
    },
  ]);
  await expect(page.getByText("This version cannot be restored safely right now.")).toBeVisible();
  await expect(page.getByText("workspace changed after checkpoint")).toHaveCount(0);
  expect(state.executeCalls()).toBe(0);
});
