import assert from "node:assert/strict";
import test from "node:test";

import {
  formatFullDateTime,
  formatRelativeTime,
  getSessionTimestamp,
  groupSessionsByDate,
  parseBackendDate,
} from "../../src/lib/utils.ts";

const NOW = new Date("2026-07-12T12:00:00.000Z"); // 20:00 in Asia/Shanghai

test("task timestamps use natural Shanghai calendar days", () => {
  assert.equal(formatRelativeTime("2026-07-12T11:59:30Z", NOW, "zh"), "刚刚");
  assert.equal(formatRelativeTime("2026-07-12T11:55:00Z", NOW, "zh"), "5分钟前");
  assert.equal(formatRelativeTime("2026-07-12T09:00:00Z", NOW, "zh"), "3小时前");

  // 23:59 Shanghai time on the previous date remains "yesterday" even
  // though fewer than 24 elapsed hours have passed.
  assert.equal(formatRelativeTime("2026-07-11T15:59:00Z", NOW, "zh"), "昨天");
  assert.equal(formatRelativeTime("2026-07-10T10:00:00Z", NOW, "zh"), "前天");
  assert.equal(formatRelativeTime("2026-07-09T10:00:00Z", NOW, "zh"), "7月9日");
});

test("task timestamps have equivalent concise English labels", () => {
  assert.equal(formatRelativeTime("2026-07-12T11:59:30Z", NOW, "en"), "just now");
  assert.equal(formatRelativeTime("2026-07-12T11:55:00Z", NOW, "en"), "5m ago");
  assert.equal(formatRelativeTime("2026-07-12T09:00:00Z", NOW, "en"), "3h ago");
  assert.equal(formatRelativeTime("2026-07-11T15:59:00Z", NOW, "en"), "yesterday");
  assert.equal(
    formatRelativeTime("2026-07-10T10:00:00Z", NOW, "en"),
    "2d ago",
  );
  assert.equal(formatRelativeTime("2026-07-09T10:00:00Z", NOW, "en"), "Jul 9");
});

test("task timestamps show the year only when it differs in Shanghai", () => {
  const januaryNow = new Date("2026-01-03T04:00:00.000Z");
  const previousYear = "2025-12-31T04:00:00.000Z";

  assert.equal(formatRelativeTime(previousYear, januaryNow, "zh"), "2025年12月31日");
  assert.equal(formatRelativeTime(previousYear, januaryNow, "en"), "Dec 31, 2025");
});

test("backend naive timestamps are parsed once as UTC", () => {
  assert.equal(
    parseBackendDate("2026-07-12T12:00:00.000").toISOString(),
    "2026-07-12T12:00:00.000Z",
  );
  assert.match(formatFullDateTime("2026-07-12T12:00:00.000", "zh"), /2026年7月12日.*20:00:00/);
});

test("sorting and chronological grouping select the same timestamp field", () => {
  const createdTodayUpdatedEarlier = {
    id: "created-today",
    time_created: "2026-07-12T10:00:00Z",
    time_updated: "2026-07-09T10:00:00Z",
  };
  const createdEarlierUpdatedToday = {
    id: "updated-today",
    time_created: "2026-07-09T10:00:00Z",
    time_updated: "2026-07-12T10:00:00Z",
  };
  const sessions = [createdTodayUpdatedEarlier, createdEarlierUpdatedToday];

  assert.equal(getSessionTimestamp(createdTodayUpdatedEarlier, "created"), createdTodayUpdatedEarlier.time_created);
  assert.equal(getSessionTimestamp(createdTodayUpdatedEarlier, "updated"), createdTodayUpdatedEarlier.time_updated);

  const createdGroups = groupSessionsByDate(sessions, "created", NOW);
  assert.deepEqual(createdGroups.map((group) => [group.label, group.sessions.map((session) => session.id)]), [
    ["today", ["created-today"]],
    ["previous7Days", ["updated-today"]],
  ]);

  const updatedGroups = groupSessionsByDate(sessions, "updated", NOW);
  assert.deepEqual(updatedGroups.map((group) => [group.label, group.sessions.map((session) => session.id)]), [
    ["today", ["updated-today"]],
    ["previous7Days", ["created-today"]],
  ]);
});
