import test from "node:test";
import assert from "node:assert/strict";

import { normalizeTrustedPosterHosts, validateTrustedPosterUrl } from "../src/lib/posterSources.js";

test("accepts only exact trusted HTTPS poster hosts", () => {
  const hosts = ["lain.bgm.tv", "cdn.example.com"];
  assert.equal(validateTrustedPosterUrl("https://lain.bgm.tv/pic/cover/l/test.jpg", hosts), "");
  assert.match(validateTrustedPosterUrl("http://lain.bgm.tv/test.jpg", hosts), /HTTPS/);
  assert.match(validateTrustedPosterUrl("https://evil.example/test.jpg", hosts), /白名单/);
  assert.match(validateTrustedPosterUrl("https://lain.bgm.tv.evil.example/test.jpg", hosts), /白名单/);
  assert.match(validateTrustedPosterUrl("https://127.0.0.1/test.jpg", hosts), /HTTPS/);
});

test("normalizes and deduplicates trusted poster hosts", () => {
  assert.deepEqual(normalizeTrustedPosterHosts([" CDN.EXAMPLE.COM. ", "cdn.example.com", "lain.bgm.tv"]), ["cdn.example.com", "lain.bgm.tv"]);
});

test("falls back to the built-in trusted hosts when settings are missing or empty", () => {
  assert.ok(normalizeTrustedPosterHosts().includes("lain.bgm.tv"));
  assert.ok(normalizeTrustedPosterHosts([]).includes("lain.bgm.tv"));
});
