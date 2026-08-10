import assert from "node:assert/strict";
import test from "node:test";

import {
  compareDashboardRecords,
  matchesDashboardQuery,
  mutationCountDelta,
  reconcileDashboardMutations,
  sortDashboardRecords,
} from "../src/pages/dashboardMutation.js";

function record(overrides = {}) {
  return {
    id: 1,
    title: "测试番剧",
    japaneseTitle: "テストアニメ",
    studio: "测试制作",
    review: "值得重看",
    period: "2026-01",
    score: null,
    status: "planned",
    statusLabel: "想看",
    visibility: "private",
    tags: ["日常"],
    externalIdentities: [],
    updatedAt: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

test("dashboard mutation matching follows the active server query semantics", () => {
  const item = record();
  assert.equal(matchesDashboardQuery(item, { search: "重看", status: "planned", tag: "日常", year: "2026" }), true);
  assert.equal(matchesDashboardQuery(item, { search: "不存在" }), false);
  assert.equal(matchesDashboardQuery(item, { status: "completed" }), false);
  assert.equal(matchesDashboardQuery(item, {
    quickFilter: { tags: ["日常"], title_keywords: ["测试"], match_mode: "all" },
  }), true);
  assert.equal(matchesDashboardQuery(item, {
    quickFilter: { tags: ["测试制作"], title_keywords: [], match_mode: "any" },
  }), false, "server quick filters only match saved tags, not display-only studio labels");
  assert.equal(matchesDashboardQuery(record({ poster: "/assets/posters/poster-01.webp" }), { activity: "needs-attention" }), true);
});

test("create, update and delete reconcile loaded records and total count without duplicates", () => {
  const first = record({ id: 1, period: "2025-01" });
  const second = record({ id: 2, title: "第二部", period: "2024-01" });
  const created = record({ id: 3, title: "新番", period: "2026-04" });
  const createResult = reconcileDashboardMutations([first, second], [{
    type: "create",
    previousRecord: null,
    nextRecord: created,
  }], { sort: "date-desc", priority: false });
  assert.deepEqual(createResult.records.map((item) => item.id), [3, 1, 2]);
  assert.equal(createResult.countDelta, 1);

  const updated = { ...first, title: "已完成", status: "completed", statusLabel: "看过" };
  const updateResult = reconcileDashboardMutations(createResult.records, [{
    type: "update",
    previousRecord: first,
    nextRecord: updated,
  }], { status: "planned", sort: "date-desc", priority: false });
  assert.deepEqual(updateResult.records.map((item) => item.id), [3, 2]);
  assert.equal(updateResult.countDelta, -1);
  assert.equal(updateResult.removedVisibleRecord, true);

  const deleteResult = reconcileDashboardMutations(updateResult.records, [{
    type: "delete",
    previousRecord: second,
    nextRecord: null,
  }], { status: "planned", sort: "date-desc", priority: false });
  assert.deepEqual(deleteResult.records.map((item) => item.id), [3]);
  assert.equal(deleteResult.countDelta, -1);
});

test("batch updates preserve failed records and sort successful values once", () => {
  const first = record({ id: 1, score: 6 });
  const second = record({ id: 2, title: "第二部", score: 8 });
  const third = record({ id: 3, title: "第三部", score: null });
  const result = reconcileDashboardMutations([first, second, third], [{
    type: "update",
    previousRecord: first,
    nextRecord: { ...first, score: 9 },
  }], { sort: "score-desc", priority: false });
  assert.deepEqual(result.records.map((item) => item.id), [1, 2, 3]);
  assert.equal(result.records.find((item) => item.id === 2).score, 8);
  assert.equal(result.countDelta, 0);
});

test("score ordering keeps null last and mutation count respects filters", () => {
  const unrated = record({ id: 1, score: null });
  const zero = record({ id: 2, score: 0 });
  const rated = record({ id: 3, score: 9.5 });
  assert.deepEqual(sortDashboardRecords([unrated, zero, rated], { sort: "score-asc", priority: false }).map((item) => item.id), [2, 3, 1]);
  assert.ok(compareDashboardRecords(rated, unrated, { sort: "score-desc", priority: false }) < 0);
  assert.equal(mutationCountDelta({
    type: "update",
    previousRecord: record({ status: "planned" }),
    nextRecord: record({ status: "completed" }),
    query: { status: "planned" },
  }), -1);
});
