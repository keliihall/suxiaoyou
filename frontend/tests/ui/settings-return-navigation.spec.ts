import { expect, test } from "@playwright/test";

import { mock苏小有Api, seed苏小有Storage } from "./fixtures/suxiaoyou-api";

test.beforeEach(async ({ page, isMobile }) => {
  test.skip(!!isMobile, "The desktop settings sidebar is hidden on mobile");
  await seed苏小有Storage(page, { force: true });
  await mock苏小有Api(page);
});

test("the full Back to app row returns to the task that opened Settings", async ({
  page,
}) => {
  await page.goto("/c/_?sessionId=session-alpha");
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page).toHaveURL(/\/settings$/);

  const back = page.getByTestId("settings-back-to-app");
  await expect(back).toHaveAccessibleName("Back to app");
  await expect(back).toHaveAttribute("href", "/c/_?sessionId=session-alpha");
  await expect(back).toHaveCSS("min-height", "44px");

  const box = await back.boundingBox();
  expect(box).not.toBeNull();
  await back.click({
    position: {
      x: Math.max(1, (box?.width ?? 1) - 3),
      y: Math.max(1, (box?.height ?? 1) - 3),
    },
  });
  await expect(page).toHaveURL(/\/c\/_\?sessionId=session-alpha$/);
});

test("Back to app tracks the latest task when only sessionId changes", async ({
  page,
}) => {
  await page.goto("/c/_?sessionId=session-alpha");
  await expect(page.getByText("Summarize the quarterly plan")).toBeVisible();

  // Desktop static-export routes keep `/c/_` and update only the query. The
  // native History API integrates with the Next router, matching that route.
  await page.evaluate(() => {
    window.history.pushState(null, "", "/c/_?sessionId=session-beta");
  });
  await expect(page).toHaveURL(/\/c\/_\?sessionId=session-beta$/);
  await expect(page.getByText("Clean up the invoice folder")).toBeVisible();

  await page.getByRole("link", { name: "Settings", exact: true }).click();
  const back = page.getByTestId("settings-back-to-app");
  await expect(back).toHaveAttribute("href", "/c/_?sessionId=session-beta");
  await back.click();
  await expect(page).toHaveURL(/\/c\/_\?sessionId=session-beta$/);
});

test("a direct Settings load uses the safe fallback and the Chinese action is keyboard accessible", async ({
  page,
}) => {
  await page.goto("/settings");
  await page.addInitScript(() => {
    window.localStorage.setItem("suxiaoyou-language", "zh");
    const settings = JSON.parse(
      window.localStorage.getItem("suxiaoyou-settings") ?? "{}",
    ) as { state?: Record<string, unknown> };
    settings.state = { ...settings.state, language: "zh" };
    window.localStorage.setItem("suxiaoyou-settings", JSON.stringify(settings));
  });
  await page.reload();

  const back = page.getByTestId("settings-back-to-app");
  await expect(back).toHaveAccessibleName("返回应用");
  await expect(back).toHaveAttribute("href", "/c/new");
  await back.focus();
  await expect(back).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/c\/new$/);
});

test("the window drag strip is separate from the interactive return row", async ({
  page,
}) => {
  await page.goto("/settings");

  const dragRegion = page.getByTestId("settings-drag-region");
  const back = page.getByTestId("settings-back-to-app");
  await expect(dragRegion).toHaveAttribute("data-tauri-drag-region");
  await expect(back).not.toHaveAttribute("data-tauri-drag-region");

  const dragBox = await dragRegion.boundingBox();
  const backBox = await back.boundingBox();
  expect(dragBox).not.toBeNull();
  expect(backBox).not.toBeNull();
  expect((dragBox?.y ?? 0) + (dragBox?.height ?? 0)).toBeLessThanOrEqual(
    backBox?.y ?? 0,
  );
});
