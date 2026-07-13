import { expect, test, type Page } from "@playwright/test";
import {
  appendLongConversationTurn,
  mock苏小有Api,
  seed苏小有Storage,
} from "./fixtures/suxiaoyou-api";

async function setup(page: Page) {
  await seed苏小有Storage(page);
  return mock苏小有Api(page);
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
    await page.route("**/api/messages/session-long?*", async (route) => {
      const offset = new URL(route.request().url()).searchParams.get("offset");
      if (offset === "0" && delayFirstPage) {
        delayFirstPage = false;
        await delay(5_200);
      }
      await route.fallback().catch(() => {});
    });
    await page.goto("/c/session-long");

    const outline = page.getByRole("navigation", { name: "Conversation outline" });
    await outline.getByRole("button", { name: /Turn 1 of 60:/ }).click();
    await expect(page.getByRole("button", { name: "Retry locating turn" })).toBeVisible({
      timeout: 5_000,
    });
    await delay(1_500);

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
