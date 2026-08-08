import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { tagColors } from "../src/data/catalogData.js";

test("uses the reference preset palette for topic tags", () => {
  assert.equal(tagColors["日常"], "blue");
  assert.equal(tagColors["治愈"], "emerald");
  assert.equal(tagColors["搞笑"], "orange");
  assert.equal(tagColors["萌系"], "amber");
  assert.equal(tagColors["异世界"], "violet");
});

test("anime file modals reuse the saved record colors shown by catalog cards", async () => {
  const [catalog, featured, css] = await Promise.all([
    readFile(new URL("../src/components/AnimeModal.jsx", import.meta.url), "utf8"),
    readFile(new URL("../src/components/featured/FeaturedAnimeModal.jsx", import.meta.url), "utf8"),
    readFile(new URL("../src/styles.css", import.meta.url), "utf8"),
  ]);

  assert.match(catalog, /<TagChip key=\{tag\} tag=\{tag\} color=\{draft\.tagColors\?\.\[tag\]\}/);
  assert.match(featured, /<TagChip tag=\{tag\} color=\{anime\.tagColors\?\.\[tag\]\}/);
  assert.match(css, /\.featured-anime-modal__tags \.tag-emerald \{ background: #ecfdf5; color: #059669; \}/);
  assert.match(css, /\.featured-anime-modal__tags \.tag-chip:nth-child\(even\) \{ transform: rotate\(1deg\); \}/);
  assert.match(css, /\.anime-modal__panel--catalog \.modal-tags > div \{ gap: 8px; \}/);
});
