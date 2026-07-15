import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { artifactTypeFromExtension } from "../../src/lib/artifacts.ts";

test("common raster image extensions use the binary image preview", () => {
  for (const extension of ["png", "jpg", "jpeg", "gif", "webp", "bmp"]) {
    assert.equal(artifactTypeFromExtension(`/tmp/preview.${extension}`), "image");
    assert.equal(artifactTypeFromExtension(`C:\\Temp\\PREVIEW.${extension.toUpperCase()}`), "image");
  }

  const messagePresentation = readFileSync(
    "src/lib/message-presentation.ts",
    "utf8",
  );
  for (const extension of ["png", "jpg", "jpeg", "gif", "webp", "bmp"]) {
    assert.match(messagePresentation, new RegExp(`"\\.${extension}"`));
  }
});

test("all path-backed content artifacts share the disk loader", () => {
  const panel = readFileSync(
    "src/components/artifacts/artifact-panel-content.tsx",
    "utf8",
  );
  const filePreview = readFileSync(
    "src/components/artifacts/renderers/file-preview-renderer.tsx",
    "utf8",
  );

  const diskGuard = panel.indexOf("if (!artifact.content && artifact.filePath)");
  const typeSwitch = panel.indexOf("switch (artifact.type)");
  assert.ok(diskGuard >= 0 && diskGuard < typeSwitch);
  assert.match(panel.slice(diskGuard, typeSwitch), /<FilePreviewRenderer/);

  for (const type of ["html", "svg", "markdown", "mermaid", "react", "csv"]) {
    assert.match(filePreview, new RegExp(`case "${type}"`));
  }
  assert.match(filePreview, /type === "image"/);
  assert.match(filePreview, /<ImageRenderer filePath=\{filePath\}/);
});

test("raster and SVG blob URLs are bounded and revoked", () => {
  const imageRenderer = readFileSync(
    "src/components/artifacts/renderers/image-renderer.tsx",
    "utf8",
  );
  const svgRenderer = readFileSync(
    "src/components/artifacts/renderers/svg-renderer.tsx",
    "utf8",
  );

  assert.match(imageRenderer, /MAX_RASTER_IMAGE_PREVIEW_BYTES = 50 \* 1024 \* 1024/);
  assert.match(imageRenderer, /API\.FILES\.CONTENT_BINARY/);
  assert.match(imageRenderer, /URL\.createObjectURL\(blob\)/);
  assert.match(imageRenderer, /URL\.revokeObjectURL\(objectUrl\)/);
  assert.match(imageRenderer, /png: "image\/png"/);
  assert.match(imageRenderer, /bmp: "image\/bmp"/);
  assert.match(svgRenderer, /URL\.revokeObjectURL\(objectUrl\)/);
});
