import { expect, test, type Page } from "@playwright/test";

import { mock苏小有Api, seed苏小有Storage } from "./fixtures/suxiaoyou-api";

const unscopedPinnedSession = {
  id: "conversation-only",
  project_id: null,
  parent_id: null,
  slug: null,
  directory: null,
  title: "Conversation without a project",
  version: 0,
  summary_additions: 0,
  summary_deletions: 0,
  summary_files: 0,
  summary_diffs: [],
  is_pinned: true,
  permission: {},
  time_created: "2026-07-11T10:00:00.000Z",
  time_updated: "2026-07-11T10:00:00.000Z",
  time_compacting: null,
  time_archived: null,
};

const unscopedSession = {
  ...unscopedPinnedSession,
  id: "conversation-only-unpinned",
  title: "Only conversation on Windows",
  is_pinned: false,
};

const windowsProjectPath = "C:\\Users\\Alex\\Projects\\Quarterly plan";

async function installWindowsTauriMock(page: Page) {
  await page.addInitScript((selectedPath) => {
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
      __PROJECT_DIALOG_CALLS__: Array<Record<string, unknown>>;
    };

    const w = window as unknown as TauriTestWindow;
    const callbacks = new Map<number, ListenerCallback>();
    const listeners = new Map<number, { event: string; handler: number }>();
    let nextCallbackId = 1;
    let nextListenerId = 1;
    w.__PROJECT_DIALOG_CALLS__ = [];

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
        if (cmd === "get_platform") return "windows";
        if (cmd === "is_maximized") return false;
        if (cmd === "plugin:dialog|open") {
          w.__PROJECT_DIALOG_CALLS__.push(args);
          return selectedPath;
        }
        if (cmd === "plugin:event|listen") {
          const listenerId = nextListenerId++;
          listeners.set(listenerId, {
            event: String(args.event),
            handler: Number(args.handler),
          });
          return listenerId;
        }
        if (cmd === "plugin:event|unlisten") {
          listeners.delete(Number(args.eventId));
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
        listeners.delete(eventId);
      },
    };
  }, windowsProjectPath);
}

test.beforeEach(async ({ page, isMobile }) => {
  test.skip(!!isMobile, "Desktop sidebar actions are covered by the desktop project");
  await seed苏小有Storage(page, { force: true });
  await mock苏小有Api(page);
});

async function mockSessionList(page: Page, sessions: unknown[]) {
  await page.route("**/api/sessions**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/sessions" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(sessions),
      });
      return;
    }
    if (url.pathname === "/api/sessions/search" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "[]",
      });
      return;
    }
    await route.fallback();
  });
}

async function seedSidebarState(
  page: Page,
  overrides: Record<string, unknown>,
) {
  await page.addInitScript((nextState) => {
    window.localStorage.setItem(
      "suxiaoyou-sidebar",
      JSON.stringify({
        state: {
          collapsedProjects: {},
          organizeMode: "by-project",
          sortBy: "updated",
          width: 260,
          ...nextState,
        },
        version: 0,
      }),
    );
  }, overrides);
}

async function expectStableCreateActions(page: Page) {
  const actions = page.getByTestId("sidebar-primary-actions");
  await expect(actions).toBeVisible();
  await expect(
    actions.getByRole("button", { name: "Add new project", exact: true }),
  ).toBeVisible();
  await expect(
    actions.getByRole("button", { name: "New chat", exact: true }),
  ).toBeVisible();
}

test("sidebar create actions survive a chronological list with only a pinned conversation", async ({
  page,
}) => {
  await seedSidebarState(page, { organizeMode: "chronological" });
  await mockSessionList(page, [unscopedPinnedSession]);

  await page.goto("/c/new");

  await expectStableCreateActions(page);
  await expect(page.getByText("Conversation without a project")).toBeVisible();
});

test("chronological groups suppress duplicate row timestamps", async ({ page }) => {
  await seedSidebarState(page, { organizeMode: "chronological" });
  await mockSessionList(page, [unscopedSession]);

  await page.goto("/c/new");

  const row = page.locator('[role="option"]').filter({ hasText: unscopedSession.title });
  await expect(row).toBeVisible();
  await expect(row.locator("time")).toHaveCount(0);
  await expect(row.locator("div.min-w-0.flex-1").first()).toHaveClass(/\bpr-2\b/);
  await expect(row.locator("div.min-w-0.flex-1").first()).not.toHaveClass(/\bpr-16\b/);
});

test("task timestamp exposes the selected field as a full accessible tooltip", async ({ page }) => {
  await seedSidebarState(page, { sortBy: "created" });
  await mockSessionList(page, [unscopedSession]);

  await page.goto("/c/new");

  const row = page.locator('[role="option"]').filter({ hasText: unscopedSession.title });
  const timestamp = row.locator("time");
  await expect(timestamp).toBeVisible();
  await expect(row.locator("div.min-w-0.flex-1").first()).toHaveClass(/\bpr-16\b/);
  await expect(timestamp).toHaveAttribute("datetime", "2026-07-11T10:00:00.000Z");
  await expect(timestamp).toHaveAttribute("title", /2026/);
  await expect(timestamp).toHaveAttribute("aria-label", /^Created /);
});

test("sidebar create actions remain available when there are no conversations", async ({
  page,
}) => {
  await mockSessionList(page, []);

  await page.goto("/c/new");

  await expectStableCreateActions(page);
  await expect(page.getByText("No conversations yet")).toBeVisible();
});

test("sidebar create actions remain available when a search has no matches", async ({
  page,
}) => {
  await seedSidebarState(page, { searchQuery: "does-not-exist" });
  await mockSessionList(page, [unscopedPinnedSession]);

  await page.goto("/c/new");

  await expectStableCreateActions(page);
  await expect(page.getByText("No matching conversations")).toBeVisible();
});

test("Windows keeps New Project visible with only an unscoped conversation and opens the native picker", async ({
  page,
}) => {
  await installWindowsTauriMock(page);
  await mockSessionList(page, [unscopedSession]);

  await page.goto("/c/new");

  const addProject = page.getByTestId("window-add-project");
  await expect(addProject).toBeVisible();
  await expect(page.getByText("Only conversation on Windows")).toBeVisible();
  await addProject.click();

  await expect
    .poll(() =>
      page.evaluate(() => {
        const w = window as unknown as Window & {
          __PROJECT_DIALOG_CALLS__?: Array<Record<string, unknown>>;
        };
        return w.__PROJECT_DIALOG_CALLS__ ?? [];
      }),
    )
    .toEqual([
      {
        options: {
          directory: true,
          multiple: false,
          title: "Add new project",
        },
      },
    ]);
  await expect(page).toHaveURL((url) =>
    url.pathname === "/c/new" &&
    url.searchParams.get("directory") === windowsProjectPath,
  );
});

test("Windows restores the fixed New Project action after reopening a collapsed sidebar", async ({
  page,
}) => {
  await installWindowsTauriMock(page);
  await mockSessionList(page, [unscopedSession]);
  await page.goto("/c/new");

  const sidebar = page.locator('aside[aria-label="Chat sidebar"]');
  const addProject = page.getByTestId("window-add-project");
  await expect(addProject).toBeVisible();

  await page.getByRole("button", { name: "Toggle sidebar" }).click();
  await expect(sidebar).toHaveAttribute("inert", "");
  await expect(sidebar).toHaveAttribute("aria-hidden", "true");
  await expect
    .poll(() => sidebar.evaluate((element) => element.getBoundingClientRect().width))
    .toBeLessThan(1);

  await page.getByRole("button", { name: "Open sidebar" }).click();
  await expect(sidebar).not.toHaveAttribute("inert", "");
  await expect(sidebar).toHaveAttribute("aria-hidden", "false");
  await expect
    .poll(() => sidebar.evaluate((element) => element.getBoundingClientRect().width))
    .toBeGreaterThan(200);
  await expect(addProject).toBeVisible();
});

test("a narrow Windows window exposes New Project in the responsive sidebar drawer", async ({
  page,
}) => {
  await page.setViewportSize({ width: 900, height: 800 });
  await installWindowsTauriMock(page);
  await mockSessionList(page, []);
  await page.goto("/c/new");

  await page.getByRole("button", { name: "Toggle sidebar" }).click();
  const drawer = page.getByRole("dialog");
  await expect(drawer).toBeVisible();
  await expect(
    drawer
      .getByTestId("sidebar-primary-actions")
      .getByRole("button", { name: "Add new project", exact: true }),
  ).toBeVisible();
  await expect(drawer.getByText("No conversations yet")).toBeVisible();
});

test("About shows product information without a GitHub download entry", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const calls: unknown[][] = [];
    (window as Window & { __RELEASE_WINDOW_OPEN_CALLS__?: unknown[][] })
      .__RELEASE_WINDOW_OPEN_CALLS__ = calls;
    window.open = ((...args: unknown[]) => {
      calls.push(args);
      return null;
    }) as typeof window.open;
  });
  await page.goto("/settings");

  await expect(page.getByRole("heading", { name: "About" })).toBeVisible();
  await expect(page.getByText(/suyo v0\.9\.0/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "View latest version and downloads" }),
  ).toHaveCount(0);
  await expect(page.getByRole("link", { name: /GitHub|download/i })).toHaveCount(0);
  await expect.poll(() => page.evaluate(() =>
    (window as Window & { __RELEASE_WINDOW_OPEN_CALLS__?: unknown[][] })
      .__RELEASE_WINDOW_OPEN_CALLS__,
  )).toEqual([]);
});
