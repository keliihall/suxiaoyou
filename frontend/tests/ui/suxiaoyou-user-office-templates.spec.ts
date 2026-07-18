import { expect, test, type Page, type Request, type Route } from "@playwright/test";

import { mock苏小有Api, seed苏小有Storage } from "./fixtures/suxiaoyou-api";

const SESSION_ID = "session-alpha";
const WORKSPACE_INSTANCE_ID = "workspace-instance-alpha";
const TEMPLATE_REF = "utpl-01jz8m5r3f4g6h7j8k9npqrs";
const SOURCE_SHA = "a".repeat(64);
const CACHE_KEY = "b".repeat(64);

interface TemplateFixture {
  template_ref: string;
  revision: number;
  state_version: number;
  display_name: string;
  format: "docx" | "xlsx" | "pptx";
  source: {
    sha256: string;
    size_bytes: number;
    manifest_sha256: string;
  };
  placeholder_schema: Array<{
    name: string;
    type: "text";
    required: true;
    min_chars: number;
    max_chars: number;
    description: string;
  }>;
  allowed_operations: string[];
  status: "needs_confirmation" | "needs_review" | "approved";
  can_approve: boolean;
  can_instantiate: boolean;
  render_evidence: {
    quality: "authoritative" | "approximate";
    renderer_id: string;
    renderer_version: string;
    font_digest: string;
    parameters_version: string;
    parameters_sha256: string;
    cache_key: string;
    manifest_sha256: string;
    page_count: number;
  };
  beta: true;
}

function templateFixture(
  overrides: Partial<TemplateFixture> = {},
): TemplateFixture {
  return {
    template_ref: TEMPLATE_REF,
    revision: 1,
    state_version: 1,
    display_name: "Board briefing",
    format: "docx",
    source: {
      sha256: SOURCE_SHA,
      size_bytes: 4096,
      manifest_sha256: "c".repeat(64),
    },
    placeholder_schema: [
      {
        name: "title",
        type: "text",
        required: true,
        min_chars: 1,
        max_chars: 120,
        description: "Document title",
      },
      {
        name: "summary",
        type: "text",
        required: true,
        min_chars: 1,
        max_chars: 800,
        description: "Executive summary",
      },
    ],
    allowed_operations: ["instantiate_text"],
    status: "needs_confirmation",
    can_approve: true,
    can_instantiate: false,
    render_evidence: {
      quality: "authoritative",
      renderer_id: "libreoffice-attested",
      renderer_version: "26.2",
      font_digest: "d".repeat(64),
      parameters_version: "office-render-v1",
      parameters_sha256: "e".repeat(64),
      cache_key: CACHE_KEY,
      manifest_sha256: "f".repeat(64),
      page_count: 2,
    },
    beta: true,
    ...overrides,
  };
}

function fulfillJson(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockRuntimeContext(page: Page) {
  await page.route("**/api/runtime/context**", async (route) => {
    const url = new URL(route.request().url());
    expect(url.searchParams.get("session_id")).toBe(SESSION_ID);
    return fulfillJson(route, {
      session_id: SESSION_ID,
      workspace_instance_id: WORKSPACE_INSTANCE_ID,
      workspace_kind: "direct",
      checkpoint_rewind_released: false,
      managed_worktrees_released: false,
      external_side_effects_reverted: false,
    });
  });
}

function expectListBinding(request: Request) {
  const url = new URL(request.url());
  expect(url.searchParams.get("session_id")).toBe(SESSION_ID);
  expect(url.searchParams.get("workspace_instance_id")).toBe(
    WORKSPACE_INSTANCE_ID,
  );
}

async function openTemplateCard(page: Page) {
  const card = page.getByTestId("user-office-template-card");
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: /Office templates/ }).click();
  return card;
}

test.beforeEach(async ({ page, isMobile }) => {
  test.skip(!!isMobile, "The desktop workspace panel is hidden on mobile");
  await seed苏小有Storage(page, { force: true });
  await mock苏小有Api(page);
  await mockRuntimeContext(page);
});

test("feature-gate 404 hides the user Office template surface completely", async ({
  page,
}) => {
  let listCalls = 0;
  await page.route("**/api/office-v2/user-templates**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/office-v2/user-templates") {
      listCalls += 1;
      expectListBinding(route.request());
      return fulfillJson(
        route,
        {
          code: "v11_user_office_templates_not_available",
          detail: "User Office templates are not available in this release",
        },
        404,
      );
    }
    return fulfillJson(route, { detail: "Unexpected template request" }, 404);
  });

  await page.goto(`/c/${SESSION_ID}`);
  await expect.poll(() => listCalls).toBeGreaterThan(0);
  await expect(page.getByTestId("user-office-template-card")).toHaveCount(0);
});

test("imports an authoritative template, previews it, approves exact evidence, and copies its opaque reference", async ({
  page,
}) => {
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  let currentTemplates: TemplateFixture[] = [];
  const importBodies: string[] = [];
  const approvalBodies: unknown[] = [];
  const previewQueries: Array<Record<string, string | null>> = [];
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL2WQAAAABJRU5ErkJggg==",
    "base64",
  );

  await page.route("**/api/office-v2/user-templates**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    if (url.pathname === "/api/office-v2/user-templates" && method === "GET") {
      expectListBinding(request);
      return fulfillJson(route, { templates: currentTemplates, beta: true });
    }
    if (url.pathname === "/api/office-v2/user-templates/import" && method === "POST") {
      const body = (request.postDataBuffer() ?? Buffer.alloc(0)).toString("utf8");
      importBodies.push(body);
      expect(body).toMatch(/name="session_id"\r?\n\r?\nsession-alpha/);
      expect(body).toMatch(
        /name="workspace_instance_id"\r?\n\r?\nworkspace-instance-alpha/,
      );
      expect(body).toMatch(
        /name="client_request_id"\r?\n\r?\ndesktop-[0-9a-f-]{36}/,
      );
      expect(body).toMatch(/name="display_name"\r?\n\r?\nBoard briefing/);
      expect(body).toContain('filename="board.docx"');
      expect(body).toContain('"name": "title"');
      const imported = templateFixture();
      currentTemplates = [imported];
      return fulfillJson(route, { template: imported, idempotent: false }, 201);
    }
    if (
      url.pathname === `/api/office-v2/user-templates/${TEMPLATE_REF}/page` &&
      method === "GET"
    ) {
      previewQueries.push({
        session_id: url.searchParams.get("session_id"),
        workspace_instance_id: url.searchParams.get("workspace_instance_id"),
        revision: url.searchParams.get("revision"),
        expected_state_version: url.searchParams.get("expected_state_version"),
        page_number: url.searchParams.get("page_number"),
      });
      return route.fulfill({ status: 200, contentType: "image/png", body: png });
    }
    if (
      url.pathname === `/api/office-v2/user-templates/${TEMPLATE_REF}/approve` &&
      method === "POST"
    ) {
      approvalBodies.push(request.postDataJSON());
      const approved = templateFixture({
        state_version: 2,
        status: "approved",
        can_approve: false,
        can_instantiate: true,
      });
      currentTemplates = [approved];
      return fulfillJson(route, { template: approved, idempotent: false });
    }
    return fulfillJson(route, { detail: `Unexpected ${method} ${url.pathname}` }, 404);
  });

  await page.goto(`/c/${SESSION_ID}`);
  const card = await openTemplateCard(page);
  await card.getByRole("button", { name: "Import Office template" }).click();
  await card.getByLabel("Template name").fill("Board briefing");
  await card.getByLabel("Template file").setInputFiles({
    name: "board.docx",
    mimeType:
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: Buffer.from("mock OOXML package"),
  });
  await card.getByRole("button", { name: "Import template" }).click();

  const record = card.getByTestId(`user-office-template-${TEMPLATE_REF}`);
  await expect(record).toContainText("Board briefing");
  await expect(record).toContainText("Authoritative preview");
  await expect(record).toContainText("State v1");
  await expect(record).toContainText("title");
  await expect(record).toContainText("summary");
  expect(importBodies).toHaveLength(1);

  await record.getByRole("button", { name: "Preview", exact: true }).click();
  await expect(
    record.getByRole("img", { name: "Board briefing template preview, page 1" }),
  ).toBeVisible();
  await expect.poll(() => previewQueries).toContainEqual({
    session_id: SESSION_ID,
    workspace_instance_id: WORKSPACE_INSTANCE_ID,
    revision: "1",
    expected_state_version: "1",
    page_number: "1",
  });

  page.once("dialog", (dialog) => dialog.accept());
  await record.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(record).toContainText("State v2");
  await expect(record).toContainText("Available for Office file creation.");
  await expect.poll(() => approvalBodies).toEqual([
    {
      session_id: SESSION_ID,
      workspace_instance_id: WORKSPACE_INSTANCE_ID,
      revision: 1,
      expected_state_version: 1,
      expected_source_sha256: SOURCE_SHA,
      expected_render_cache_key: CACHE_KEY,
    },
  ]);

  await record.getByRole("button", { name: "Copy template reference" }).click();
  await expect
    .poll(() => page.evaluate(() => navigator.clipboard.readText()))
    .toBe(TEMPLATE_REF);
});

test("an approximate needs-review revision stays visibly unavailable and cannot be approved", async ({
  page,
}) => {
  const approximate = templateFixture({
    display_name: "Draft workbook",
    format: "xlsx",
    status: "needs_review",
    can_approve: false,
    can_instantiate: false,
    render_evidence: {
      ...templateFixture().render_evidence,
      quality: "approximate",
      renderer_id: "structural-preview",
    },
  });
  await page.route("**/api/office-v2/user-templates**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/office-v2/user-templates" && request.method() === "GET") {
      expectListBinding(request);
      return fulfillJson(route, { templates: [approximate], beta: true });
    }
    return fulfillJson(route, { detail: "Unexpected template request" }, 404);
  });

  await page.goto(`/c/${SESSION_ID}`);
  const card = await openTemplateCard(page);
  const record = card.getByTestId(`user-office-template-${TEMPLATE_REF}`);
  await expect(record).toContainText("Draft workbook");
  await expect(record).toContainText("Needs review");
  await expect(record).toContainText("Approximate preview");
  await expect(record).toContainText(
    "Unavailable for Office file creation until the server marks this revision approved.",
  );
  await expect(
    record.getByRole("button", { name: "Approve", exact: true }),
  ).toHaveCount(0);
});

test("delete sends the exact workspace and state CAS before removing the record", async ({
  page,
}) => {
  const approved = templateFixture({
    display_name: "Approved deck",
    format: "pptx",
    state_version: 7,
    status: "approved",
    can_approve: false,
    can_instantiate: true,
  });
  const deleteBodies: unknown[] = [];
  await page.route("**/api/office-v2/user-templates**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/office-v2/user-templates" && request.method() === "GET") {
      expectListBinding(request);
      return fulfillJson(route, { templates: [approved], beta: true });
    }
    if (
      url.pathname === `/api/office-v2/user-templates/${TEMPLATE_REF}` &&
      request.method() === "DELETE"
    ) {
      deleteBodies.push(request.postDataJSON());
      return fulfillJson(route, { template: approved });
    }
    return fulfillJson(route, { detail: "Unexpected template request" }, 404);
  });

  await page.goto(`/c/${SESSION_ID}`);
  const card = await openTemplateCard(page);
  const record = card.getByTestId(`user-office-template-${TEMPLATE_REF}`);
  page.once("dialog", (dialog) => dialog.accept());
  await record.getByRole("button", { name: "Delete", exact: true }).click();

  await expect.poll(() => deleteBodies).toEqual([
    {
      session_id: SESSION_ID,
      workspace_instance_id: WORKSPACE_INSTANCE_ID,
      revision: 1,
      expected_state_version: 7,
    },
  ]);
  await expect(record).toHaveCount(0);
});
