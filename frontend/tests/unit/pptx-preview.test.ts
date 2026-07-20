import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function readJson(path: string): Record<string, string> {
  return JSON.parse(readFileSync(path, "utf8")) as Record<string, string>;
}

test("PPTX preview uses the bounded scene endpoint and rejects legacy PPT first", () => {
  const renderer = readFileSync(
    "src/components/artifacts/renderers/pptx-renderer.tsx",
    "utf8",
  );
  const constants = readFileSync("src/lib/constants.ts", "utf8");

  assert.match(constants, /PPTX_PREVIEW:\s*"\/api\/files\/pptx-preview"/);
  const legacyGuard = renderer.indexOf("if (isLegacyPpt)");
  const previewRequest = renderer.indexOf("loadPptxPreview(filePath, workspace)");
  assert.ok(legacyGuard >= 0 && legacyGuard < previewRequest);
  assert.match(renderer, /extension\(filePath\) === "\.ppt"/);
  assert.match(renderer, /pptxLegacyUnsupported/);
  assert.match(renderer, /pptx_preview_busy/);
  assert.equal(
    renderer.match(/setError\(t\("pptxLegacyUnsupported"\)\)/g)?.length,
    1,
  );
});

test("PPTX scene renderer allow-lists inert raster data URLs", () => {
  const renderer = readFileSync(
    "src/components/artifacts/renderers/pptx-renderer.tsx",
    "utf8",
  );

  assert.match(renderer, /SAFE_IMAGE_DATA_URL/);
  assert.match(renderer, /png\|jpeg\|bmp/);
  assert.doesNotMatch(renderer, /png\|jpeg\|gif/);
  assert.doesNotMatch(renderer, /image\/svg\+xml/);
  assert.match(renderer, /SAFE_IMAGE_DATA_URL\.test\(asset\.dataUrl\)/);
  assert.doesNotMatch(renderer, /dangerouslySetInnerHTML/);
  assert.equal(renderer.match(/\{run\.text\}/g)?.length, 1);
  assert.match(renderer, /MAX_PPTX_MAIN_DOM_NODES = 20_000/);
  assert.match(renderer, /MAX_PPTX_RESPONSE_SCENE_NODES = 25_000/);
  assert.match(renderer, /MAX_PPTX_RESPONSE_ASSETS = 64/);
  assert.match(renderer, /boundedSlideElements/);
  assert.match(renderer, /MAX_PPTX_THUMBNAIL_ELEMENTS = 48/);
  assert.match(renderer, /<ThumbnailCanvas/);
  assert.match(renderer, /setTimeout\(\(\) => \{/);
  assert.match(
    renderer,
    /pptxPreviewRequests\.get\(key\)\?\.request === request[\s\S]*pptxPreviewRequests\.delete\(key\)/,
  );
});

test("PPTX preview exposes thumbnails, page navigation, zoom, and localized degradation", () => {
  const renderer = readFileSync(
    "src/components/artifacts/renderers/pptx-renderer.tsx",
    "utf8",
  );
  const zh = readJson("src/i18n/locales/zh/chat.json");
  const en = readJson("src/i18n/locales/en/chat.json");

  assert.match(renderer, /preview\.slides\.map/);
  assert.match(renderer, /pptxPreviousSlide/);
  assert.match(renderer, /pptxNextSlide/);
  assert.match(renderer, /pptxZoomIn/);
  assert.match(renderer, /pptxZoomOut/);
  assert.match(renderer, /onKeyDown/);
  assert.match(renderer, /ArrowLeft/);
  assert.match(renderer, /ArrowRight/);

  for (const key of [
    "pptxLegacyUnsupported",
    "pptxStaticNotice",
    "pptxExternalLinksIgnored",
    "pptxEmbeddedContentIgnored",
    "pptxDownloadOriginal",
    "pptxThumbnails",
    "pptxSceneLimitExceeded",
    "pptxDownloadLimitExceeded",
  ]) {
    assert.ok(zh[key], `missing zh translation: ${key}`);
    assert.ok(en[key], `missing en translation: ${key}`);
  }
});

test("PPTX preview keeps a bounded responsive height chain in narrow windows", () => {
  const panel = readFileSync("src/components/artifacts/artifact-panel.tsx", "utf8");
  const office = readFileSync(
    "src/components/artifacts/renderers/office-v2-renderer.tsx",
    "utf8",
  );
  const renderer = readFileSync(
    "src/components/artifacts/renderers/pptx-renderer.tsx",
    "utf8",
  );

  assert.equal(
    panel.match(/flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden/g)?.length,
    2,
  );
  assert.match(panel, /window\.addEventListener\("resize", clampPanelToWindow\)/);
  assert.match(office, /h-full min-h-0 w-full flex-1 flex-col overflow-hidden/);
  assert.match(renderer, /className="flex min-h-0 min-w-0 flex-1"/);
  assert.match(renderer, /new ResizeObserver\(update\)/);
});
