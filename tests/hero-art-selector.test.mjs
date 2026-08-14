import test from "node:test";
import assert from "node:assert/strict";
import { animeRecords } from "../src/data/anime.js";
import { DEFAULT_POSTER } from "../src/lib/demoMedia.js";
import { selectDailyHeroPosters, shanghaiDateKey } from "../src/lib/heroArtSelector.js";

test("hero selection uses the Asia/Shanghai calendar date deterministically", () => {
  const beforeMidnight = new Date("2026-08-12T15:59:59.000Z");
  const afterMidnight = new Date("2026-08-12T16:00:00.000Z");
  assert.equal(shanghaiDateKey(beforeMidnight), "2026-08-12");
  assert.equal(shanghaiDateKey(afterMidnight), "2026-08-13");
  assert.deepEqual(
    selectDailyHeroPosters(animeRecords, { now: afterMidnight, domain: "universe" }),
    selectDailyHeroPosters(animeRecords, { now: afterMidnight, domain: "universe" }),
  );
});

test("hero selection falls back safely when no provider-backed posters exist", () => {
  assert.deepEqual(selectDailyHeroPosters([], { now: new Date("2026-08-13T00:00:00Z") }), [DEFAULT_POSTER, DEFAULT_POSTER]);
});
