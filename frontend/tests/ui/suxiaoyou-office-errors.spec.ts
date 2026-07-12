import { expect, test, type Page } from "@playwright/test";
import {
  mock苏小有Api,
  seed苏小有Storage,
  type 苏小有MockOptions,
  type 苏小有MockState,
} from "./fixtures/suxiaoyou-api";

async function setupMockedApp(page: Page, options?: 苏小有MockOptions): Promise<苏小有MockState> {
  await seed苏小有Storage(page);
  return mock苏小有Api(page, options);
}

async function expectNoAppCrash(page: Page) {
  await expect(page.getByText("Runtime", { exact: false })).toHaveCount(0);
  await expect(page.getByText("API 401", { exact: false })).toHaveCount(0);
}

async function openArtifactFile(page: Page, fileName: string) {
  const fileButton = page.locator("#main-content").getByRole("button", { name: fileName, exact: true });
  await expect(fileButton).toBeVisible();
  await fileButton.click();
  await expect(page.getByText(fileName).first()).toBeVisible();
}

async function closeArtifactPanel(page: Page) {
  const panelButtons = page.locator("aside").getByRole("button");
  await expect.poll(() => panelButtons.count()).toBeGreaterThan(0);
  await panelButtons.last().click();
}

test.describe("苏小有 Office artifact and error-state GUI workflows", () => {
  test.describe.configure({ timeout: 75_000 });

  test("office artifact workflow: preview DOCX, XLSX, PDF, and safely fall back for PPTX", async ({ page }) => {
    const state = await setupMockedApp(page);

    await page.goto("/c/session-artifacts");
    await expect(page.getByText("Artifact showcase").first()).toBeVisible();

    await openArtifactFile(page, "office-brief.docx");
    await expect(page.getByText("苏小有 DOCX workflow")).toBeVisible({ timeout: 20_000 });
    await closeArtifactPanel(page);

    await openArtifactFile(page, "office-matrix.xlsx");
    await expect(page.getByText("Coverage")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText("Office XLSX")).toBeVisible();
    await expect(page.getByText("Rendered")).toBeVisible();
    await closeArtifactPanel(page);

    await openArtifactFile(page, "office-report.pdf");
    await expect.poll(() => page.locator("canvas").count(), { timeout: 20_000 }).toBeGreaterThan(0);
    await closeArtifactPanel(page);

    await openArtifactFile(page, "office-deck.pptx");
    const pptxFallback = page.getByText(
      "Presentation preview is temporarily unavailable",
    );
    await expect(pptxFallback).toBeVisible({ timeout: 20_000 });
    await expect(
      pptxFallback
        .locator("xpath=..")
        .getByRole("button", { name: "Download", exact: true }),
    ).toBeEnabled();

    expect(state.binaryReads.join("\n")).toContain("office-brief.docx");
    expect(state.binaryReads.join("\n")).toContain("office-matrix.xlsx");
    expect(state.binaryReads.join("\n")).toContain("office-report.pdf");
    expect(state.binaryReads.join("\n")).toContain("office-deck.pptx");
    await expectNoAppCrash(page);
  });

  test("artifact error workflow: missing binary preview shows a recoverable file error", async ({ page }) => {
    await setupMockedApp(page, { binaryFailures: ["missing-report.xlsx"] });

    await page.goto("/c/session-artifacts");
    await openArtifactFile(page, "missing-report.xlsx");
    await expect(page.getByText("File not found:", { exact: false })).toBeVisible();
    await expect(page.getByText("missing-report.xlsx", { exact: false }).first()).toBeVisible();
    await expectNoAppCrash(page);
  });

  test("chat upload error workflow: failed file upload surfaces a toast and keeps composer usable", async ({ page }) => {
    const state = await setupMockedApp(page, { failUploads: ["broken-upload.txt"] });

    await page.goto("/c/new");
    await expect(page.getByRole("heading", { name: /What should (苏小有 help you do|we do in)/i })).toBeVisible();
    await page.locator('input[type="file"]').setInputFiles({
      name: "broken-upload.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("this upload should fail"),
    });

    await expect(page.getByText("Failed to upload file")).toBeVisible();
    await expect(page.getByPlaceholder(/Describe the result you want/i)).toBeVisible();
    expect(state.fileUploads).not.toContain("broken-upload.txt");
    await expectNoAppCrash(page);
  });

  test("provider error workflow: rate-limit and payment-required errors stay in composer flow", async ({ page }) => {
    await setupMockedApp(page, {
      promptErrors: [
        { match: "rate limit gate", status: 429, detail: "Rate limit exceeded" },
        { match: "payment required gate", status: 402, detail: "Payment required" },
      ],
    });

    await page.goto("/c/new");
    await page.getByPlaceholder(/Describe the result you want/i).fill("rate limit gate");
    const rateLimitResponse = page.waitForResponse((res) =>
      res.url().includes("/api/chat/prompt") && res.status() === 429,
    );
    await page.getByRole("button", { name: /Send message/i }).click();
    await rateLimitResponse;
    await expect(page.getByText(/Failed to send message|API 429/i)).toBeVisible();

    await page.getByPlaceholder(/Describe the result you want/i).fill("payment required gate");
    const paymentRequiredResponse = page.waitForResponse((res) =>
      res.url().includes("/api/chat/prompt") && res.status() === 402,
    );
    await page.getByRole("button", { name: /Send message/i }).click();
    await paymentRequiredResponse;
    await expect(page.getByText(/Failed to send message|API 402/i)).toBeVisible();
    await expectNoAppCrash(page);
  });

  test("mobile remote error workflow: invalid token fails without entering the task flow", async ({ page }) => {
    await seed苏小有Storage(page);
    await page.addInitScript(() => {
      window.localStorage.removeItem("suxiaoyou_remote_config");
    });
    await mock苏小有Api(page, { healthStatus: 401 });

    await page.goto("/m/settings");
    await expect(page.getByRole("heading", { name: "远程连接" })).toBeVisible();
    await expect(page.getByText("未连接", { exact: true })).toBeVisible();
    await page.getByPlaceholder("https://xxx.trycloudflare.com").fill("http://127.0.0.1:3317");
    await page.getByPlaceholder("suxiaoyou_rt_...").fill("bad-token");
    await page.getByRole("button", { name: "连接", exact: true }).click();

    await expect(page.getByRole("button", { name: "连接失败" })).toBeVisible();
    await expect(page.getByText("令牌无效")).toBeVisible();
    await expect(page).toHaveURL(/\/m\/settings$/);
    await expectNoAppCrash(page);
  });
});
