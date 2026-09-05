import test from "node:test";
import assert from "node:assert/strict";

import {
  hydrateDemoAnimeRecords,
  hydrateDemoFeaturedColumns,
  hydrateDemoUniverseOwners,
  reconcileDemoRecords,
} from "../src/lib/demoMedia.js";
import {
  ANIMEMO_AVATAR_PATH,
  ANIMEMO_POSTER_FALLBACK_PATH,
} from "../src/lib/mediaAssets.js";
import { getDemoUniverseOwner } from "../src/data/universe.js";

const record = {
  id: 7,
  title: "《对我垂涎欲滴的非人少女》",
  bangumiJapaneseTitle: "私を喰べたい、ひとでなし",
  resourceIdentity: { provider: "bangumi", externalId: "520842" },
  poster: ANIMEMO_POSTER_FALLBACK_PATH,
};

function clientFor(payload) {
  return {
    get: async (url) => {
      assert.equal(
        url,
        "external-media/providers/bangumi/subjects/520842/",
      );
      return { data: payload };
    },
  };
}

test("hydrates a demo poster only from its matching Bangumi subject", async () => {
  const poster = "https://lain.bgm.tv/pic/cover/l/2c/af/520842_J06fL.jpg";
  const [hydrated] = await hydrateDemoAnimeRecords([record], {
    client: clientFor({
      provider: "bangumi",
      external_id: "520842",
      japanese_title: record.bangumiJapaneseTitle,
      poster_url: poster,
      canonical_url: "https://bgm.tv/subject/520842",
    }),
  });
  assert.equal(hydrated.poster, poster);
  assert.equal(hydrated.posterSource, "default_url");
  assert.equal(hydrated.externalIdentities[0].external_id, "520842");
});

test("fails safely when identity, title, host, or provider lookup is invalid", async () => {
  const invalidPayloads = [
    { provider: "bangumi", external_id: "1", japanese_title: record.bangumiJapaneseTitle, poster_url: "https://lain.bgm.tv/wrong.jpg" },
    { provider: "bangumi", external_id: "520842", japanese_title: "別作品", poster_url: "https://lain.bgm.tv/wrong.jpg" },
    { provider: "bangumi", external_id: "520842", japanese_title: record.bangumiJapaneseTitle, poster_url: "https://evil.example/wrong.jpg" },
    null,
  ];
  for (const payload of invalidPayloads) {
    const client = payload === null
      ? { get: async () => { throw new Error("offline"); } }
      : clientFor(payload);
    const [hydrated] = await hydrateDemoAnimeRecords([record], { client });
    assert.equal(hydrated.poster, ANIMEMO_POSTER_FALLBACK_PATH);
    assert.equal(hydrated.posterOriginal, "");
  }
});

test("reconciles legacy demo records by stable id without preserving stale titles or posters", () => {
  const [reconciled] = reconcileDemoRecords(
    [{ ...record, title: "旧版标题", poster: "/assets/posters/poster-02.webp" }],
    [record],
  );
  assert.equal(reconciled.title, record.title);
  assert.deepEqual(reconciled.resourceIdentity, record.resourceIdentity);
  assert.equal(reconciled.poster, ANIMEMO_POSTER_FALLBACK_PATH);
});

test("featured and universe demo media keep brand avatars separate from anime posters", async () => {
  const options = {
    client: clientFor({
      provider: "bangumi",
      external_id: "520842",
      japanese_title: record.bangumiJapaneseTitle,
      poster_url: "https://lain.bgm.tv/pic/cover/l/2c/af/520842_J06fL.jpg",
    }),
  };
  const [column] = await hydrateDemoFeaturedColumns([{
    authorAvatar: "/assets/legacy-author.webp",
    cover: "/assets/legacy-cover.webp",
    anime: record,
    relatedAnime: [record],
  }], options);
  assert.equal(column.authorAvatar, ANIMEMO_AVATAR_PATH);
  assert.equal(column.cover, column.anime.poster);

  const [owner] = await hydrateDemoUniverseOwners([{
    avatar: "/assets/legacy-avatar.webp",
    records: [record],
    top_picks: [record],
  }], options);
  assert.equal(owner.avatar, ANIMEMO_AVATAR_PATH);
  assert.equal(owner.top_picks[0].poster, owner.records[0].poster);
});

test("retired demo links remain compatible without restoring the retired identity", () => {
  const legacySlug = String.fromCodePoint(100, 101, 109, 111, 45, 120, 104);
  assert.equal(getDemoUniverseOwner(legacySlug)?.public_slug, "animemo-demo");
});
