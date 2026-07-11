import { expect, test, type Page } from "@playwright/test";

import {
  mock苏小有Api,
  seed苏小有Storage,
  type 苏小有MockState,
} from "./fixtures/suxiaoyou-api";

const outputPath = "/Users/alex/suxiaoyou-demo/slides/office-deck.pptx";

async function mockOutputFileMessage(page: Page) {
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
                    input: { file_path: outputPath, title: "Office deck" },
                    output: null,
                    metadata: { file_path: outputPath, title: "Office deck" },
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

async function installLocalTauriMock(page: Page, platform: "macos" | "windows" | "linux") {
  await page.addInitScript((mockedPlatform) => {
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
    };

    const w = window as unknown as TauriTestWindow;
    let nextCallbackId = 1;
    let nextListenerId = 1;
    const callbacks = new Map<number, ListenerCallback>();
    const listenerEntries = new Map<number, { event: string; handler: number }>();
    w.__FILE_ACTION_SAVE_CALLS__ = [];

    w.__TAURI_INTERNALS__ = {
      metadata: {
        currentWindow: { label: "main" },
        currentWebview: { label: "main" },
      },
      invoke: async (cmd, args = {}) => {
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
        if (cmd === "download_and_save") {
          w.__FILE_ACTION_SAVE_CALLS__.push(args);
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
  }, platform);
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
  await expect(page.getByRole("menuitem", { name: "Show in File Explorer" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Copy file path" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Save as…" })).toBeVisible();

  await page.getByRole("menuitem", { name: "Open with default app" }).click();
  await expect.poll(() => mockState.systemOpenRequests).toEqual([
    { path: outputPath, workspace: "/Users/alex/suxiaoyou-demo" },
  ]);

  await actions.click();
  await page.getByRole("menuitem", { name: "Show in File Explorer" }).click();
  await expect.poll(() => mockState.systemRevealRequests).toEqual([
    { path: outputPath, workspace: "/Users/alex/suxiaoyou-demo" },
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
        return w.__FILE_ACTION_SAVE_CALLS__.map((call) => call.defaultName);
      }),
    )
    .toEqual(["office-deck.pptx"]);
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
