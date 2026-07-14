#!/usr/bin/env node

import { writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { setTimeout as delay } from "node:timers/promises";

const [mode, portText, pidFile, termMarker, startupDelayText = "0"] = process.argv.slice(2);
const port = Number(portText);

await delay(Number(startupDelayText));
writeFileSync(pidFile, String(process.pid));

// Arm each mode's termination behavior before reporting ready. The fixture then
// waits for go so the parent can install its ChildProcess listeners first.
let start;

if (mode === "success" || mode === "unauthorized") {
  let server;
  process.on("SIGTERM", () => {
    if (server?.listening) server.close(() => process.exit(0));
    else process.exit(0);
  });
  start = () => {
    server = createServer((_request, response) => {
      if (mode === "unauthorized") {
        response.writeHead(401, { "content-type": "application/json" });
        response.end('{"detail":"Authentication required"}');
      } else {
        response.writeHead(200, { "content-type": "text/html" });
        response.end("<!DOCTYPE html><html><body>ready</body></html>");
      }
    });
    server.listen(port, "127.0.0.1");
  };
} else if (mode === "natural-exit") {
  start = () => setTimeout(() => process.exit(23), 30);
} else if (mode === "natural-signal") {
  start = () => setTimeout(() => process.kill(process.pid, "SIGTERM"), 30);
} else if (mode === "timeout") {
  process.on("SIGTERM", () => process.exit(0));
  start = () => setInterval(() => {}, 1_000);
} else if (mode === "ignore-term") {
  process.on("SIGTERM", () => {
    writeFileSync(termMarker, "received");
  });
  start = () => setInterval(() => {}, 1_000);
} else {
  process.exit(64);
}

process.once("message", (message) => {
  if (message?.type !== "go") process.exit(65);
  start();
});

if (typeof process.send !== "function") {
  throw new Error("fixture requires an IPC channel");
}
process.send({ type: "ready", pid: process.pid });
