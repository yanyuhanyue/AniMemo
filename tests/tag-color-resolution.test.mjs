import test from "node:test";
import assert from "node:assert/strict";
import {
  generatedTagColorKey,
  resolveTagColors,
} from "../src/lib/tagPresets.js";

test("treats explicit hex colors as deliberate values without legacy rewriting", () => {
  const colors = resolveTagColors(
    ["异世界", "奇幻", "TV"],
    { 异世界: "#ffe66d", 奇幻: "#ffe66d", TV: "#ffe66d" },
    { 异世界: "violet", 奇幻: "indigo" },
  );

  assert.deepEqual(colors, {
    异世界: "#ffe66d",
    奇幻: "#ffe66d",
    TV: "#ffe66d",
  });
});

test("custom tags receive a stable non-gray palette color instead of a random color", () => {
  const first = generatedTagColorKey("StudioBind");
  const second = generatedTagColorKey("StudioBind");

  assert.equal(first, second);
  assert.notEqual(first, "slate");
});

test("keeps deliberate palette choices and custom hex colors", () => {
  assert.deepEqual(
    resolveTagColors(["泡面番", "纪念色"], { 泡面番: "yellow", 纪念色: "#123456" }, {}),
    { 泡面番: "yellow", 纪念色: "#123456" },
  );
});

test("normalizes saved palette keys before resolving tag colors", () => {
  assert.deepEqual(
    resolveTagColors(["泡面番"], { 泡面番: "  YELLOW  " }, {}),
    { 泡面番: "yellow" },
  );
});
