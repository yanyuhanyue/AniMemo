import test from "node:test";
import assert from "node:assert/strict";
import { BOOT_PROGRESS_STAGES, nextMonotonicProgress } from "../src/lib/bootProgress.js";
import { getFeaturedScoreTier, getFilledStarCount, getRatingTier, normalizeRating } from "../src/lib/rating.js";

test("normalizes rating values and preserves zero", () => {
  assert.equal(normalizeRating(0), 0);
  assert.equal(normalizeRating("9.94"), 9.9);
  assert.equal(normalizeRating(12), 10);
  assert.equal(normalizeRating(-1), 0);
  assert.equal(normalizeRating(null), null);
  assert.equal(normalizeRating(""), null);
});

test("uses the final black red rainbow boundaries", () => {
  assert.equal(getRatingTier(0), "black");
  assert.equal(getRatingTier(8.9), "black");
  assert.equal(getRatingTier(9), "orange-red");
  assert.equal(getRatingTier(9.4), "orange-red");
  assert.equal(getRatingTier(9.5), "red");
  assert.equal(getRatingTier(9.8), "red");
  assert.equal(getRatingTier(9.9), "rainbow");
  assert.equal(getRatingTier(10), "rainbow");
  assert.equal(getRatingTier(null), "unrated");
});

test("maps ten-point ratings to five stars", () => {
  assert.equal(getFilledStarCount(8.8), 4);
  assert.equal(getFilledStarCount(9), 5);
  assert.equal(getFilledStarCount(9.9), 5);
  assert.equal(getFilledStarCount(null), 0);
});

test("uses the featured score meter tiers without changing global ratings", () => {
  assert.equal(getFeaturedScoreTier(null), "pending");
  assert.equal(getFeaturedScoreTier(0), "yellow");
  assert.equal(getFeaturedScoreTier(9.4), "yellow");
  assert.equal(getFeaturedScoreTier(9.5), "orange-red");
  assert.equal(getFeaturedScoreTier(9.8), "orange-red");
  assert.equal(getFeaturedScoreTier(9.9), "gradient");
  assert.equal(getFeaturedScoreTier(10), "gradient");
});

test("keeps boot progress monotonic across stale and out-of-order updates", () => {
  const updates = [
    BOOT_PROGRESS_STAGES.mounted,
    BOOT_PROGRESS_STAGES.dataReady,
    BOOT_PROGRESS_STAGES.fontsReady,
    BOOT_PROGRESS_STAGES.imagesReady,
    40,
    BOOT_PROGRESS_STAGES.layoutReady,
    BOOT_PROGRESS_STAGES.complete,
    60,
  ];
  const observed = updates.reduce((values, next) => {
    values.push(nextMonotonicProgress(values.at(-1) || 0, next));
    return values;
  }, []);

  assert.deepEqual(observed, [10, 55, 55, 85, 85, 95, 100, 100]);
  assert.equal(observed.every((value, index) => index === 0 || value >= observed[index - 1]), true);
});
