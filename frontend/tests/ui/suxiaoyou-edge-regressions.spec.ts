import { expect, test, type Page } from "@playwright/test";
import {
  mock苏小有Api,
  seed苏小有Storage,
  type 苏小有MockOptions,
  type 苏小有MockState,
} from "./fixtures/suxiaoyou-api";

async function setupMockedApp(
  page: Page,
  options?: 苏小有MockOptions,
  seedOptions?: Parameters<typeof seed苏小有Storage>[1],
): Promise<苏小有MockState> {
  await seed苏小有Storage(page, seedOptions);
  return mock苏小有Api(page, options);
}

async function expectNoAppCrash(page: Page) {
  await expect(page.getByText("Runtime", { exact: false })).toHaveCount(0);
  await expect(page.getByText("API 401", { exact: false })).toHaveCount(0);
}

test.describe("苏小有 edge-state GUI regressions", () => {
  test.describe.configure({ timeout: 75_000 });

  test("a late files response from the previous session cannot overwrite the focused workspace", async ({ page }) => {
    await setupMockedApp(page);

    let releaseAlphaFiles!: () => void;
    const alphaFilesGate = new Promise<void>((resolve) => {
      releaseAlphaFiles = resolve;
    });
    let markAlphaFilesRequested!: () => void;
    const alphaFilesRequested = new Promise<void>((resolve) => {
      markAlphaFilesRequested = resolve;
    });

    await page.route("**/api/sessions/session-alpha/files", async (route) => {
      markAlphaFilesRequested();
      await alphaFilesGate;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          files: [{
            name: "alpha-only.txt",
            path: "/Users/alex/suxiaoyou-demo/alpha-only.txt",
            type: "generated",
            tool: "write",
          }],
        }),
      });
    });
    await page.route("**/api/sessions/session-beta/files", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ files: [] }),
      }),
    );

    await page.goto("/c/session-alpha");
    await alphaFilesRequested;

    const betaFilesResponse = page.waitForResponse((response) =>
      response.url().endsWith("/api/sessions/session-beta/files")
      && response.status() === 200,
    );
    await page.getByText("Invoice cleanup", { exact: true }).click();
    await expect(page).toHaveURL(/\/c\/session-beta$/);
    await betaFilesResponse;
    await expect(page.getByRole("button", { name: "Hide workspace" })).toBeVisible();

    const lateAlphaResponse = page.waitForResponse((response) =>
      response.url().endsWith("/api/sessions/session-alpha/files")
      && response.status() === 200,
    );
    releaseAlphaFiles();
    await lateAlphaResponse;

    await expect(page.getByText("alpha-only.txt", { exact: true })).toHaveCount(0);
    await expect(page).toHaveURL(/\/c\/session-beta$/);
  });

  test("a background stream cannot project its todos, files, tasks, or artifact into the focused session", async ({ page }) => {
    await setupMockedApp(page, {
      activeJobs: [{
        stream_id: "stream-background-projection",
        session_id: "session-alpha",
      }],
    });

    let releaseBackgroundStream!: () => void;
    const backgroundStreamGate = new Promise<void>((resolve) => {
      releaseBackgroundStream = resolve;
    });
    let markBackgroundStreamRequested!: () => void;
    const backgroundStreamRequested = new Promise<void>((resolve) => {
      markBackgroundStreamRequested = resolve;
    });
    const event = (id: number, name: string, data: Record<string, unknown>) =>
      `id: ${id}\nevent: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
    let backgroundActive = true;

    await page.route("**/api/chat/active", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(backgroundActive
          ? [{
            stream_id: "stream-background-projection",
            session_id: "session-alpha",
          }]
          : []),
      }),
    );

    await page.route(
      "**/api/chat/stream/stream-background-projection",
      async (route) => {
        markBackgroundStreamRequested();
        await backgroundStreamGate;
        backgroundActive = false;
        await route.fulfill({
          status: 200,
          headers: {
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
          },
          body: [
            event(1, "tool-call", {
              tool: "artifact",
              call_id: "background-artifact",
              arguments: {
                command: "create",
                type: "code",
                title: "Background A artifact",
                content: "background A content",
              },
            }),
            event(2, "tool-call", {
              tool: "todo",
              call_id: "background-todo",
              arguments: {},
            }),
            event(3, "tool-result", {
              tool: "todo",
              call_id: "background-todo",
              output: "updated",
              metadata: {
                todos: [{
                  content: "Background A todo",
                  status: "in_progress",
                  activeForm: "Running background A",
                }],
              },
            }),
            event(4, "task-batch-start", {
              batch_id: "background-batch",
              mode: "parallel",
              tasks: [{
                task_id: "background-task",
                session_id: "session-alpha",
                title: "Background A agent task",
                agent: "general",
                status: "running",
              }],
            }),
            event(5, "tool-call", {
              tool: "bash",
              call_id: "background-write",
              arguments: { command: "create background-a.txt" },
            }),
            event(6, "tool-result", {
              tool: "bash",
              call_id: "background-write",
              output: "created",
              metadata: { written_files: ["background-a.txt"] },
            }),
            event(7, "done", {
              session_id: "session-alpha",
              finish_reason: "stop",
            }),
          ].join(""),
        });
      },
    );
    await page.route("**/api/sessions/session-beta/todos", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ todos: [] }),
      }),
    );
    await page.route("**/api/sessions/session-beta/files", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ files: [] }),
      }),
    );
    await page.route("**/api/sessions/session-alpha/todos", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          todos: [{
            content: "Background A todo",
            status: "in_progress",
            activeForm: "Running background A",
          }],
        }),
      }),
    );
    await page.route("**/api/sessions/session-alpha/files", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          files: [{
            name: "background-a.txt",
            path: "/Users/alex/suxiaoyou-demo/background-a.txt",
            type: "generated",
            tool: "bash",
          }],
        }),
      }),
    );

    await page.goto("/c/session-beta");
    await backgroundStreamRequested;
    await expect(page).toHaveURL(/\/c\/session-beta$/);
    await expect(page.getByRole("button", { name: "Show workspace" })).toBeVisible();

    const streamResponse = page.waitForResponse((response) =>
      response.url().endsWith("/api/chat/stream/stream-background-projection")
      && response.status() === 200,
    );
    releaseBackgroundStream();
    await streamResponse;
    await page.waitForTimeout(250);

    await expect(page.getByRole("button", { name: "Show workspace" })).toBeVisible();
    await expect(page.getByText("Background A artifact", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Show workspace" }).click();
    await expect(page.getByText("Background A todo", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Background A agent task", { exact: true })).toHaveCount(0);
    await expect(page.getByText("background-a.txt", { exact: true })).toHaveCount(0);

    await page.getByText("Quarterly planning notes", { exact: true }).click();
    await expect(page).toHaveURL(/\/c\/session-alpha$/);
    await expect(page.getByText(/background-a\.txt/)).toBeVisible();
    await page.getByText("Progress", { exact: true }).click();
    await expect(page.getByText("Background A todo", { exact: true })).toBeVisible();
  });

  test("auth expiry workflow: backend 401 while sending is recoverable and keeps the composer usable", async ({ page }) => {
    await setupMockedApp(page, {
      promptErrors: [{ match: "expired auth", status: 401, detail: "Session expired" }],
    });

    await page.goto("/c/new");
    await page.getByPlaceholder(/Describe the result you want/i).fill("expired auth should not crash");
    const failedPrompt = page.waitForResponse((res) =>
      res.url().includes("/api/chat/prompt") && res.status() === 401,
    );
    await page.getByRole("button", { name: /Send message/i }).click();
    await failedPrompt;

    await expect(page.getByText(/Session expired|API 401/i)).toBeVisible();
    await expect(page.getByPlaceholder(/Describe the result you want/i)).toBeVisible();
    await expectNoAppCrash(page);
  });

  test("mobile needs-input workflow: task list badge opens the detail prompt and responds from the GUI", async ({ page }) => {
    const state = await setupMockedApp(page, {
      activeJobs: [{ stream_id: "stream-question", session_id: "session-alpha", needs_input: true }],
    });

    await page.goto("/m?token=remote-token");
    await expect(page.getByRole("heading", { name: "suyo" })).toBeVisible();
    await expect(page.getByText("Needs input")).toBeVisible();
    await page.getByText("Quarterly planning notes").click();

    await expect(page.getByText("Agent is asking")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText("Which release channel should this automation watch?")).toBeVisible();
    const response = page.waitForResponse((res) =>
      res.url().includes("/api/chat/respond") && res.status() === 200,
    );
    await page.getByRole("button", { name: /Stable/i }).click();
    await response;

    expect(state.chatResponses).toHaveLength(1);
    await expectNoAppCrash(page);
  });

  test("mobile remote disconnect workflow: an unreachable desktop tunnel shows disconnected health without leaving tasks", async ({ page }) => {
    await setupMockedApp(page, { remoteProviderInfoStatus: 503 });

    await page.goto("/m?token=remote-token");
    await expect(page.getByRole("heading", { name: "suyo" })).toBeVisible();
    await expect(page.getByText("Quarterly planning notes")).toBeVisible();
    await expect(page.locator('span[title="disconnected"]')).toBeVisible({ timeout: 20_000 });
    await expectNoAppCrash(page);
  });

  test("connector auth failure workflow: failed OAuth is surfaced as a toast instead of an unhandled UI error", async ({ page }) => {
    await setupMockedApp(page, {
      connectorErrors: [{ match: "notion/connect", status: 500, detail: "Notion OAuth unavailable" }],
    });

    await page.goto("/settings?tab=plugins");
    await expect(page.getByRole("heading", { name: "Plugins" })).toBeVisible();
    await page.locator('input[placeholder="Search..."]:visible').fill("notion");
    const notionRow = page.locator("div").filter({ hasText: "Notion" }).filter({ hasText: "Search and update pages" }).first();
    await expect(notionRow).toBeVisible();
    await notionRow.getByRole("switch").click();

    await expect(page.getByText("Notion OAuth unavailable")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Plugins" })).toBeVisible();
    await expectNoAppCrash(page);
  });

  test("chatgpt auth launch failure stops the waiting state", async ({ page }) => {
    await setupMockedApp(page, {
      openaiSubscriptionConnected: false,
      openaiLoginStatus: 500,
    });

    await page.goto("/settings?tab=providers");
    await page.getByRole("button", { name: /ChatGPT Subscription/i }).click();
    await page.getByRole("button", { name: "Sign in with ChatGPT" }).click();

    await expect(page.getByText("Failed to start authentication")).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign in with ChatGPT" })).toBeEnabled();
    await expect(page.getByText("Waiting for authentication...")).toHaveCount(0);
    await expectNoAppCrash(page);
  });

  test("ollama status failure shows a retryable error instead of an endless spinner", async ({ page }) => {
    await setupMockedApp(page, {
      ollamaStatusCode: 500,
    });

    await page.goto("/settings?tab=providers");
    await page.getByRole("button", { name: "Ollama" }).click();

    await expect(page.getByText("Failed to load Ollama status.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
    await expectNoAppCrash(page);
  });
});
