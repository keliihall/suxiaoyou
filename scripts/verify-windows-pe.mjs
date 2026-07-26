#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

export const WINDOWS_PE_MACHINES = Object.freeze({
  x64: 0x8664,
  arm64: 0xaa64,
});

const DOS_HEADER_MINIMUM_BYTES = 0x40;
const PE_OFFSET_FIELD = 0x3c;
const COFF_MACHINE_OFFSET = 4;
const COFF_MACHINE_END = COFF_MACHINE_OFFSET + 2;
const PE_SIGNATURE = 0x00004550;

function asBuffer(bytes) {
  if (Buffer.isBuffer(bytes)) return bytes;
  if (bytes instanceof Uint8Array) {
    return Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  }
  throw new TypeError("PE image must be a Buffer or Uint8Array");
}

function machineHex(machine) {
  return `0x${machine.toString(16).padStart(4, "0").toUpperCase()}`;
}

export function parsePeMachine(bytes) {
  const image = asBuffer(bytes);
  if (image.length < DOS_HEADER_MINIMUM_BYTES) {
    throw new Error("PE image is truncated before the complete DOS header");
  }
  if (image[0] !== 0x4d || image[1] !== 0x5a) {
    throw new Error("PE image is missing the MZ DOS signature");
  }

  const peOffset = image.readUInt32LE(PE_OFFSET_FIELD);
  if (
    peOffset < DOS_HEADER_MINIMUM_BYTES ||
    peOffset > image.length - COFF_MACHINE_END
  ) {
    throw new Error("PE header offset is outside the image");
  }
  if (image.readUInt32LE(peOffset) !== PE_SIGNATURE) {
    throw new Error("PE image is missing the PE\\0\\0 signature");
  }
  return image.readUInt16LE(peOffset + COFF_MACHINE_OFFSET);
}

export function verify(
  expectedArchitecture,
  paths,
  { readFile = readFileSync } = {},
) {
  if (!Object.hasOwn(WINDOWS_PE_MACHINES, expectedArchitecture)) {
    throw new Error("expected architecture must be x64 or arm64");
  }
  const expectedMachine = WINDOWS_PE_MACHINES[expectedArchitecture];
  if (!Array.isArray(paths) || paths.length === 0) {
    throw new Error("at least one PE path is required");
  }
  if (typeof readFile !== "function") {
    throw new TypeError("readFile must be a function");
  }

  return paths.map((path) => {
    if (typeof path !== "string" || path.length === 0 || path !== path.trim()) {
      throw new Error("PE paths must be non-empty strings without surrounding whitespace");
    }
    let bytes;
    try {
      bytes = readFile(path);
    } catch (error) {
      throw new Error(`could not read PE image ${path}: ${error.message}`, {
        cause: error,
      });
    }

    let actualMachine;
    try {
      actualMachine = parsePeMachine(bytes);
    } catch (error) {
      throw new Error(`invalid PE image ${path}: ${error.message}`, {
        cause: error,
      });
    }
    if (actualMachine !== expectedMachine) {
      throw new Error(
        `PE machine mismatch for ${path}: expected ` +
          `${expectedArchitecture} ${machineHex(expectedMachine)}, found ` +
          machineHex(actualMachine),
      );
    }
    return Object.freeze({
      path,
      architecture: expectedArchitecture,
      machine: actualMachine,
      machine_hex: machineHex(actualMachine),
    });
  });
}

function runCli() {
  const [expectedArchitecture, ...paths] = process.argv.slice(2);
  if (
    !Object.hasOwn(WINDOWS_PE_MACHINES, expectedArchitecture) ||
    paths.length === 0
  ) {
    console.error(
      "Usage: node scripts/verify-windows-pe.mjs <x64|arm64> <path> [<path> ...]",
    );
    process.exitCode = 2;
    return;
  }

  try {
    const results = verify(expectedArchitecture, paths);
    for (const result of results) {
      console.log(
        `[verify-windows-pe] ${result.architecture} ${result.machine_hex}: ${result.path}`,
      );
    }
  } catch (error) {
    console.error(`[verify-windows-pe] ${error.message}`);
    process.exitCode = 1;
  }
}

if (
  process.argv[1] &&
  pathToFileURL(process.argv[1]).href === import.meta.url
) {
  runCli();
}
