import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";


test("DOCX fallback exposes every rendered page inside a real scroll viewport", () => {
  const panel = readFileSync("src/components/artifacts/artifact-panel.tsx", "utf8");
  const renderer = readFileSync(
    "src/components/artifacts/renderers/docx-renderer.tsx",
    "utf8",
  );
  const css = readFileSync("src/app/globals.css", "utf8");
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));

  assert.equal(
    panel.match(/flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden/g)?.length,
    2,
  );
  assert.match(renderer, /className: "docx"/);
  assert.match(renderer, /\.docx-wrapper > section\.docx/);
  assert.match(renderer, /setPageCount\(Math\.max\(1, pages\.length\)\)/);
  assert.match(renderer, /ref=\{scrollRef\}[\s\S]*onScroll=\{syncCurrentPage\}/);
  assert.match(renderer, /goToPage\(currentPage - 1\)/);
  assert.match(renderer, /goToPage\(currentPage \+ 1\)/);
  assert.match(renderer, /\{currentPage\} \/ \{Math\.max\(1, pageCount\)\}/);
  assert.match(css, /\.docx-wrapper > section\.docx/);
  assert.equal(zh.docxPreviousPage, "上一页 Word 预览");
  assert.equal(zh.docxNextPage, "下一页 Word 预览");
});
