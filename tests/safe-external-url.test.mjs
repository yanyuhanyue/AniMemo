import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { normalizeHttpUrl } from "../src/lib/safeUrl.js";

const animeModal = readFileSync(new URL("../src/components/AnimeModal.jsx", import.meta.url), "utf8");
const featuredModal = readFileSync(new URL("../src/components/featured/FeaturedAnimeModal.jsx", import.meta.url), "utf8");
const identityPanel = readFileSync(new URL("../src/components/dashboard/ExternalMediaIdentityPanel.jsx", import.meta.url), "utf8");

test("external links accept only absolute HTTP(S) URLs without credentials", () => {
  assert.equal(normalizeHttpUrl(" https://example.com/path?q=1 "), "https://example.com/path?q=1");
  assert.equal(normalizeHttpUrl("http://example.com/path"), "http://example.com/path");
  assert.equal(normalizeHttpUrl("javascript:alert(1)"), "");
  assert.equal(normalizeHttpUrl("data:text/html,<script>alert(1)</script>"), "");
  assert.equal(normalizeHttpUrl("//example.com/path"), "");
  assert.equal(normalizeHttpUrl("/media/poster.webp", "https://animemo.example"), "https://animemo.example/media/poster.webp");
  assert.equal(normalizeHttpUrl("javascript:alert(1)", "https://animemo.example"), "");
  assert.equal(normalizeHttpUrl("https://user:password@example.com/path"), "");
  assert.equal(normalizeHttpUrl("not a URL"), "");
});

test("every API-backed external anchor uses the shared protocol gate", () => {
  assert.match(animeModal, /const baikeUrl = normalizeHttpUrl\(draft\.baikeUrl\)/);
  assert.match(animeModal, /normalizeHttpUrl\(draft\.posterOriginal \|\| draft\.poster, window\.location\.origin\)/);
  assert.match(featuredModal, /const externalUrl = normalizeHttpUrl\(anime\?\.externalUrl\)/);
  assert.match(featuredModal, /normalizeHttpUrl\(source, window\.location\.origin\)/);
  assert.match(identityPanel, /const canonicalUrl = normalizeHttpUrl\(identity\?\.canonical_url\)/);
  assert.doesNotMatch(animeModal, /href=\{draft\.baikeUrl\}/);
  assert.doesNotMatch(featuredModal, /href=\{anime\.externalUrl\}/);
  assert.doesNotMatch(identityPanel, /href=\{identity\.canonical_url\}/);
});
