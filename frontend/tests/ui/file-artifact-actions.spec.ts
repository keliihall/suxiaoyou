import { expect, test, type Page } from "@playwright/test";

import {
  mock苏小有Api,
  seed苏小有Storage,
  type 苏小有MockState,
} from "./fixtures/suxiaoyou-api";

const outputPath = "/Users/alex/suxiaoyou-demo/slides/office-deck.pptx";

async function mockOutputFileMessage(
  page: Page,
  artifactPath = outputPath,
  artifactTitle = "Office deck",
) {
  await page.route("**/api/messages/session-artifacts**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        total: 1,
        offset: 0,
        messages: [
          {
            id: "file-actions-assistant",
            session_id: "session-artifacts",
            time_created: "2026-07-11T12:00:00.000Z",
            data: {
              role: "assistant",
              agent: "build",
              model_id: "openrouter/anthropic/claude-sonnet-4.5",
              provider_id: "openrouter",
              finish: "stop",
              cost: 0,
            },
            parts: [
              {
                id: "file-actions-text",
                message_id: "file-actions-assistant",
                session_id: "session-artifacts",
                time_created: "2026-07-11T12:00:00.000Z",
                data: { type: "text", text: "The presentation is ready." },
              },
              {
                id: "file-actions-present",
                message_id: "file-actions-assistant",
                session_id: "session-artifacts",
                time_created: "2026-07-11T12:00:01.000Z",
                data: {
                  type: "tool",
                  tool: "present_file",
                  call_id: "present-office-deck",
                  state: {
                    status: "completed",
                    input: { file_path: artifactPath, title: artifactTitle },
                    output: null,
                    metadata: { file_path: artifactPath, title: artifactTitle },
                    time_start: "2026-07-11T12:00:00.000Z",
                    time_end: "2026-07-11T12:00:01.000Z",
                  },
                },
              },
              {
                id: "file-actions-finish",
                message_id: "file-actions-assistant",
                session_id: "session-artifacts",
                time_created: "2026-07-11T12:00:02.000Z",
                data: {
                  type: "step-finish",
                  reason: "stop",
                  tokens: {
                    input: 10,
                    output: 10,
                    reasoning: 0,
                    cache_read: 0,
                    cache_write: 0,
                  },
                  cost: 0,
                },
              },
            ],
          },
        ],
      }),
    });
  });
}

async function installLocalTauriMock(
  page: Page,
  platform: "macos" | "windows" | "linux",
  nativeFailure?: { command: string; message: string },
) {
  await page.addInitScript(({ mockedPlatform, mockedNativeFailure }) => {
    type ListenerEvent = { id: number; event: string; payload: unknown };
    type ListenerCallback = (event: ListenerEvent) => void;
    type TauriTestWindow = Window & {
      __TAURI_INTERNALS__: {
        metadata: {
          currentWindow: { label: string };
          currentWebview: { label: string };
        };
        invoke: (
          cmd: string,
          args?: Record<string, unknown>,
        ) => Promise<unknown>;
        transformCallback: (callback: ListenerCallback) => number;
        unregisterCallback: (id: number) => void;
        convertFileSrc: (filePath: string) => string;
      };
      __TAURI_EVENT_PLUGIN_INTERNALS__: {
        unregisterListener: (event: string, eventId: number) => void;
      };
      __FILE_ACTION_SAVE_CALLS__: Array<Record<string, unknown>>;
      __FILE_ACTION_OPEN_WITH_CALLS__: Array<Record<string, unknown>>;
    };

    const w = window as unknown as TauriTestWindow;
    let nextCallbackId = 1;
    let nextListenerId = 1;
    const callbacks = new Map<number, ListenerCallback>();
    const listenerEntries = new Map<number, { event: string; handler: number }>();
    w.__FILE_ACTION_SAVE_CALLS__ = [];
    w.__FILE_ACTION_OPEN_WITH_CALLS__ = [];

    w.__TAURI_INTERNALS__ = {
      metadata: {
        currentWindow: { label: "main" },
        currentWebview: { label: "main" },
      },
      invoke: async (cmd, args = {}) => {
        if (mockedNativeFailure?.command === cmd) {
          throw new Error(mockedNativeFailure.message);
        }
        if (cmd === "get_backend_url") return "http://localhost:8000";
        if (cmd === "get_backend_token") return "test-session-token";
        if (cmd === "get_backend_status") {
          return {
            phase: "ready",
            revision: 1,
            url: "http://localhost:8000",
          };
        }
        if (cmd === "get_pending_navigation") return null;
        if (cmd === "get_platform") return mockedPlatform;
        if (cmd === "is_maximized") return false;
        if (cmd === "download_and_save" || cmd === "save_authorized_file_as") {
          w.__FILE_ACTION_SAVE_CALLS__.push(args);
          return true;
        }
        if (cmd === "open_authorized_file_with") {
          w.__FILE_ACTION_OPEN_WITH_CALLS__.push(args);
          return true;
        }
        if (cmd === "plugin:event|listen") {
          const listenerId = nextListenerId++;
          listenerEntries.set(listenerId, {
            event: String(args.event),
            handler: Number(args.handler),
          });
          return listenerId;
        }
        if (cmd === "plugin:event|unlisten") {
          listenerEntries.delete(Number(args.eventId));
          return null;
        }
        return null;
      },
      transformCallback: (callback) => {
        const id = nextCallbackId++;
        callbacks.set(id, callback);
        return id;
      },
      unregisterCallback: (id) => {
        callbacks.delete(id);
      },
      convertFileSrc: (filePath) => filePath,
    };
    w.__TAURI_EVENT_PLUGIN_INTERNALS__ = {
      unregisterListener: (_event, eventId) => {
        listenerEntries.delete(eventId);
      },
    };
  }, { mockedPlatform: platform, mockedNativeFailure: nativeFailure });
}

let mockState: 苏小有MockState;

test.beforeEach(async ({ page, isMobile }) => {
  test.skip(!!isMobile, "The compact mobile surface uses the same remote-safe action policy");
  await seed苏小有Storage(page, { force: true });
  mockState = await mock苏小有Api(page);
  await mockOutputFileMessage(page);
});

test("output file dropdown and right-click menu share remote-safe actions", async ({
  page,
}) => {
  await page.goto("/c/session-artifacts");

  const card = page.getByTestId("file-artifact-card").filter({ hasText: "Office deck" });
  await expect(card).toBeVisible();
  await expect(card.locator("button button")).toHaveCount(0);
  await expect(card.getByRole("button", { name: "Open preview Office deck" })).toBeVisible();

  const fileActions = card.getByRole("button", { name: "File actions" });
  await fileActions.focus();
  await fileActions.press("Enter");
  await expect(page.getByRole("menuitem", { name: "Open preview" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Download" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Open with default app" })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "Choose another app…" })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "Copy file path" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  await card.click({ button: "right" });
  await expect(page.getByRole("menuitem", { name: "Open preview" })).toBeVisible();
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("menuitem", { name: "Download" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("office-deck.pptx");
  expect(mockState.binaryReads).toContain(outputPath);
});

test("local desktop menu opens, reveals, copies, and saves the output file", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await installLocalTauriMock(page, "windows");
  await page.goto("/c/session-artifacts");

  const card = page.getByTestId("file-artifact-card").filter({ hasText: "Office deck" });
  const actions = card.getByRole("button", { name: "Open with" });
  await expect(actions).toBeVisible();

  await actions.click();
  await expect(page.getByRole("menuitem", { name: "Open with default app" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Choose another app…" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Show in File Explorer" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Copy file path" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Save as…" })).toBeVisible();

  await page.getByRole("menuitem", { name: "Open with default app" }).click();
  await expect.poll(() => mockState.systemOpenRequests).toEqual([
    { path: outputPath, session_id: "session-artifacts" },
  ]);

  await actions.click();
  await page.getByRole("menuitem", { name: "Choose another app…" }).click();
  await expect
    .poll(() =>
      page.evaluate(() => {
        const w = window as unknown as {
          __FILE_ACTION_OPEN_WITH_CALLS__: Array<Record<string, unknown>>;
        };
        return w.__FILE_ACTION_OPEN_WITH_CALLS__;
      }),
    )
    .toEqual([
      {
        path: outputPath,
        sessionId: "session-artifacts",
        dialogTitle: "Choose an application to open this file",
      },
    ]);

  await actions.click();
  await page.getByRole("menuitem", { name: "Show in File Explorer" }).click();
  await expect.poll(() => mockState.systemRevealRequests).toEqual([
    { path: outputPath, session_id: "session-artifacts" },
  ]);

  await actions.click();
  await page.getByRole("menuitem", { name: "Copy file path" }).click();
  await expect(page.getByText("File path copied")).toBeVisible();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(outputPath);

  await actions.click();
  await page.getByRole("menuitem", { name: "Save as…" }).click();
  await expect(page.getByText("File saved")).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(() => {
        const w = window as unknown as {
          __FILE_ACTION_SAVE_CALLS__: Array<Record<string, unknown>>;
        };
        return w.__FILE_ACTION_SAVE_CALLS__;
      }),
    )
    .toEqual([
      {
        path: outputPath,
        sessionId: "session-artifacts",
        defaultName: "office-deck.pptx",
        dialogTitle: "Save as…",
      },
    ]);
  expect(mockState.binaryReads).not.toContain(outputPath);
});

test("local file history lists durable versions and restores without discarding current contents", async ({
  page,
}) => {
  await installLocalTauriMock(page, "macos");
  let restoreBody: unknown = null;

  await page.route("**/api/file-versions?**", async (route) => {
    const url = new URL(route.request().url());
    expect(url.searchParams.get("session_id")).toBe("session-artifacts");
    expect(url.searchParams.get("file_path")).toBe(outputPath);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        workspace: "/Users/alex/suxiaoyou-demo",
        versions: [
          {
            id: "version-before-edit",
            relative_path: "slides/office-deck.pptx",
            sha256: "a".repeat(64),
            size: 1536,
            created_at: "2026-07-11T11:59:00.000Z",
            created_at_ns: 1,
            operation: "office.edit",
            session_id: "session-artifacts",
            message_id: "message-one",
            call_id: "call-one",
            original_mode: 420,
          },
        ],
      }),
    });
  });
  await page.route("**/api/file-versions/version-before-edit/restore", async (route) => {
    restoreBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        file_path: outputPath,
        restored_version: { id: "version-before-edit" },
        recovery_version: { id: "version-current" },
      }),
    });
  });

  await page.goto("/c/session-artifacts");
  const card = page.getByTestId("file-artifact-card").filter({ hasText: "Office deck" });
  await card.getByRole("button", { name: "Open with" }).click();
  await page.getByRole("menuitem", { name: "Version history" }).click();

  await expect(page.getByRole("heading", { name: "Version history for office-deck.pptx" })).toBeVisible();
  await expect(page.getByTestId("file-version-list")).toContainText("office.edit");
  await expect(page.getByTestId("file-version-list")).toContainText("1.5 KiB");
  await expect(page.getByTestId("file-version-list")).toContainText("aaaaaaaaaaaa…");

  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain("current contents will be saved as a recovery version");
    await dialog.accept();
  });
  await page.getByRole("button", { name: "Restore" }).click();

  await expect(page.getByText("File version restored; the previous contents were saved as a recovery version")).toBeVisible();
  expect(restoreBody).toEqual({ session_id: "session-artifacts" });
});

test("local native save failures have a specific recoverable message", async ({ page }) => {
  await installLocalTauriMock(page, "macos", {
    command: "save_authorized_file_as",
    message: "disk_full:no space left on device",
  });
  await page.goto("/c/session-artifacts");

  const card = page.getByTestId("file-artifact-card").filter({ hasText: "Office deck" });
  await card.getByRole("button", { name: "Open with" }).click();
  await page.getByRole("menuitem", { name: "Save as…" }).click();

  await expect(
    page.getByText("There is not enough disk space to save this file"),
  ).toBeVisible();
  expect(mockState.binaryReads).not.toContain(outputPath);
});

for (const {
  platform,
  artifactPath,
  revealLabel,
} of [
  {
    platform: "macos" as const,
    artifactPath: "/Users/alex/Documents/苏小有/office deck.pptx",
    revealLabel: "Reveal in Finder",
  },
  {
    platform: "windows" as const,
    artifactPath: "C:\\Users\\Alex\\Documents\\苏小有\\office deck.pptx",
    revealLabel: "Show in File Explorer",
  },
  {
    platform: "linux" as const,
    artifactPath: "/home/alex/Documents/苏小有/office deck.pptx",
    revealLabel: "Show in file manager",
  },
]) {
  test(`local ${platform} right-click and dropdown expose the same host file actions`, async ({
    page,
  }) => {
    await installLocalTauriMock(page, platform);
    await mockOutputFileMessage(page, artifactPath);
    await page.goto("/c/session-artifacts");

    const card = page.getByTestId("file-artifact-card").filter({ hasText: "Office deck" });
    await card.click({ button: "right" });
    await expect(page.locator("[data-localized-context-menu]")).toHaveCount(0);
    for (const label of [
      "Open preview",
      "Open with default app",
      "Choose another app…",
      revealLabel,
      "Copy file path",
      "Save as…",
    ]) {
      await expect(page.getByRole("menuitem", { name: label })).toBeVisible();
    }
    await page.keyboard.press("Escape");

    await card.getByRole("button", { name: "Open with" }).click();
    for (const label of [
      "Open preview",
      "Open with default app",
      "Choose another app…",
      revealLabel,
      "Copy file path",
      "Save as…",
    ]) {
      await expect(page.getByRole("menuitem", { name: label })).toBeVisible();
    }
  });
}

test("generated CSV preview loads its content from the output path", async ({ page }) => {
  const csvPath = "/Users/alex/suxiaoyou-demo/data/generated-summary.csv";
  let contentRequest: Record<string, unknown> | null = null;
  await mockOutputFileMessage(page, csvPath, "Generated summary");
  await page.route("**/api/files/content", async (route) => {
    const request = route.request().postDataJSON() as Record<string, unknown>;
    if (request.path !== csvPath) {
      await route.fallback();
      return;
    }
    contentRequest = request;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        content: "Name,Status\nPreview loaded from disk,Ready",
        name: "generated-summary.csv",
        mime_type: "text/csv",
        size: 47,
      }),
    });
  });

  await page.goto("/c/session-artifacts");
  await page.getByRole("button", { name: "Open preview Generated summary" }).click();

  await expect(page.getByText("Preview loaded from disk", { exact: true })).toBeVisible();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();
  expect(contentRequest).toEqual({
    path: csvPath,
    workspace: "/Users/alex/suxiaoyou-demo",
  });
});

test("generated raster image uses a bounded binary preview and revokes its blob URL", async ({
  page,
}) => {
  const imagePath = "/Users/alex/suxiaoyou-demo/images/preview-pixel.png";
  const onePixelPng =
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
  let binaryRequest: Record<string, unknown> | null = null;

  await page.addInitScript(() => {
    const revoked: string[] = [];
    const originalRevoke = URL.revokeObjectURL.bind(URL);
    Object.defineProperty(window, "__REVOKED_BLOB_URLS__", {
      configurable: true,
      value: revoked,
    });
    URL.revokeObjectURL = (url: string) => {
      revoked.push(url);
      originalRevoke(url);
    };
  });
  await mockOutputFileMessage(page, imagePath, "Preview image");
  await page.route("**/api/files/content-binary", async (route) => {
    const request = route.request().postDataJSON() as Record<string, unknown>;
    if (request.path !== imagePath) {
      await route.fallback();
      return;
    }
    binaryRequest = request;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        content_base64: onePixelPng,
        name: "preview-pixel.png",
        mime_type: "image/png",
        size: 68,
      }),
    });
  });

  await page.goto("/c/session-artifacts");
  await page.getByRole("button", { name: "Open preview Preview image" }).click();

  const image = page.getByTestId("raster-image-preview");
  await expect(image).toBeVisible();
  const blobUrl = await image.getAttribute("src");
  expect(blobUrl).toMatch(/^blob:/);
  expect(binaryRequest).toEqual({
    path: imagePath,
    workspace: "/Users/alex/suxiaoyou-demo",
  });

  const previewAside = page.locator("aside").filter({ has: image });
  const header = previewAside.getByText("Preview image", { exact: true }).locator("xpath=../..");
  await header.getByRole("button").last().click();
  await expect(image).toHaveCount(0);
  await expect
    .poll(() =>
      page.evaluate(() =>
        (window as unknown as { __REVOKED_BLOB_URLS__: string[] })
          .__REVOKED_BLOB_URLS__,
      ),
    )
    .toContain(blobUrl);
});

test("a missing output file reports a visible save error", async ({ page }) => {
  await page.route("**/api/files/content-binary", async (route) => {
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: `File not found: ${outputPath}` }),
    });
  });
  await page.goto("/c/session-artifacts");

  const card = page.getByTestId("file-artifact-card").filter({ hasText: "Office deck" });
  await card.getByRole("button", { name: "File actions" }).click();
  await page.getByRole("menuitem", { name: "Download" }).click();

  await expect(page.getByText("Could not save file")).toBeVisible();
});
