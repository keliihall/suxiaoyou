import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  WINDOWS_PE_MACHINES,
  parsePeMachine,
  verify,
} from "./verify-windows-pe.mjs";

const SCRIPT = fileURLToPath(
  new URL("./verify-windows-pe.mjs", import.meta.url),
);

function peImage(machine, peOffset = 0x80) {
  const image = Buffer.alloc(peOffset + 24);
  image.write("MZ", 0, "ascii");
  image.writeUInt32LE(peOffset, 0x3c);
  image.write("PE\0\0", peOffset, "binary");
  image.writeUInt16LE(machine, peOffset + 4);
  return image;
}

test("parses x64 and ARM64 COFF Machine values", () => {
  assert.equal(
    parsePeMachine(peImage(WINDOWS_PE_MACHINES.x64)),
    0x8664,
  );
  assert.equal(
    parsePeMachine(peImage(WINDOWS_PE_MACHINES.arm64)),
    0xaa64,
  );
  assert.equal(
    parsePeMachine(new Uint8Array(peImage(WINDOWS_PE_MACHINES.arm64))),
    0xaa64,
  );
});

test("rejects truncated, non-MZ, out-of-range, and non-PE images", () => {
  assert.throws(() => parsePeMachine(Buffer.alloc(8)), /DOS header/u);

  const noMz = peImage(WINDOWS_PE_MACHINES.x64);
  noMz[0] = 0;
  assert.throws(() => parsePeMachine(noMz), /MZ DOS signature/u);

  const outOfRange = peImage(WINDOWS_PE_MACHINES.x64);
  outOfRange.writeUInt32LE(outOfRange.length, 0x3c);
  assert.throws(() => parsePeMachine(outOfRange), /outside the image/u);

  const noPe = peImage(WINDOWS_PE_MACHINES.x64);
  noPe.writeUInt32LE(0, 0x80);
  assert.throws(() => parsePeMachine(noPe), /PE\\0\\0 signature/u);
});

test("verifies every supplied image and reports architecture mismatches", () => {
  const x64 = peImage(WINDOWS_PE_MACHINES.x64);
  const arm64 = peImage(WINDOWS_PE_MACHINES.arm64);
  const files = new Map([
    ["backend.exe", x64],
    ["node.exe", x64],
  ]);
  const results = verify(
    "x64",
    [...files.keys()],
    { readFile: (path) => files.get(path) },
  );

  assert.deepEqual(
    results.map(({ path, architecture, machine, machine_hex: machineHex }) => ({
      path,
      architecture,
      machine,
      machine_hex: machineHex,
    })),
    [
      {
        path: "backend.exe",
        architecture: "x64",
        machine: 0x8664,
        machine_hex: "0x8664",
      },
      {
        path: "node.exe",
        architecture: "x64",
        machine: 0x8664,
        machine_hex: "0x8664",
      },
    ],
  );
  assert.throws(
    () => verify("arm64", ["backend.exe"], { readFile: () => x64 }),
    /expected arm64 0xAA64, found 0x8664/u,
  );
  assert.throws(
    () => verify("x64", ["backend.exe"], { readFile: () => arm64 }),
    /expected x64 0x8664, found 0xAA64/u,
  );
  assert.throws(() => verify("ia32", ["backend.exe"]), /x64 or arm64/u);
  assert.throws(() => verify("toString", ["backend.exe"]), /x64 or arm64/u);
  assert.throws(() => verify("x64", []), /at least one PE path/u);
});

test("CLI validates multiple PE files and fails closed on one mismatch", (t) => {
  const root = mkdtempSync(join(tmpdir(), "verify-windows-pe-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const backend = join(root, "suxiaoyou-backend.exe");
  const node = join(root, "node.exe");
  writeFileSync(backend, peImage(WINDOWS_PE_MACHINES.arm64));
  writeFileSync(node, peImage(WINDOWS_PE_MACHINES.arm64));

  const accepted = spawnSync(
    process.execPath,
    [SCRIPT, "arm64", backend, node],
    { encoding: "utf8" },
  );
  assert.equal(accepted.status, 0, accepted.stderr);
  assert.equal(
    accepted.stdout.match(/\[verify-windows-pe\] arm64 0xAA64:/gu)?.length,
    2,
  );

  writeFileSync(node, peImage(WINDOWS_PE_MACHINES.x64));
  const rejected = spawnSync(
    process.execPath,
    [SCRIPT, "arm64", backend, node],
    { encoding: "utf8" },
  );
  assert.equal(rejected.status, 1);
  assert.match(rejected.stderr, /PE machine mismatch/u);
  assert.match(rejected.stderr, /node\.exe/u);
});

test("CLI rejects an unsupported architecture or missing path", () => {
  const invalidArchitecture = spawnSync(
    process.execPath,
    [SCRIPT, "ia32", "app.exe"],
    { encoding: "utf8" },
  );
  assert.equal(invalidArchitecture.status, 2);
  assert.match(invalidArchitecture.stderr, /Usage:/u);

  const missingPath = spawnSync(
    process.execPath,
    [SCRIPT, "x64"],
    { encoding: "utf8" },
  );
  assert.equal(missingPath.status, 2);
  assert.match(missingPath.stderr, /Usage:/u);
});
