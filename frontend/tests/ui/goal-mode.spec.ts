import { expect, test, type Page } from "@playwright/test";
import {
  mock苏小有Api,
  seed苏小有Storage,
  type 苏小有MockState,
} from "./fixtures/suxiaoyou-api";

async function setupGoalApp(page: Page): Promise<苏小有MockState> {
  await seed苏小有Storage(page, { force: true });
  return mock苏小有Api(page, { goalsReleased: true });
}

test.describe("local desktop Goal mode", () => {
  test.describe.configure({ timeout: 60_000 });
  test.skip(
    ({ isMobile }) => isMobile,
    "The local desktop Goal control plane is covered in the desktop project.",
  );

  test("creates with /目标, safely pauses with Stop, resumes, and clears", async ({
    page,
  }) => {
    const state = await setupGoalApp(page);
    await page.goto("/c/session-alpha");

    await page.locator('input[type="file"]').setInputFiles({
      name: "goal-context.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("release checklist and acceptance evidence\n"),
    });
    await expect(page.getByText("goal-context.txt")).toBeVisible();

    const composer = page.getByPlaceholder(/Describe the result you want/i);
    await composer.fill("/goal pause");
    await page.getByRole("button", { name: "Send message" }).click();
    await expect(page.getByText(/Attachments can only be used when creating a goal/)).toBeVisible();
    await expect(page.getByText("goal-context.txt")).toBeVisible();
    expect(state.goalStarts).toHaveLength(0);

    await composer.fill("/目标 完成稳定版发布审计");
    const startResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/api/chat/goal")
        && response.request().method() === "POST"
        && response.status() === 201,
    );
    await page.getByRole("button", { name: "Send message" }).click();
    await startResponse;

    await expect.poll(() => state.goalStarts.length).toBe(1);
    expect(state.promptBodies).toHaveLength(0);
    expect(state.goalStarts[0]).toMatchObject({
      session_id: "session-alpha",
      objective: "完成稳定版发布审计",
      attachments: [
        expect.objectContaining({
          file_id: "file-1",
          name: "goal-context.txt",
          path: "/tmp/suxiaoyou-ui/goal-context.txt",
          mime_type: "text/plain",
        }),
      ],
    });
    expect(state.fileUploads).toEqual(["goal-context.txt"]);
    const messageList = page.getByTestId("message-list-scroller");
    await expect(
      messageList.getByText("完成稳定版发布审计", { exact: true }),
    ).toBeVisible();
    await expect(messageList.getByText("goal-context.txt", { exact: true })).toBeVisible();
    await expect(
      messageList.getByText("Summarize the quarterly plan", { exact: true }),
    ).toBeVisible();
    await expect(
      messageList.getByText(
        "The plan has three priorities: retention, onboarding, and pricing clarity.",
        { exact: true },
      ),
    ).toBeVisible();

    const goalStatus = page.getByTestId("goal-status-control");
    await expect(goalStatus).toBeVisible();
    await expect(goalStatus).toContainText("Running");
    await expect(goalStatus).not.toHaveClass(/-mx-|-my-/);
    await expect(goalStatus.getByRole("status")).not.toHaveClass(/rounded-full|border-/);
    await goalStatus.click();

    const goalCard = page.getByTestId("goal-card");
    await expect(goalCard).toBeVisible();
    await expect(goalCard).toContainText("完成稳定版发布审计");
    const goalRunStatus = goalCard.getByTestId("goal-run-status");
    await expect(goalRunStatus.getByRole("status")).toHaveText("Running");
    await expect(goalRunStatus.getByRole("status")).not.toHaveClass(/rounded-full|border-|brand-primary/);
    await expect(goalCard).toContainText("37,614 / 250,000 tokens");
    await expect(goalCard.getByTestId("goal-token-breakdown")).toHaveCount(0);
    await expect(goalCard).not.toContainText("Uncached input");

    await expect(page.getByRole("button", { name: "Stop" })).toBeVisible();
    await page.getByRole("button", { name: "Stop" }).click();
    await expect.poll(() => state.goalPauses.length).toBe(1);
    expect(state.abortRequests).toHaveLength(0);
    expect(state.goalPauses[0]).toMatchObject({
      session_id: "session-alpha",
      expected_revision: 2,
    });
    await expect(goalRunStatus.getByRole("status")).toHaveText("Paused");
    await expect(page.getByRole("button", { name: "Stop" })).toHaveCount(0);

    const resumeResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/api/sessions/session-alpha/goal/resume")
        && response.status() === 200,
    );
    await goalCard.getByRole("button", { name: "Resume" }).click();
    await resumeResponse;
    await expect.poll(() => state.goalResumes.length).toBe(1);
    await expect(goalRunStatus.getByRole("status")).toHaveText("Running");
    await expect(page.getByRole("button", { name: "Stop" })).toBeVisible();

    // A resumed autonomous Goal is running again, so clear remains protected
    // until a second safe boundary is reached.
    await page.getByRole("button", { name: "Stop" }).click();
    await expect.poll(() => state.goalPauses.length).toBe(2);
    expect(state.abortRequests).toHaveLength(0);
    await expect(goalRunStatus.getByRole("status")).toHaveText("Paused");
    await expect(page.getByRole("button", { name: "Stop" })).toHaveCount(0);

    await goalCard.getByRole("button", { name: "Clear goal" }).click();
    const confirmation = page.getByRole("dialog", { name: "Clear this goal?" });
    await expect(confirmation).toBeVisible();
    const clearResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/api/sessions/session-alpha/goal?")
        && response.request().method() === "DELETE"
        && response.status() === 204,
    );
    await confirmation.getByRole("button", { name: "Clear goal" }).click();
    await clearResponse;

    expect(state.goalClears).toEqual(["session-alpha"]);
    await expect(page.getByTestId("goal-card-empty")).toBeVisible();
    await expect(page.getByTestId("goal-status-control")).toHaveCount(0);
  });

  test("accepts a large explicit budget when the server has no token ceiling", async ({ page }) => {
    const state = await setupGoalApp(page);
    await page.goto("/c/session-alpha");

    const composer = page.getByPlaceholder(/Describe the result you want/i);
    await composer.fill("/目标 完成可恢复的预算编辑流程");
    const startResponse = page.waitForResponse(
      (response) => response.url().includes("/api/chat/goal") && response.status() === 201,
    );
    await page.getByRole("button", { name: "Send message" }).click();
    await startResponse;

    await page.getByTestId("goal-status-control").click();
    const goalCard = page.getByTestId("goal-card");
    await goalCard.getByRole("button", { name: "Edit goal" }).click();

    const dialog = page.getByRole("dialog", { name: "Edit goal" });
    const budget = dialog.getByRole("spinbutton", {
      name: /total-context token budget/i,
    });
    const save = dialog.getByRole("button", { name: "Save goal" });
    await expect(dialog).toContainText("There is no token limit");

    await budget.fill("2500000");
    await expect(save).toBeEnabled();
    const updateResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/api/sessions/session-alpha/goal")
        && response.request().method() === "PATCH"
        && response.status() === 200,
    );
    await save.click();
    await updateResponse;

    await expect(dialog).toHaveCount(0);
    await expect.poll(() => state.goalUpdates.length).toBe(1);
    expect(state.goalUpdates[0]).toMatchObject({
      session_id: "session-alpha",
      expected_revision: 2,
      token_budget: 2_500_000,
    });
  });
});
