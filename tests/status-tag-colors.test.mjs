import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { statusTagColors } from "../src/data/catalogData.js";

test("keeps all watch-state tag colors fixed to the reference palette", () => {
  assert.deepEqual(statusTagColors, {
    看过: "coral",
    在看: "teal",
    想看: "yellow",
    搁置: "white",
  });
});

test("does not allow custom tag colors to override watch-state chips", async () => {
  const source = await readFile(new URL("../src/components/TagChip.jsx", import.meta.url), "utf8");
  assert.match(source, /const fixedStatusColor = statusTagColors\[tag\]/);
  assert.match(source, /const customColor = !fixedStatusColor/);
});

test("renders the on-hold poster status as a white chip", async () => {
  const css = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(css, /\.anime-poster-card__status--on_hold\s*\{\s*background:\s*#fff;\s*\}/);
  assert.match(css, /\.tag-white\s*\{\s*background:\s*#fff;\s*\}/);
});
