import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  curatedLicenseDocument,
  licenseFiles,
  normalizeRepository,
} from "./generate-javascript-licenses.mjs";

test("normalizes GitHub shorthand for monorepo license fallback", () => {
  assert.equal(
    normalizeRepository("vercel/next.js"),
    "https://github.com/vercel/next.js",
  );
  assert.equal(
    normalizeRepository("git+https://github.com/vercel/next.js.git"),
    "https://github.com/vercel/next.js",
  );
});

test("keeps exact version-scoped notices for npm artifacts that omit LICENSE", () => {
  assert.match(
    curatedLicenseDocument("react-remove-scroll-bar@2.3.8"),
    /Copyright \(c\) 2025 Anton Korzunov <thekashey@gmail\.com>[\s\S]*Permission is hereby granted/,
  );
  assert.match(
    curatedLicenseDocument("isarray@1.0.0"),
    /Copyright \(c\) 2013 Julian Gruber <julian@juliangruber\.com>[\s\S]*Permission is hereby granted/,
  );
  assert.equal(curatedLicenseDocument("react-remove-scroll-bar@2.3.7"), "");
});

test("collects nested vendored notices only when explicitly requested", (t) => {
  const packagePath = mkdtempSync(join(tmpdir(), "javascript-license-package-"));
  t.after(() => rmSync(packagePath, { recursive: true, force: true }));

  writeFileSync(join(packagePath, "LICENSE"), "root license\n");
  const vendorPath = join(packagePath, "lib-vendor", "dependency");
  mkdirSync(vendorPath, { recursive: true });
  writeFileSync(join(vendorPath, "LICENSE"), "vendored license\n");
  writeFileSync(join(vendorPath, "Copyright.js"), "not a license document\n");

  assert.deepEqual(licenseFiles(packagePath), ["LICENSE"]);
  assert.deepEqual(licenseFiles(packagePath, { recursive: true }), [
    "LICENSE",
    join("lib-vendor", "dependency", "LICENSE"),
  ]);
});
