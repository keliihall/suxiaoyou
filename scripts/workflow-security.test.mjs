import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const workflows = new Map(
  ["release.yml", "ci.yml"].map((name) => [
    name,
    readFileSync(join(root, ".github", "workflows", name), "utf8"),
  ]),
);

function actionReferences(workflow) {
  return [...workflow.matchAll(/^\s*-?\s*uses:\s*([^\s#]+).*$/gm)].map(
    (match) => match[1],
  );
}

function directActionStepBlocks(workflow, action) {
  const lines = workflow.split("\n");
  const blocks = [];
  for (let index = 0; index < lines.length; index += 1) {
    const match = /^(\s*)- uses:\s*([^\s#]+)/.exec(lines[index]);
    if (!match || !match[2].startsWith(`${action}@`)) continue;
    const indentation = match[1].length;
    let end = index + 1;
    while (
      end < lines.length &&
      !new RegExp(`^\\s{${indentation}}- (?:uses:|name:)`).test(lines[end])
    ) {
      end += 1;
    }
    blocks.push(lines.slice(index, end).join("\n"));
  }
  return blocks;
}

test("all GitHub Actions are immutable full-SHA references", () => {
  for (const [name, workflow] of workflows) {
    const references = actionReferences(workflow);
    assert.ok(references.length > 0, `${name} has no actions`);
    for (const reference of references) {
      assert.match(
        reference,
        /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+@[0-9a-f]{40}$/,
        `${name}: mutable action reference ${reference}`,
      );
    }
  }
});

test("release builds do not consume mutable dependency caches", () => {
  const release = workflows.get("release.yml");
  assert.doesNotMatch(release, /swatinem\/rust-cache/i);
  assert.doesNotMatch(release, /^\s+cache(?:-dependency-path)?:/m);
});

test("checkout never persists credentials and workflows use least privilege", () => {
  for (const [name, workflow] of workflows) {
    assert.match(workflow, /^permissions:\n\s{2}contents:\s*read\s*$/m, name);
    const checkoutSteps = directActionStepBlocks(workflow, "actions/checkout");
    assert.ok(checkoutSteps.length > 0, `${name} has no checkout steps`);
    for (const checkoutStep of checkoutSteps) {
      assert.match(
        checkoutStep,
        /persist-credentials:\s*false/,
        `${name} checkout persists token`,
      );
    }
  }
});

test("release uses the audited Rust compiler version", () => {
  const release = workflows.get("release.yml");
  const rustSteps = directActionStepBlocks(release, "dtolnay/rust-toolchain");
  assert.equal(rustSteps.length, 4);
  for (const rustStep of rustSteps) assert.match(rustStep, /toolchain:\s*"1\.96\.1"/);
});
