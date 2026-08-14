import test from "node:test";
import assert from "node:assert/strict";
import { animeRecords } from "../src/data/anime.js";
import { DEFAULT_POSTER, resolveDemoIdentity, resolveDemoPoster } from "../src/lib/demoMedia.js";

test("every demo anime keeps title, Bangumi subject identity, and provider poster together", () => {
  assert.equal(animeRecords.length, 16);
  for (const record of animeRecords) {
    const identity = resolveDemoIdentity(record);
    assert.equal(identity?.provider, "bangumi");
    assert.match(identity?.external_id || "", /^\d+$/);
    assert.equal(resolveDemoPoster(record), record.posterUrl);
    assert.match(record.posterUrl, new RegExp(`/${identity.external_id}_[A-Za-z0-9]+\\.jpg$`));
    assert.doesNotMatch(record.posterUrl, /poster-\d+\.webp/);
  }
});

test("untrusted or missing poster URLs use the AniMemo fallback", () => {
  assert.equal(resolveDemoPoster({ posterUrl: "https://example.com/poster.jpg" }), DEFAULT_POSTER);
  assert.equal(resolveDemoPoster({}), DEFAULT_POSTER);
});
