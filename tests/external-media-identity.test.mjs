import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  bangumiIdentityFromResult,
  externalMediaResultFromApi,
  refreshRecordPatch,
  replaceProviderIdentity,
} from "../src/lib/externalMedia.js";

const addModal = readFileSync(new URL("../src/components/dashboard/AddAnimeModal.jsx", import.meta.url), "utf8");
const editor = readFileSync(new URL("../src/components/dashboard/EditAnimeRecordContent.jsx", import.meta.url), "utf8");
const identityPanel = readFileSync(new URL("../src/components/dashboard/ExternalMediaIdentityPanel.jsx", import.meta.url), "utf8");
const dashboardData = readFileSync(new URL("../src/pages/dashboardData.js", import.meta.url), "utf8");
const upgradeFixture = readFileSync(new URL("../scripts/stateful_upgrade_fixture.py", import.meta.url), "utf8");

test("Bangumi selections retain a provider-neutral external identity", () => {
  const result = externalMediaResultFromApi({
    provider: "bangumi",
    external_id: " 456 ",
    title: "测试番剧",
    japanese_title: "テスト",
    poster_url: "https://lain.bgm.tv/test.jpg",
  });
  assert.equal(result.externalId, " 456 ");
  assert.equal(result.japaneseTitle, "テスト");
  assert.deepEqual(bangumiIdentityFromResult(result), { provider: "bangumi", external_id: "456" });
  assert.equal(bangumiIdentityFromResult({ id: 123 }), null);
  assert.match(addModal, /externalIdentity:\s*bangumiIdentityFromResult\(item\)/);
  assert.match(dashboardData, /payload\.external_identity\s*=\s*record\.externalIdentity/);
  assert.match(addModal, /external-media\/providers\/bangumi\/search/);
  assert.doesNotMatch(addModal, /catalog\/bangumi-(?:search|autofill)/);
  assert.doesNotMatch(identityPanel, /result\.(?:id|name|japanese_name|eps|poster|thumbnail)\b/);
});

test("refresh applies only the approved provider-owned fields", () => {
  const changed = {
    japanese_title: { provider: "新日文名" },
    airing_period: { provider: "2026-8" },
    studio: { provider: "新公司" },
    episodes: { provider: "13" },
    poster_url: { provider: "https://lain.bgm.tv/pic/cover/l/test.jpg" },
    title: { provider: "不能覆盖" },
    description: { provider: "不能覆盖" },
    tags: { provider: ["不能覆盖"] },
    review: { provider: "不能覆盖" },
  };
  assert.deepEqual(refreshRecordPatch(changed), {
    japaneseTitle: "新日文名",
    period: "2026-8",
    studio: "新公司",
    episodes: "13",
    posterUrl: "https://lain.bgm.tv/pic/cover/l/test.jpg",
    poster: "https://lain.bgm.tv/pic/cover/l/test.jpg",
    posterSource: "default_url",
  });
});

test("refresh preserves custom poster priority", () => {
  const patch = refreshRecordPatch(
    { poster_url: { provider: "https://lain.bgm.tv/pic/cover/l/new.jpg" } },
    { poster: "https://lain.bgm.tv/pic/cover/l/custom.jpg", customPosterUrl: "https://lain.bgm.tv/pic/cover/l/custom.jpg", posterSource: "trusted_url" },
  );
  assert.deepEqual(patch, { posterUrl: "https://lain.bgm.tv/pic/cover/l/new.jpg" });
});

test("provider identity replacement does not disturb other providers", () => {
  const next = replaceProviderIdentity(
    [{ provider: "other", external_id: "a" }, { provider: "bangumi", external_id: "1" }],
    { provider: "bangumi", external_id: "2" },
  );
  assert.deepEqual(next, [{ provider: "other", external_id: "a" }, { provider: "bangumi", external_id: "2" }]);
});

test("edit workflow exposes bind, refresh, explicit metadata source choices, source link, and guarded unbind", () => {
  assert.match(editor, /\["external",\s*"外部资料",\s*"link"\]/);
  assert.match(identityPanel, /entries\/\$\{draft\.id\}\/external-identities\//);
  assert.match(identityPanel, /external-identities\/\$\{PROVIDER\}\/refresh\//);
  assert.match(identityPanel, /external-identities\/\$\{PROVIDER\}\/metadata-source\//);
  assert.match(identityPanel, /apply_metadata:\s*applyMetadata/);
  assert.match(identityPanel, /仅设为来源/);
  assert.match(identityPanel, /设为来源并应用/);
  assert.match(identityPanel, /解除后不会删除你的番剧记录、评分、评论或观看记录/);
  assert.match(identityPanel, /normalizeHttpUrl\(identity\?\.canonical_url\)/);
  assert.match(identityPanel, /href=\{canonicalUrl\}/);
  assert.match(identityPanel, /result\.episodes \? `\$\{result\.episodes\} 话` : "集数未定"/);
  assert.doesNotMatch(identityPanel, /JSON\.stringify/);
});

test("stateful upgrade gate seeds and verifies identity metadata source across restart", () => {
  assert.match(upgradeFixture, /ExternalMediaIdentity\.objects\.update_or_create/);
  assert.match(upgradeFixture, /ExternalMediaIdentity did not survive the upgrade or restart/);
  assert.match(upgradeFixture, /Existing ExternalMediaIdentity was not assigned as metadata source/);
  assert.match(upgradeFixture, /sync_baselines = \{"watch_status": \{"present": True, "value": "completed"\}\}/);
  assert.match(upgradeFixture, /ExternalCollectionSyncState partial baseline semantics changed/);
  assert.match(upgradeFixture, /Upgrade synthesized missing score or review baselines/);
});
