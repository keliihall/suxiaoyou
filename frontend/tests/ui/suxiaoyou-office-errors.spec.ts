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
  await expect(page.getByText(/^(?:Unhandled )?Runtime (?:Error|TypeError|ReferenceError)$/)).toHaveCount(0);
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

function pptxText(text: string, size = 28) {
  return {
    paragraphs: [
      {
        runs: [
          {
            text,
            family: "Arial",
            size,
            bold: true,
            italic: false,
            underline: false,
            color: "#1F1F1F",
          },
        ],
        align: "left",
        level: 0,
        bullet: false,
        spaceBefore: 0,
        spaceAfter: 0,
      },
    ],
    marginLeft: 8,
    marginRight: 8,
    marginTop: 4,
    marginBottom: 4,
    vertical: "middle",
  };
}

async function mockPptxStaticPreview(page: Page) {
  let requests = 0;
  await page.route("**/api/files/pptx-preview", async (route) => {
    requests += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        name: "office-deck.pptx",
        path: "/Users/alex/suxiaoyou-demo/slides/office-deck.pptx",
        width: 960,
        height: 540,
        slideCount: 2,
        sceneNodeCount: 18,
        assets: {},
        warnings: ["static_preview_limitations", "ignored_external_links"],
        slides: [
          {
            index: 1,
            background: "#FFFFFF",
            hidden: false,
            elements: [
              {
                kind: "shape",
                x: 80,
                y: 50,
                width: 800,
                height: 90,
                rotation: 0,
                geometry: "roundRect",
                fill: "#DCEBFF",
                stroke: "#4F81BD",
                strokeWidth: 2,
                flipH: false,
                flipV: false,
                arrowStart: false,
                arrowEnd: false,
                text: pptxText("First slide title"),
              },
              {
                kind: "table",
                x: 160,
                y: 210,
                width: 640,
                height: 150,
                rotation: 0,
                cells: [
                  { x: 0, y: 0, width: 320, height: 75, fill: "#FFFFFF", text: pptxText("Name", 16) },
                  { x: 320, y: 0, width: 320, height: 75, fill: "#FFFFFF", text: pptxText("Value", 16) },
                  { x: 0, y: 75, width: 320, height: 75, fill: "#F3F6FA", text: pptxText("苏小有", 16) },
                  { x: 320, y: 75, width: 320, height: 75, fill: "#F3F6FA", text: pptxText("Ready", 16) },
                ],
              },
            ],
          },
          {
            index: 2,
            background: "#F8FAFC",
            hidden: false,
            elements: [
              {
                kind: "shape",
                x: 120,
                y: 130,
                width: 720,
                height: 180,
                rotation: 0,
                geometry: "ellipse",
                fill: "#E9F7EF",
                stroke: "#2E8B57",
                strokeWidth: 2,
                flipH: false,
                flipV: false,
                arrowStart: false,
                arrowEnd: false,
                text: pptxText("Second slide content"),
              },
            ],
          },
        ],
      }),
    });
  });
  return () => requests;
}

async function mockAuthoritativeOfficePreview(page: Page) {
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lL3u4QAAAABJRU5ErkJggg==",
    "base64",
  );
  const renderBodies: Array<Record<string, unknown>> = [];
  let pageRequests = 0;
  await page.route("**/api/office-v2/context**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        session_id: "session-artifacts",
        workspace_instance_id: "workspace-artifacts",
        renderer_available: true,
        renderer_id: "suxiaoyou-attested-office",
        renderer_version: "attestation-fixture",
        font_digest: "f".repeat(64),
        preview_quality: "authoritative",
        formula_values_recalculated: false,
      }),
    });
  });
  await page.route("**/api/office-v2/render", async (route) => {
    renderBodies.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        session_id: "session-artifacts",
        workspace_instance_id: "workspace-artifacts",
        relative_path: "docs/office-brief.docx",
        source_sha256: "a".repeat(64),
        checkpoint_id: "checkpoint-office-1",
        root_turn_id: "turn-office-1",
        preview_quality: "authoritative",
        formula_values_recalculated: false,
        manifest: {
          cache_key: "b".repeat(64),
          renderer_id: "suxiaoyou-attested-office",
          renderer_version: "attestation-fixture",
          font_digest: "f".repeat(64),
          quality: "authoritative",
          pages: [{
            page_number: 1,
            filename: "page-1.png",
            sha256: "c".repeat(64),
            size_bytes: png.length,
            width_px: 1,
            height_px: 1,
            mime_type: "image/png",
          }],
        },
      }),
    });
  });
  await page.route("**/api/office-v2/validation**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        session_id: "session-artifacts",
        workspace_instance_id: "workspace-artifacts",
        relative_path: "docs/office-brief.docx",
        source_sha256: "a".repeat(64),
        status: "authoritative_pass",
        stale_reason: null,
        report: null,
      }),
    });
  });
  await page.route("**/api/office-v2/page**", async (route) => {
    pageRequests += 1;
    await route.fulfill({ status: 200, contentType: "image/png", body: png });
  });
  return { renderBodies, pageRequests: () => pageRequests };
}

async function mockLegacyPptMessage(page: Page) {
  await page.route("**/api/messages/session-artifacts**", async (route) => {
    if (new URL(route.request().url()).pathname.endsWith("/turn-index")) {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        total: 1,
        offset: 0,
        messages: [
          {
            id: "legacy-ppt-assistant",
            session_id: "session-artifacts",
            time_created: "2026-07-13T00:00:00.000Z",
            data: { role: "assistant", agent: "build", finish: "stop" },
            parts: [
              {
                id: "legacy-ppt-tool",
                message_id: "legacy-ppt-assistant",
                session_id: "session-artifacts",
                time_created: "2026-07-13T00:00:00.000Z",
                data: {
                  type: "tool",
                  tool: "present_file",
                  call_id: "legacy-ppt-present",
                  state: {
                    status: "completed",
                    input: {
                      file_path: "/Users/alex/slides/legacy-deck.ppt",
                      title: "legacy-deck.ppt",
                    },
                    output: null,
                    metadata: {
                      file_path: "/Users/alex/slides/legacy-deck.ppt",
                      title: "legacy-deck.ppt",
                    },
                  },
                },
              },
            ],
          },
        ],
      }),
    });
  });
}

test.describe("苏小有 Office artifact and error-state GUI workflows", () => {
  test.describe.configure({ timeout: 75_000 });

  test("office artifact workflow: preview DOCX, XLSX, PDF, and PPTX static slides", async ({ page }) => {
    const state = await setupMockedApp(page);
    const pptxPreviewRequests = await mockPptxStaticPreview(page);

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
    await expect(page.getByText("1 / 2", { exact: true })).toBeVisible({ timeout: 20_000 });
    const slideOne = page.getByLabel("Preview of slide 1");
    await expect(slideOne.getByText("First slide title", { exact: true })).toHaveCount(1);
    await expect(page.locator('aside[aria-label="Slide thumbnails"]')).toBeVisible();
    await expect(page.getByRole("button", { name: "Go to slide 1" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Go to slide 2" })).toBeVisible();
    await expect(
      page.getByText(
        "This is an offline static preview; animations, transitions, fonts, and advanced effects may differ from the original.",
        { exact: false },
      ),
    ).toBeVisible();

    await page.getByRole("button", { name: "Next slide" }).click();
    await expect(page.getByText("2 / 2", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Preview of slide 2").getByText("Second slide content", { exact: true })).toHaveCount(1);

    await expect(page.getByText("100%", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Zoom in" }).click();
    await expect(page.getByText("125%", { exact: true })).toBeVisible();
    expect(pptxPreviewRequests()).toBe(1);

    expect(state.binaryReads.join("\n")).toContain("office-brief.docx");
    expect(state.binaryReads.join("\n")).toContain("office-matrix.xlsx");
    expect(state.binaryReads.join("\n")).toContain("office-report.pdf");
    expect(state.binaryReads.join("\n")).not.toContain("office-deck.pptx");
    await expectNoAppCrash(page);
  });

  test("authoritative Office preview stays bound to a server-owned checkpoint", async ({ page }) => {
    const state = await setupMockedApp(page);
    const office = await mockAuthoritativeOfficePreview(page);

    await page.goto("/c/session-artifacts");
    await openArtifactFile(page, "office-brief.docx");

    await expect(page.getByText("High-fidelity preview", { exact: true })).toBeVisible();
    await expect(page.getByText("Version linked", { exact: false })).toBeVisible();
    await expect(page.getByText("Authoritative validation current", { exact: true })).toBeVisible();
    await expect(page.getByRole("img", { name: "Office preview page 1" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Previous Office page" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Next Office page" })).toBeDisabled();
    expect(office.renderBodies).toEqual([{
      session_id: "session-artifacts",
      workspace_instance_id: "workspace-artifacts",
      relative_path: "docs/office-brief.docx",
    }]);
    expect(office.pageRequests()).toBe(1);
    expect(state.binaryReads.join("\n")).not.toContain("office-brief.docx");
    await expectNoAppCrash(page);
  });

  test("legacy .ppt degrades without calling the OOXML preview endpoint", async ({ page }) => {
    await setupMockedApp(page);
    await mockLegacyPptMessage(page);
    let previewRequests = 0;
    await page.route("**/api/files/pptx-preview", async (route) => {
      previewRequests += 1;
      await route.fulfill({ status: 500, body: "legacy PPT must not reach this endpoint" });
    });

    await page.goto("/c/session-artifacts");
    await page.getByRole("button", { name: "Open preview legacy-deck.ppt" }).click();
    await expect(page.getByText("legacy-deck.ppt").first()).toBeVisible();
    await expect(page.getByText("Legacy PPT preview is not supported")).toBeVisible();
    await expect(
      page.getByText("This is a legacy .ppt file.", { exact: false }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Download original" })).toBeEnabled();
    expect(previewRequests).toBe(0);
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
    await expect(page.getByRole("heading", { name: /What should (suyo help you do|we do in)/i })).toBeVisible();
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
    await expect(page.getByRole("heading", { name: "Remote connection" })).toBeVisible();
    await expect(page.getByText("Not connected", { exact: true })).toBeVisible();
    await page.getByPlaceholder("https://xxx.trycloudflare.com").fill("http://127.0.0.1:3317");
    await page.getByPlaceholder("suxiaoyou_rt_...").fill("bad-token");
    await page.getByRole("button", { name: "Connect", exact: true }).click();

    await expect(page.getByRole("button", { name: "Connection failed" })).toBeVisible();
    await expect(page.getByText("Invalid token")).toBeVisible();
    await expect(page).toHaveURL(/\/m\/settings$/);
    await expectNoAppCrash(page);
  });
});
