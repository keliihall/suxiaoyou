import { expect, test, type Page, type Route } from "@playwright/test";

import { mock苏小有Api, seed苏小有Storage } from "./fixtures/suxiaoyou-api";

interface PendingInput {
  id: string;
  session_id: string;
  client_request_id: string;
  mode: "queue" | "steer";
  status: "queued" | "applying" | "blocked";
  position: number;
  text: string;
  attachments: unknown[];
  target_stream_id: string | null;
  error_message: string | null;
}

async function installSessionInputApi(
  page: Page,
  initial: PendingInput[] = [],
  options: { idleOnPost?: boolean; loseDeleteResponse?: boolean } = {},
) {
  let items = [...initial];
  const posts: Array<Record<string, unknown>> = [];
  const patches: Array<Record<string, unknown>> = [];
  let getCount = 0;

  await page.route("**/api/chat/inputs**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const path = url.pathname;

    if (path === "/api/chat/inputs" && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      posts.push(body);
      if (options.idleOnPost) {
        await route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            detail: {
              code: "session_idle",
              message: "The current task already finished; send this as a normal message.",
            },
          }),
        });
        return;
      }
      const response: PendingInput = {
        id: `input-${posts.length}`,
        session_id: String(body.session_id),
        client_request_id: String(body.client_request_id),
        mode: body.mode === "steer" ? "steer" : "queue",
        status: "queued",
        position: items.length + 1,
        text: String(body.text ?? ""),
        attachments: Array.isArray(body.attachments) ? body.attachments : [],
        target_stream_id: body.mode === "steer" ? "stream-slow" : null,
        error_message: null,
      };
      items = [...items, response];
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(response),
      });
      return;
    }

    const match = path.match(/^\/api\/chat\/inputs\/([^/]+)(?:\/([^/]+))?$/);
    if (match && method === "GET") {
      getCount += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(items),
      });
      return;
    }
    if (match && match[2] && method === "DELETE") {
      items = items.filter((item) => item.id !== decodeURIComponent(match[2]!));
      if (options.loseDeleteResponse) {
        await route.abort("connectionreset");
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "cancelled", input_id: match[2] }),
      });
      return;
    }
    if (match && match[2] && method === "PATCH") {
      const inputId = decodeURIComponent(match[2]);
      const body = request.postDataJSON() as { mode?: "queue" | "steer"; move?: "up" | "down"; position?: number };
      patches.push({ inputId, ...body });
      const index = items.findIndex((item) => item.id === inputId);
      if (index < 0) {
        await route.fulfill({ status: 409, body: "{}" });
        return;
      }
      if (body.mode) {
        items[index] = {
          ...items[index],
          mode: body.mode,
          target_stream_id: body.mode === "steer" ? "stream-slow" : null,
        };
      }
      if (body.move) {
        const neighbor = body.move === "up" ? index - 1 : index + 1;
        if (neighbor >= 0 && neighbor < items.length) {
          const currentPosition = items[index].position;
          items[index].position = items[neighbor].position;
          items[neighbor].position = currentPosition;
          items.sort((a, b) => a.position - b.position);
        }
      }
      if (body.position) {
        const [moving] = items.splice(index, 1);
        const targetIndex = Math.min(Math.max(body.position - 1, 0), items.length);
        items.splice(targetIndex, 0, moving);
        items.forEach((item, itemIndex) => {
          item.position = itemIndex + 1;
        });
      }
      const updated = items.find((item) => item.id === inputId)!;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(updated),
      });
      return;
    }
    await route.fallback();
  });

  return {
    posts,
    patches,
    get getCount() {
      return getCount;
    },
  };
}

async function startSlowTask(page: Page) {
  await page.goto("/c/new");
  await page.getByPlaceholder(/Describe the result you want/i).fill("Start a slow stream");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page).toHaveURL(/\/c\/session-new/);
  await expect(page.getByText("Starting a deliberately slow GUI stream.")).toBeVisible();
}

test.describe("long-task follow-up queue", () => {
  test.skip(({ isMobile }) => isMobile, "Desktop composer queue is covered in the desktop project");

  test.beforeEach(async ({ page }) => {
    await seed苏小有Storage(page, { force: true });
  });

  test("defaults to queue, then supports edit, steer, order, and cancel from the queue", async ({ page }) => {
    await mock苏小有Api(page);
    const inputs = await installSessionInputApi(page);
    await startSlowTask(page);

    const composer = page.getByPlaceholder("The task is running — add a follow-up");
    await expect(composer).toBeEditable();
    await expect(page.getByRole("button", { name: "Stop" })).toBeVisible();

    await composer.fill("Run the validation suite after the current task");
    await page.getByRole("button", { name: "Queue follow-up" }).click();
    await expect.poll(() => inputs.posts.length).toBe(1);
    expect(inputs.posts[0]?.mode).toBe("queue");
    await expect(page.getByTestId("pending-inputs")).toContainText(
      "Run the validation suite after the current task",
    );
    await page.getByRole("button", { name: "More actions for queued message 1" }).click();
    await expect(page.getByRole("menuitem", { name: "Edit" })).toBeVisible();
    await page.mouse.click(500, 500);
    await expect(page.getByRole("menuitem", { name: "Edit" })).toHaveCount(0);
    await expect(page.getByTestId("input-delivery-mode")).toHaveCount(0);

    await composer.fill("Prioritize the data-loss check first");
    await page.getByRole("button", { name: "Queue follow-up" }).click();
    await expect.poll(() => inputs.posts.length).toBe(2);
    expect(inputs.posts[1]?.mode).toBe("queue");
    await expect(page.getByTestId("pending-inputs")).toContainText(
      "Prioritize the data-loss check first",
    );

    await page.getByRole("button", { name: "More actions for queued message 2" }).click();
    await page.getByRole("menuitem", { name: "Steer" }).click();
    await expect.poll(() => inputs.patches.length).toBe(1);
    expect(inputs.patches[0]).toMatchObject({ mode: "steer" });

    const queueRows = page.getByTestId("pending-inputs").locator("li");
    await queueRows.nth(1).dragTo(queueRows.nth(0));
    await expect.poll(() => inputs.patches.length).toBe(2);
    expect(inputs.patches[1]).toMatchObject({ position: 1 });
    await expect(page.getByTestId("pending-inputs").locator("li").first()).toContainText(
      "Prioritize the data-loss check first",
    );

    await page.getByRole("button", { name: "More actions for queued message 2" }).click();
    await page.getByRole("menuitem", { name: "Cancel" }).click();
    await expect(page.getByTestId("pending-inputs")).not.toContainText(
      "Run the validation suite after the current task",
    );
    await expect(page.getByRole("button", { name: "Stop" })).toBeVisible();
  });

  test("keeps a stalled hint quiet and separate from queued-message guidance", async ({ page }) => {
    await page.clock.install();
    const apiState = await mock苏小有Api(page);
    await page.route("**/api/chat/active", async (route) => {
      const slowTaskStarted = apiState.promptBodies.some((body) =>
        /slow stream/i.test(String((body as Record<string, unknown> | null)?.text ?? "")),
      );
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(slowTaskStarted
          ? [{ stream_id: "stream-slow", session_id: "session-new" }]
          : []),
      });
    });
    await page.route("**/api/chat/stream/stream-slow**", async (route) => {
      const lastEventId = new URL(route.request().url()).searchParams.get("last_event_id");
      if (!lastEventId) {
        await route.fallback();
        return;
      }
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream",
          "cache-control": "no-cache",
        },
        body: [
          "id: 3",
          "event: heartbeat",
          'data: {"status":"ok"}',
          "",
          "",
        ].join("\n"),
      });
    });
    const inputs = await installSessionInputApi(page);
    await startSlowTask(page);

    const composer = page.getByPlaceholder("The task is running — add a follow-up");
    await composer.fill("Run this after the current task");
    await page.getByRole("button", { name: "Queue follow-up" }).click();
    await expect.poll(() => inputs.posts.length).toBe(1);

    const queue = page.getByTestId("pending-inputs");
    await expect(queue).toBeVisible();
    await expect(queue.getByTestId("pending-inputs-guidance")).toContainText(
      "Runs in order after the current task",
    );

    await page.clock.runFor(65_000);

    const stalled = page.getByTestId("progress-stalled-notice");
    await expect(stalled).toBeVisible();
    await expect(stalled).toHaveClass(/rounded-lg/);
    await expect(stalled).toContainText("Still running with no new progress");
    await expect(stalled).not.toContainText(/queued|steer/i);
    await expect(stalled.getByRole("button")).toHaveCount(0);
    await expect(stalled.getByTestId("progress-stalled-duration")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    await expect(page.getByRole("button", { name: "Stop" })).toHaveCount(1);
    await expect(queue).toBeVisible();
  });

  test("keeps interaction acknowledgements authoritative without blocking queued edits", async ({ page }) => {
    await page.clock.install();
    await mock苏小有Api(page, {
      activeJobs: [
        {
          stream_id: "stream-permission",
          session_id: "session-new",
          needs_input: true,
        },
      ],
    });
    await page.route("**/api/chat/stream/stream-permission**", async (route) => {
      const lastEventId = new URL(route.request().url()).searchParams.get("last_event_id");
      if (!lastEventId) {
        await route.fallback();
        return;
      }
      // Reconnect proves transport liveness without replaying the original
      // permission card or supplying a continuation event.
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream",
          "cache-control": "no-cache",
        },
        body: [
          "id: 3",
          "event: heartbeat",
          'data: {"status":"ok"}',
          "",
          "",
        ].join("\n"),
      });
    });
    await installSessionInputApi(page);

    await page.goto("/c/new");
    await page.getByRole("button", { name: /Auto-edit/i }).click();
    await page.getByRole("button", { name: /Ask first/i }).click();
    await page.getByPlaceholder(/Describe the result you want/i).fill(
      "Trigger permission flow after a long wait",
    );
    await page.getByRole("button", { name: "Send message" }).click();
    await expect(page).toHaveURL(/\/c\/session-new/);
    await expect(page.getByText("Permission Required")).toBeVisible();

    const composer = page.getByPlaceholder("The task is running — add a follow-up");
    await composer.fill("Keep this queued while confirmation recovers");
    await page.getByRole("button", { name: "Queue follow-up" }).click();
    const queue = page.getByTestId("pending-inputs");
    await expect(queue).toContainText("Keep this queued while confirmation recovers");

    // Waiting for a user decision is not a stalled task, even after the normal
    // no-progress threshold has elapsed.
    await page.clock.runFor(65_000);
    await expect(page.getByTestId("progress-stalled-notice")).toHaveCount(0);

    const respond = page.waitForResponse(
      (response) => response.url().includes("/api/chat/respond") && response.status() === 200,
    );
    await page.getByRole("button", { name: /Allow/i }).click();
    await respond;
    await expect(page.getByText("Confirmed", { exact: true })).toBeVisible();

    // The pre-confirmation progress timestamp is now old. If continuation SSE
    // is delayed, the acknowledgement remains the one truthful status instead
    // of being joined by the generic stalled notice.
    await page.clock.runFor(6_000);
    await expect(page.getByTestId("progress-stalled-notice")).toHaveCount(0);

    // Exercise the bounded recovery terminal state too: one Stop affordance,
    // no duplicate stalled status, and queued messages remain editable.
    await page.clock.runFor(17_000);
    await expect(
      page.getByText("Confirmation was submitted, but continuation status is unavailable."),
    ).toBeVisible();
    await expect(page.getByTestId("progress-stalled-notice")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Stop" })).toHaveCount(1);

    await page.getByRole("button", { name: "More actions for queued message 1" }).click();
    await page.getByRole("menuitem", { name: "Edit" }).click();
    await expect(composer).toHaveValue("Keep this queued while confirmation recovers");
    await expect(queue).toHaveCount(0);
  });

  test("restores pending inputs with GET after remount/reconnect", async ({ page }) => {
    await mock苏小有Api(page);
    const inputs = await installSessionInputApi(page, [
      {
        id: "restored-input",
        session_id: "session-alpha",
        client_request_id: "restored-request",
        mode: "queue",
        status: "queued",
        position: 1,
        text: "This follow-up survived a reconnect",
        attachments: [],
        target_stream_id: null,
        error_message: null,
      },
    ]);

    await page.goto("/c/session-alpha");
    await expect(page.getByText("This follow-up survived a reconnect")).toBeVisible();
    await page.reload();
    await expect(page.getByText("This follow-up survived a reconnect")).toBeVisible();
    expect(inputs.getCount).toBeGreaterThanOrEqual(2);
  });

  test("falls back to a normal send when the task finished before enqueue", async ({ page }) => {
    const apiState = await mock苏小有Api(page);
    const inputs = await installSessionInputApi(page, [], { idleOnPost: true });
    await startSlowTask(page);

    await page.getByPlaceholder("The task is running — add a follow-up").fill(
      "Send normally if the previous task already finished",
    );
    await page.getByRole("button", { name: "Queue follow-up" }).click();

    await expect.poll(() => inputs.posts.length).toBe(1);
    await expect.poll(() => apiState.promptBodies.length).toBeGreaterThanOrEqual(2);
    expect(JSON.stringify(apiState.promptBodies.at(-1))).toContain(
      "Send normally if the previous task already finished",
    );
    await expect(page.getByText(/sent as a normal message/i)).toBeVisible();
  });

  test("Stop retries a lost acknowledgement and clears the UI only after success", async ({ page }) => {
    await mock苏小有Api(page);
    await installSessionInputApi(page);
    await startSlowTask(page);
    let attempts = 0;
    await page.route("**/api/chat/abort", async (route) => {
      attempts += 1;
      if (attempts === 1) {
        await route.abort("connectionreset");
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "aborted" }),
      });
    });

    await page.getByRole("button", { name: "Stop" }).click();
    await expect.poll(() => attempts, { timeout: 10_000 }).toBe(2);
    await expect(page.getByRole("button", { name: "Stop" })).toHaveCount(0);
  });

  test("Stop failure keeps the truthful running state and offers a retry", async ({ page }) => {
    await mock苏小有Api(page);
    await installSessionInputApi(page);
    await startSlowTask(page);
    let attempts = 0;
    await page.route("**/api/chat/abort", async (route) => {
      attempts += 1;
      await route.abort("connectionreset");
    });

    await page.getByRole("button", { name: "Stop" }).click();
    await expect(
      page.getByText(/Could not confirm that the task stopped/i),
    ).toBeVisible({ timeout: 15_000 });
    await expect.poll(() => attempts).toBe(4);
    await expect(page.getByRole("button", { name: "Stop" })).toBeVisible();
    await expect(page.getByPlaceholder("The task is running — add a follow-up")).toBeEditable();
  });

  test("blocked input is cancelled before its text and attachments return to the composer", async ({ page }) => {
    await mock苏小有Api(page);
    const inputs = await installSessionInputApi(page, [
      {
        id: "blocked-input",
        session_id: "session-alpha",
        client_request_id: "blocked-request",
        mode: "queue",
        status: "blocked",
        position: 1,
        text: "Review this interrupted follow-up before retrying",
        attachments: [
          {
            file_id: "managed-blocked-file",
            name: "interrupted-notes.txt",
            path: "/managed/session-alpha/inputs/interrupted-notes.txt",
            size: 64,
            mime_type: "text/plain",
            source: "managed",
          },
        ],
        target_stream_id: null,
        error_message: "Application exited before this input completed",
      },
    ], { loseDeleteResponse: true });

    await page.goto("/c/session-alpha");
    await page.getByRole("button", { name: "More actions for queued message 1" }).click();
    await page.getByRole("menuitem", { name: "Edit" }).click();

    await expect(page.getByPlaceholder(/Describe the result you want/i)).toHaveValue(
      "Review this interrupted follow-up before retrying",
    );
    await expect(page.getByText("interrupted-notes.txt")).toBeVisible();
    await expect(page.getByTestId("pending-inputs")).toHaveCount(0);
    expect(inputs.posts).toHaveLength(0);
  });

  test("an idle queued input can be moved back for explicit editing", async ({ page }) => {
    await mock苏小有Api(page);
    const inputs = await installSessionInputApi(page, [
      {
        id: "orphaned-queued-input",
        session_id: "session-alpha",
        client_request_id: "orphaned-queued-request",
        mode: "queue",
        status: "queued",
        position: 1,
        text: "Edit me before explicitly sending again",
        attachments: [],
        target_stream_id: null,
        error_message: null,
      },
    ]);

    await page.goto("/c/session-alpha");
    await page.getByRole("button", { name: "More actions for queued message 1" }).click();
    await page.getByRole("menuitem", { name: "Edit" }).click();

    await expect(page.getByPlaceholder(/Describe the result you want/i)).toHaveValue(
      "Edit me before explicitly sending again",
    );
    await expect(page.getByTestId("pending-inputs")).toHaveCount(0);
    expect(inputs.posts).toHaveLength(0);
  });

  test("folderless sessions never ingest attachments into the previous global project", async ({ page }) => {
    await page.addInitScript(() => {
      const raw = window.localStorage.getItem("suxiaoyou-settings");
      if (!raw) return;
      const persisted = JSON.parse(raw) as { state?: Record<string, unknown> };
      persisted.state = {
        ...(persisted.state ?? {}),
        workspaceDirectory: "/Users/alex/previous-project",
      };
      window.localStorage.setItem("suxiaoyou-settings", JSON.stringify(persisted));
    });
    const apiState = await mock苏小有Api(page);

    await page.goto("/c/session-default-directory");
    await page.locator('input[type="file"]').setInputFiles({
      name: "folderless-notes.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("folderless data"),
    });

    await expect(page.getByText("folderless-notes.txt")).toBeVisible();
    await page.waitForTimeout(500);
    expect(apiState.ingestRequests).toEqual([]);
  });
});
