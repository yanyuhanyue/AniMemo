import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  LONG_IDLE_DAYS,
  buildSmartReminders,
  deriveEntryStatistics,
  matchesActivityFilter,
  runBounded,
  sortContinueWatching,
  suggestNextEpisode,
} from "../src/lib/journalExperience.js";

const dashboard = readFileSync(new URL("../src/pages/DashboardPage.jsx", import.meta.url), "utf8");
const animeModal = readFileSync(new URL("../src/components/AnimeModal.jsx", import.meta.url), "utf8");
const editor = readFileSync(new URL("../src/components/dashboard/EditAnimeRecordContent.jsx", import.meta.url), "utf8");
const sections = readFileSync(new URL("../src/components/dashboard/JournalDashboardSections.jsx", import.meta.url), "utf8");
const bulk = readFileSync(new URL("../src/components/dashboard/BulkManagementToolbar.jsx", import.meta.url), "utf8");
const addAnime = readFileSync(new URL("../src/components/dashboard/AddAnimeModal.jsx", import.meta.url), "utf8");

test("continue watching ordering prefers latest watch then deterministic update time", () => {
  const records = [
    { id: 3, status: "completed", lastWatchedOn: "2026-08-09" },
    { id: 2, status: "watching", lastWatchedOn: null, updatedAt: "2026-08-08T10:00:00Z" },
    { id: 1, status: "watching", lastWatchedOn: "2026-08-09", updatedAt: "2026-08-01T10:00:00Z" },
    { id: 4, status: "watching", lastWatchedOn: null, updatedAt: "2026-08-07T10:00:00Z" },
  ];
  assert.deepEqual(sortContinueWatching(records).map((record) => record.id), [1, 2, 4]);
});

test("next episode suggestion advances the latest canonical range and respects total episodes", () => {
  const records = [
    { watched_on: "2026-08-08", episode_start: 1, episode_end: 3, sequence: 1 },
    { watched_on: "2026-08-09", episode_start: 4, episode_end: 6, sequence: 2 },
  ];
  assert.deepEqual(suggestNextEpisode(records, "12"), { episodeStart: "7", episodeEnd: "7" });
  assert.deepEqual(suggestNextEpisode([{ watched_on: "2026-08-09", episode_end: 12 }], "12"), { episodeStart: "", episodeEnd: "" });
});

test("entry statistics use explicit dates, brush data, and episode ranges", () => {
  const result = deriveEntryStatistics([
    { watched_on: "2026-08-09", brush_number: 2, episode_start: 4, episode_end: 6 },
    { watched_on: "2026-08-01", brush_number: 1, episode_start: 1, episode_end: 3 },
  ]);
  assert.deepEqual(result, {
    count: 2,
    firstWatchedOn: "2026-08-01",
    lastWatchedOn: "2026-08-09",
    highestBrushNumber: 2,
    episodeStart: 1,
    episodeEnd: 6,
  });
});

test("activity filters and reminders use centralized deterministic rules", () => {
  const now = new Date("2026-08-09T12:00:00+08:00");
  const record = { id: 1, status: "watching", score: null, lastWatchedOn: "2026-07-01", updatedAt: "2026-08-08T00:00:00+08:00", externalIdentities: [] };
  assert.equal(matchesActivityFilter(record, "external-unbound", now), true);
  assert.equal(matchesActivityFilter(record, "recent-updated", now), true);
  assert.equal(matchesActivityFilter(record, "recent-watched", now), false);
  assert.equal(LONG_IDLE_DAYS, 14);
  assert.ok(buildSmartReminders([record], now).some((item) => item.type === "idle"));
});

test("bounded bulk runner reports partial failure without hiding successful work", async () => {
  const results = await runBounded([1, 2, 3], async (value) => {
    if (value === 2) throw new Error("failed");
    return value * 10;
  }, 2);
  assert.deepEqual(results.map((result) => result.status), ["fulfilled", "rejected", "fulfilled"]);
  assert.deepEqual(results.filter((result) => result.status === "fulfilled").map((result) => result.value), [10, 30]);
});

test("dashboard wires analytics, quick status, bulk management, reminders, and recoverable deep links", () => {
  assert.match(sections, /继续观看/);
  assert.match(sections, /手账统计与最近动态/);
  assert.match(sections, /待完善/);
  assert.match(dashboard, /api\.patch\(`entries\/\$\{record\.id\}\/`, \{ watch_status: nextStatus \}\)/);
  assert.match(dashboard, /runBounded\(targets/);
  assert.match(dashboard, /成功 \$\{successful\.length\}，失败 \$\{failed\}/);
  assert.match(dashboard, /params\.set\("entry", record\.id\)/);
  assert.match(dashboard, /openingEntryRef\.current = String\(record\.id\)/);
  assert.match(dashboard, /openingEntryRef\.current === String\(entryId\)/);
  assert.match(dashboard, /location\.search\.includes\("entry="\) \|\| openingEntryRef\.current/);
  assert.match(dashboard, /params\.delete\("entry"\)/);
  assert.match(dashboard, /没有找到这部作品，或它不属于当前账号/);
  assert.match(animeModal, /parentCount >= localHistory\.length/);
  assert.match(animeModal, /next\.watchHistory = localHistory/);
  assert.match(bulk, /批量管理/);
});

test("entry hub keeps external data independent and uses server-issued sync flow", () => {
  assert.match(editor, /role="tabpanel" aria-labelledby="entry-hub-tab-external"/);
  assert.match(editor, /ExternalMediaIdentityPanel/);
  assert.match(editor, /onOpenExternalAccount/);
  assert.match(editor, /suggestNextEpisode/);
  assert.match(editor, /api\.patch\(`entries\/\$\{draft\.id\}\/watch-history\/\$\{editing\.id\}\/`/);
});

test("add anime keeps every canonical status and warns about duplicate Bangumi subjects", () => {
  assert.match(addAnime, /<option value="dropped">弃番<\/option>/);
  assert.match(addAnime, /boundBangumiIds/);
  assert.match(addAnime, /已经绑定到你的另一部手账/);
  assert.match(addAnime, /disabled=\{selectedBangumiId !== null \|\| alreadyBound\}/);
});
