import test from "node:test";
import assert from "node:assert/strict";
import { getTagVisualUnits, isOversizedTag, splitTagsIntoRows } from "../src/lib/tagLayout.js";

test("counts CJK glyphs as full width and Latin glyphs as half width", () => {
  assert.equal(getTagVisualUnits("运动"), 4);
  assert.equal(getTagVisualUnits("P.A.WORKS"), 9);
  assert.equal(getTagVisualUnits("CygamesPictures"), 15);
  assert.equal(getTagVisualUnits("凉宫ハルヒの憂鬱"), 16);
});

test("keeps mixed-script labels readable while preserving three tags per row", () => {
  assert.deepEqual(
    splitTagsIntoRows(["看过", "赛马娘", "运动", "CygamesPictures", "2025年4月", "漫画改"]),
    [["看过", "赛马娘", "运动"], ["CygamesPictures"], ["2025年4月", "漫画改"]],
  );

  assert.deepEqual(
    splitTagsIntoRows(["看过", "P.A.WORKS", "运动", "游戏改", "百合"]),
    [["看过", "P.A.WORKS"], ["运动", "游戏改", "百合"]],
  );

  assert.deepEqual(
    splitTagsIntoRows(["看过", "京阿尼", "凉宫ハルヒの憂鬱", "SOS团", "校园", "团长"]),
    [["看过", "京阿尼"], ["凉宫ハルヒの憂鬱"], ["SOS团", "校园", "团长"]],
  );
});

test("marks only labels that need a multiline capsule as oversized", () => {
  assert.equal(isOversizedTag("CygamesPictures"), false);
  assert.equal(isOversizedTag("凉宫ハルヒの憂鬱"), false);
  assert.equal(isOversizedTag("Re:ゼロから始める異世界生活"), true);
  assert.equal(isOversizedTag("我的青春恋爱物语果然有问题"), true);
});
