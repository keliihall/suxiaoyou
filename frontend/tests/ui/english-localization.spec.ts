import { expect, test, type Page } from "@playwright/test";
import { mock苏小有Api, seed苏小有Storage } from "./fixtures/suxiaoyou-api";

const CJK = /[\u3400-\u9fff\uf900-\ufaff]/u;

async function expectNoVisibleCjk(page: Page, surface: string) {
  const visibleText = await page.locator("body").innerText();
  expect(visibleText, `${surface} contains untranslated CJK text`).not.toMatch(CJK);
}

test.describe("English localization guard", () => {
  test("onboarding, landing, and settings remain fully English", async ({ page }) => {
    await seed苏小有Storage(page, { force: true, hasCompletedOnboarding: false });
    await mock苏小有Api(page);
    await page.route("**/api/config/providers", async (route) => {
      const isChinese = route.request().headers()["accept-language"]?.startsWith("zh");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: "deepseek",
            name: isChinese ? "深度求索（DeepSeek）" : "DeepSeek",
            is_configured: false,
            enabled: true,
            masked_key: null,
            model_count: 0,
            status: "unconfigured",
          },
          {
            id: "qwen",
            name: isChinese ? "通义千问" : "Qwen",
            is_configured: false,
            enabled: true,
            masked_key: null,
            model_count: 0,
            status: "unconfigured",
          },
        ]),
      });
    });

    // Exercise the onboarding surface directly. The root route is only a
    // client-side redirect shim, whose scheduling under a cold CI dev server
    // is unrelated to localization and can leave this assertion on `/`.
    await page.goto("/c/new");
    await expect(page.getByRole("heading", { name: "Welcome to suyo" })).toBeVisible();
    await expectNoVisibleCjk(page, "English onboarding");

    await page.getByRole("button", { name: "Start now" }).click();
    await expect(page.getByRole("heading", { name: /What should suyo help you do\?/i })).toBeVisible();
    await expectNoVisibleCjk(page, "English landing page");

    await page.getByRole("link", { name: "Settings", exact: true }).click();
    await expect(page).toHaveURL(/\/settings$/);
    await expect(page.getByRole("heading", { name: "General" })).toBeVisible();
    await expect(page.getByRole("button", { name: "English" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Chinese" })).toBeVisible();
    await expect(page.getByText(/suyo v0\.8\.3/)).toBeVisible();
    await expectNoVisibleCjk(page, "English general settings");

    await page.getByRole("button", { name: "Chinese", exact: true }).click();
    await expect(page.getByRole("heading", { name: "通用" })).toBeVisible();
    await expect(page).toHaveTitle("苏小有");

    await page.getByRole("button", { name: "服务商", exact: true }).click();
    await expect(page.getByText("深度求索（DeepSeek）", { exact: true })).toBeVisible();
    await expect(page.getByText("通义千问", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "通用", exact: true }).click();

    await page.getByRole("button", { name: "英文", exact: true }).click();
    await expect(page.getByRole("heading", { name: "General" })).toBeVisible();
    await expect(page).toHaveTitle("suyo");
    await expectNoVisibleCjk(page, "general settings after Chinese-to-English switch");

    await page.getByRole("button", { name: "Providers", exact: true }).click();
    await expect(page.getByText("DeepSeek", { exact: true })).toBeVisible();
    await expect(page.getByText("Qwen", { exact: true })).toBeVisible();
    await expectNoVisibleCjk(page, "providers after Chinese-to-English switch");
  });

  test("mobile task and remote settings surfaces remain fully English", async ({ page }) => {
    await page.setViewportSize({ width: 393, height: 852 });
    await seed苏小有Storage(page, { force: true });
    await mock苏小有Api(page);

    await page.goto("/m/settings?token=remote-token");
    await expect(page.getByRole("heading", { name: "Remote connection" })).toBeVisible();
    await expect(page.getByText("Connected", { exact: true })).toBeVisible();
    await expectNoVisibleCjk(page, "English mobile remote settings");

    await page.goto("/m?token=remote-token");
    await expect(page.getByRole("heading", { name: "suyo" })).toBeVisible();
    await expect(page.getByText("Quarterly planning notes")).toBeVisible();
    await expectNoVisibleCjk(page, "English mobile task list");

    await page.getByRole("button", { name: "New task" }).click();
    await expect(page.getByRole("heading", { name: "New task" })).toBeVisible();
    await expect(page.getByPlaceholder("What should suyo do?")).toBeVisible();
    await expectNoVisibleCjk(page, "English mobile new task");
  });
});
