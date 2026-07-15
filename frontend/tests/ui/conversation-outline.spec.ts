import { expect, test, type Page } from "@playwright/test";
import {
  appendLongConversationTurn,
  mock苏小有Api,
  seed苏小有Storage,
} from "./fixtures/suxiaoyou-api";

async function setup(page: Page) {
  await seed苏小有Storage(page);
  const state = await mock苏小有Api(page);
  await page.route("**/api/sessions/session-long/todos", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ todos: [] }),
    }),
  );
  return state;
}

const delay = (milliseconds: number) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

async function submitFirstTurnEdit(page: Page, text: string) {
  const firstMessage = page
    .getByTestId("message-list-scroller")
    .locator('[data-message-id="session-long-user-001"]');
  await firstMessage.hover();
  await firstMessage.getByRole("button", { name: "Edit message" }).click();
  await firstMessage.locator("textarea").fill(text);
  const editResponse = page.waitForResponse((response) =>
    response.url().includes("/api/chat/edit") && response.status() === 200,
  );
  await firstMessage.getByRole("button", { name: "Send", exact: true }).click();
  await editResponse;
}

test.describe("苏小有 conversation outline navigation", () => {
  test("keeps idle desktop markers uniform and only emphasizes hover or focus", async ({ page }) => {
    await setup(page);
    await page.goto("/c/session-long");

    const outline = page.getByRole("navigation", { name: "Conversation outline" });
    await expect(outline).toBeVisible();
    const markers = outline.locator("button[data-outline-turn]");
    const firstMarker = markers.nth(0);
    const secondMarker = markers.nth(1);
    const firstBox = await firstMarker.boundingBox();
    const secondBox = await secondMarker.boundingBox();
    expect(firstBox).not.toBeNull();
    expect(secondBox).not.toBeNull();
    expect((secondBox?.y ?? 0) - (firstBox?.y ?? 0)).toBe(8);

    const firstLine = firstMarker.locator('span[aria-hidden="true"]');
    await page.evaluate(() => document.documentElement.classList.remove("dark"));
    await expect(firstMarker).toHaveCSS("color", "rgb(138, 143, 152)");
    const idleMarkerStyles = await markers.evaluateAll((elements) =>
      elements.map((element) => {
        const line = element.querySelector<HTMLElement>('span[aria-hidden="true"]');
        return {
          color: getComputedStyle(element).color,
          height: line ? getComputedStyle(line).height : null,
          width: line ? getComputedStyle(line).width : null,
        };
      }),
    );
    expect(idleMarkerStyles).toHaveLength(await markers.count());
    expect(
      idleMarkerStyles.every(
        ({ color, height, width }) =>
          color === "rgb(138, 143, 152)" && height === "2px" && width === "6px",
      ),
    ).toBe(true);
    await page.evaluate(() => document.documentElement.classList.add("dark"));
    await expect(firstMarker).toHaveCSS("color", "rgb(149, 151, 157)");
    await page.evaluate(() => document.documentElement.classList.remove("dark"));
    await expect(firstLine).toHaveCSS("height", "2px");
    await expect(firstLine).toHaveCSS("width", "6px");
    await firstMarker.hover();
    await expect(firstLine).toHaveCSS("height", "3px");
    await expect(firstLine).toHaveCSS("width", "10px");
    const markerTooltip = page.getByRole("tooltip");
    await expect(markerTooltip).toContainText("Long user turn 001:");
    await expect(markerTooltip.locator("p")).toHaveCount(2);
    expect(await markerTooltip.innerText()).not.toMatch(
      /(?:Turn\s+\d+(?:\s+of\s+\d+)?|第\s*\d+(?:\s*\/\s*\d+)?\s*轮)/u,
    );
    await expect(page.getByText(/^(?:60 turns|60\s*轮对话)$/i)).toHaveCount(0);
    await page.mouse.move(800, 400);
    await expect(firstMarker).toHaveCSS("color", "rgb(138, 143, 152)");
    await expect(firstLine).toHaveCSS("height", "2px");
    await expect(firstLine).toHaveCSS("width", "6px");
    await firstMarker.focus();
    await expect(firstMarker).toHaveCSS("outline-style", "solid");
    await expect(firstMarker).toHaveCSS("outline-width", "1px");
    await expect(firstMarker).toHaveCSS("outline-offset", "1px");
    await expect(firstMarker).toHaveCSS("outline-color", "rgb(51, 156, 255)");
    const focusedLine = firstMarker.locator('span[aria-hidden="true"]');
    await expect(focusedLine).toHaveCSS("height", "3px");
    await expect(focusedLine).toHaveCSS("width", "10px");
    const activeMarker = outline.locator('button[aria-current="location"]');
    const activeLine = activeMarker.locator('span[aria-hidden="true"]');
    await expect(activeMarker).toHaveCSS("color", "rgb(138, 143, 152)");
    await expect(activeLine).toHaveCSS("height", "2px");
    await expect(activeLine).toHaveCSS("width", "6px");
    await activeMarker.hover();
    await expect(activeMarker).toHaveCSS("color", "rgb(26, 28, 31)");
    await expect(activeLine).toHaveCSS("height", "3px");
    await expect(activeLine).toHaveCSS("width", "10px");

    // The compact rail remains at an 8px pitch and contributes only one
    // roving marker to the Tab order, without a separate turn-count control.
    const rovingMarker = outline.locator(
      'button[data-outline-turn][tabindex="0"]',
    );
    await expect(rovingMarker).toHaveCount(1);
    await expect(
      outline.locator('button[data-outline-turn][tabindex="-1"]'),
    ).toHaveCount((await markers.count()) - 1);
    await expect(outline.locator("button")).toHaveCount(await markers.count());
    await expect(outline.getByRole("button")).toHaveCount(await markers.count());
    await expect(
      outline.getByRole("button", { name: "Open full conversation navigation" }),
    ).toHaveCount(0);

    // Center the rail as a flex item when it fits. In a short window, cap the
    // scroll container itself so both ends remain reachable from a stable
    // start position instead of falling into negative centered overflow.
    await page.setViewportSize({ width: 1280, height: 520 });
    await expect(outline).toBeVisible();
    const markerScroller = outline.locator("[data-conversation-outline-list]");
    expect(
      await markerScroller.evaluate(
        (element) => element.scrollHeight > element.clientHeight,
      ),
    ).toBe(true);
    const shortStartGeometry = await markerScroller.evaluate((element) => {
      element.scrollTop = 0;
      const scrollerBox = element.getBoundingClientRect();
      const firstBox = element
        .querySelector("button[data-outline-turn]")
        ?.getBoundingClientRect();
      return {
        firstTop: firstBox?.top ?? Number.NEGATIVE_INFINITY,
        scrollerTop: scrollerBox.top,
      };
    });
    expect(shortStartGeometry.firstTop + 0.5).toBeGreaterThanOrEqual(
      shortStartGeometry.scrollerTop,
    );
    const shortEndGeometry = await markerScroller.evaluate((element) => {
      element.scrollTop = element.scrollHeight;
      const scrollerBox = element.getBoundingClientRect();
      const markerButtons = element.querySelectorAll("button[data-outline-turn]");
      const lastBox = markerButtons.item(markerButtons.length - 1).getBoundingClientRect();
      return {
        lastBottom: lastBox.bottom,
        scrollerBottom: scrollerBox.bottom,
      };
    });
    expect(shortEndGeometry.lastBottom).toBeLessThanOrEqual(
      shortEndGeometry.scrollerBottom + 0.5,
    );

    await page.getByRole("button", { name: "Show workspace" }).click();
    await expect(page.getByRole("button", { name: "Hide workspace" })).toBeVisible();
    await expect(outline).toBeHidden();

    await page.getByRole("button", { name: "Hide workspace" }).click();
    await expect(outline).toBeVisible();
  });

  test("loads an unloaded early turn and returns to the live latest page", async ({ page }) => {
    await setup(page);
    await page.goto("/c/session-long");

    await expect(page.getByText("Long assistant turn 060")).toBeVisible();
    await expect(page.getByText("Long user turn 001")).toHaveCount(0);

    const outline = page.getByRole("navigation", { name: "Conversation outline" });
    const scroller = page.getByTestId("message-list-scroller");
    await expect(outline).toBeVisible();
    const firstTurn = outline.getByRole("button", { name: /Turn 1 of 60:/ });
    await firstTurn.click();

    await expect(scroller.getByText(/Long user turn 001:/)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("Long assistant turn 060")).toHaveCount(0);
    await expect(firstTurn).toHaveAttribute("aria-current", "location");

    const secondTurn = outline.getByRole("button", { name: /Turn 2 of 60:/ });
    await firstTurn.focus();
    await firstTurn.press("ArrowDown");
    await expect(secondTurn).toBeFocused();
    await expect(scroller.getByText(/Long user turn 002:/)).toBeVisible();

    await page.getByRole("button", { name: "Return to latest messages" }).click();
    await expect(page.getByText("Long assistant turn 060")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("Long user turn 001")).toHaveCount(0);
  });

  test("offers retry without replacing the current view when a target page fails", async ({ page }) => {
    await setup(page);
    let failFirstPageOnce = true;
    await page.route("**/api/messages/session-long?*", async (route) => {
      const url = new URL(route.request().url());
      if (url.searchParams.get("offset") === "0" && failFirstPageOnce) {
        failFirstPageOnce = false;
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({ detail: "temporary history failure" }),
        });
        return;
      }
      await route.fallback();
    });
    await page.goto("/c/session-long");

    const outline = page.getByRole("navigation", { name: "Conversation outline" });
    const scroller = page.getByTestId("message-list-scroller");
    await outline.getByRole("button", { name: /Turn 1 of 60:/ }).click();

    await expect(page.getByRole("button", { name: "Retry locating turn" })).toBeVisible();
    await expect(page.getByText("Long assistant turn 060")).toBeVisible();
    await page.getByRole("button", { name: "Retry locating turn" }).click();
    await expect(scroller.getByText(/Long user turn 001:/)).toBeVisible({ timeout: 15_000 });
  });

  test("uses a folded outline on narrow screens", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 720 });
    await setup(page);
    await page.goto("/c/session-long");

    await expect(
      page.getByRole("navigation", { name: "Conversation outline" }),
    ).toBeHidden();
    const outlineButton = page.getByRole("button", { name: "Conversation outline" });
    await expect(outlineButton).toBeVisible();
    await outlineButton.click();
    await expect(page.getByText("60 turns", { exact: true })).toHaveCount(0);
    const firstTurn = page.getByRole("button", { name: /Turn 1 of 60:/ });
    await expect(firstTurn).toBeVisible();
    await firstTurn.click();
    await expect(
      page.getByTestId("message-list-scroller").getByText(/Long user turn 001:/),
    ).toBeVisible({ timeout: 15_000 });
  });

  test("keeps new messages unread in history and returns to their true latest page", async ({ page }) => {
    const state = await setup(page);
    await page.goto("/c/session-long");
    const outline = page.getByRole("navigation", { name: "Conversation outline" });
    await outline.getByRole("button", { name: /Turn 1 of 60:/ }).click();
    await expect(
      page.getByTestId("message-list-scroller").getByText(/Long user turn 001:/),
    ).toBeVisible({ timeout: 15_000 });

    const frozenHistoryPage = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === "/api/messages/session-long"
        && url.searchParams.get("offset") === "100"
        && response.status() === 200;
    });

    appendLongConversationTurn(
      state,
      "A remote teammate added a final launch check.",
      "The new final check is persisted on the live latest page.",
    );

    // Deliberately traverse to the end of the frozen history window. The
    // final request must stop at the pre-navigation total instead of leaking
    // messages that arrived while the user was reading history.
    await delay(800);
    await page.getByTestId("message-list-scroller").evaluate((element) => {
      element.scrollTop = element.scrollHeight;
    });
    const frozenResponse = await frozenHistoryPage;
    expect(new URL(frozenResponse.url()).searchParams.get("limit")).toBe("20");

    const returnButton = page.getByRole("button", {
      name: "Return to latest messages",
    });
    await expect(returnButton.getByText("2", { exact: true })).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByText(/Long assistant turn 061:/)).toHaveCount(0);

    await returnButton.click();
    await expect(
      page.getByTestId("message-list-scroller").getByText(/Long assistant turn 061:/),
    ).toBeVisible({ timeout: 15_000 });
  });

  test("a timed-out target cannot replace the live view when its response arrives late", async ({ page }) => {
    await setup(page);
    let delayFirstPage = true;
    let releaseFirstPage!: () => void;
    let markFirstPageCaptured!: () => void;
    let markFirstPageSettled!: () => void;
    const firstPageGate = new Promise<void>((resolve) => {
      releaseFirstPage = resolve;
    });
    const firstPageCaptured = new Promise<void>((resolve) => {
      markFirstPageCaptured = resolve;
    });
    const firstPageSettled = new Promise<void>((resolve) => {
      markFirstPageSettled = resolve;
    });
    await page.route("**/api/messages/session-long?*", async (route) => {
      const offset = new URL(route.request().url()).searchParams.get("offset");
      if (offset === "0" && delayFirstPage) {
        delayFirstPage = false;
        markFirstPageCaptured();
        await firstPageGate;
        await route.fallback().catch(() => {});
        markFirstPageSettled();
        return;
      }
      await route.fallback().catch(() => {});
    });
    await page.goto("/c/session-long");
    await page.clock.install();

    const outline = page.getByRole("navigation", { name: "Conversation outline" });
    await outline.getByRole("button", { name: /Turn 1 of 60:/ }).press("Enter");
    await firstPageCaptured;
    await page.clock.fastForward(4_100);
    try {
      await expect(
        page.getByRole("button", { name: "Retry locating turn" }),
      ).toBeVisible();
    } finally {
      releaseFirstPage();
    }
    await firstPageSettled;

    await expect(page.getByText("Long assistant turn 060")).toBeVisible();
    await expect(
      page.getByTestId("message-list-scroller").getByText(/Long user turn 001:/),
    ).toHaveCount(0);
  });

  test("selecting an already-loaded latest turn cancels a delayed early target", async ({ page }) => {
    await setup(page);
    let delayFirstPage = true;
    await page.route("**/api/messages/session-long?*", async (route) => {
      const offset = new URL(route.request().url()).searchParams.get("offset");
      if (offset === "0" && delayFirstPage) {
        delayFirstPage = false;
        await delay(2_000);
      }
      await route.fallback().catch(() => {});
    });
    await page.goto("/c/session-long");

    const outline = page.getByRole("navigation", { name: "Conversation outline" });
    await outline.getByRole("button", { name: /Turn 1 of 60:/ }).click();
    const latestTurn = outline.getByRole("button", { name: /Turn 60 of 60:/ });
    await latestTurn.click();
    await delay(2_500);

    await expect(latestTurn).toHaveAttribute("aria-current", "location");
    await expect(page.getByText("Long assistant turn 060")).toBeVisible();
    await expect(
      page.getByTestId("message-list-scroller").getByText(/Long user turn 001:/),
    ).toHaveCount(0);
  });

  test("rapid unloaded targets only allow the final navigation generation to land", async ({ page }) => {
    await setup(page);
    const delayedFirstOffsets = new Set<string>();
    await page.route("**/api/messages/session-long?*", async (route) => {
      const offset = new URL(route.request().url()).searchParams.get("offset") ?? "";
      if ((offset === "0" || offset === "50") && !delayedFirstOffsets.has(offset)) {
        delayedFirstOffsets.add(offset);
        await delay(2_000);
      }
      await route.fallback().catch(() => {});
    });
    await page.goto("/c/session-long");

    const outline = page.getByRole("navigation", { name: "Conversation outline" });
    await outline.getByRole("button", { name: /Turn 1 of 60:/ }).click();
    const finalTarget = outline.getByRole("button", { name: /Turn 31 of 60:/ });
    await finalTarget.click();

    await expect(
      page.getByTestId("message-list-scroller").getByText(/Long user turn 031:/),
    ).toBeVisible({ timeout: 10_000 });
    await delay(2_500);
    await expect(finalTarget).toHaveAttribute("aria-current", "location");
    await expect(
      page.getByTestId("message-list-scroller").getByText(/Long user turn 031:/),
    ).toBeVisible();
  });

  test("an early edit stays in history until authoritative latest atomically replaces live cache", async ({ page }) => {
    const state = await setup(page);
    await page.route("**/api/messages/session-long?*", async (route) => {
      const offset = new URL(route.request().url()).searchParams.get("offset");
      if (offset === "-1" && state.editBodies.length > 0) await delay(1_200);
      await route.fallback();
    });
    await page.goto("/c/session-long");
    const outline = page.getByRole("navigation", { name: "Conversation outline" });
    await outline.getByRole("button", { name: /Turn 1 of 60:/ }).click();
    const scroller = page.getByTestId("message-list-scroller");
    await expect(scroller.getByText(/Long user turn 001:/)).toBeVisible();

    const editedText = "Edited first turn with an authoritative latest-page handoff.";
    await submitFirstTurnEdit(page, editedText);
    await delay(300);
    await expect(page.getByRole("button", { name: "Return to latest messages" })).toBeVisible();
    await expect(scroller.getByText(/Long user turn 002:/)).toHaveCount(0);
    await expect(page.getByText("Long assistant turn 060")).toHaveCount(0);

    await expect(scroller.getByText(editedText)).toBeVisible({ timeout: 10_000 });
    await expect(scroller.getByText(/Long user turn 002:/)).toHaveCount(0);
    await expect(page.getByText("Long assistant turn 060")).toHaveCount(0);
  });

  test("a committed early edit with latest refresh failure prunes history and never exposes stale live messages", async ({ page }) => {
    const state = await setup(page);
    await page.route("**/api/messages/session-long?*", async (route) => {
      const offset = new URL(route.request().url()).searchParams.get("offset");
      if (offset === "-1" && state.editBodies.length > 0) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({ detail: "latest page unavailable" }),
        });
        return;
      }
      await route.fallback();
    });
    await page.goto("/c/session-long");
    const outline = page.getByRole("navigation", { name: "Conversation outline" });
    await outline.getByRole("button", { name: /Turn 1 of 60:/ }).click();
    const scroller = page.getByTestId("message-list-scroller");

    await submitFirstTurnEdit(page, "Committed edit whose latest refresh fails.");
    await expect(
      page.getByText(/The edit was saved, but the latest messages could not be refreshed/),
    ).toBeVisible({ timeout: 10_000 });
    await expect(scroller.locator("textarea")).toHaveCount(0);
    await expect(scroller.getByText(/Long user turn 002:/)).toHaveCount(0);
    await expect(page.getByText("Long assistant turn 060")).toHaveCount(0);

    await page.getByRole("button", { name: "Return to latest messages" }).click();
    await expect(scroller.getByText(/Long user turn 002:/)).toHaveCount(0);
    await expect(page.getByText("Long assistant turn 060")).toHaveCount(0);
  });
});
