import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  DASHBOARD_PAGE_SIZE,
  appendUniqueDashboardRecords,
  buildDashboardQueryParams,
  buildDashboardQueryKey,
  getDashboardNextPage,
} from "../src/pages/dashboardQuery.js";

const dashboard = readFileSync(new URL("../src/pages/DashboardPage.jsx", import.meta.url), "utf8");
const dashboardData = readFileSync(new URL("../src/pages/useDashboardData.js", import.meta.url), "utf8");
const dashboardImport = readFileSync(new URL("../src/pages/useDashboardImport.js", import.meta.url), "utf8");

test("dashboard query maps search, filters, quick filter, sort and fixed page size to the server", () => {
  const params = buildDashboardQueryParams({
    search: " 无职转生 ",
    status: "watching",
    visibility: "private",
    tag: "异世界",
    year: "2021",
    activity: "recent-watched",
    sort: "score-desc",
    priority: true,
    quickFilter: {
      id: 7,
      tags: ["成长", "冒险"],
      title_keywords: ["转生"],
      match_mode: "all",
    },
  }, { page: 3, includeFacets: true });

  assert.equal(DASHBOARD_PAGE_SIZE, 48);
  assert.equal(params.get("page"), "3");
  assert.equal(params.get("page_size"), "48");
  assert.equal(params.get("search"), "无职转生");
  assert.equal(params.get("status"), "watching");
  assert.equal(params.get("visibility"), "private");
  assert.equal(params.get("tag"), "异世界");
  assert.equal(params.get("year"), "2021");
  assert.equal(params.get("activity"), "recent-watched");
  assert.equal(params.get("ordering"), "-personal_score");
  assert.equal(params.get("priority"), "1");
  assert.deepEqual(params.getAll("quick_tags"), ["成长", "冒险"]);
  assert.deepEqual(params.getAll("quick_title_keywords"), ["转生"]);
  assert.equal(params.get("quick_match_mode"), "all");
  assert.equal(params.get("include_facets"), "1");
});

test("dashboard query key follows actual first-page params, not metadata object identity", () => {
  const base = {
    search: "",
    status: "all",
    tag: "all",
    year: "all",
    activity: "all",
    sort: "date-desc",
    priority: true,
    quickFilter: { id: "all", name: "全部", tags: [] },
  };
  const recreatedMetadata = {
    ...base,
    quickFilter: { id: "all", name: "全部", tags: [], description: "来自服务端" },
  };

  assert.equal(buildDashboardQueryKey(base), buildDashboardQueryKey(recreatedMetadata));
  assert.equal(buildDashboardQueryKey(base), "page=1&page_size=48&priority=1&ordering=-airing_period&include_facets=1");
  assert.notEqual(
    buildDashboardQueryKey(base),
    buildDashboardQueryKey({ ...base, search: "进击的巨人" }),
  );
});

test("infinite pages append once per entry id and stop when next is absent", () => {
  const appended = appendUniqueDashboardRecords(
    [{ id: 1 }, { id: 2 }],
    [{ id: 2 }, { id: 3 }, { id: 3 }, { id: 4 }],
  );
  assert.deepEqual(appended.map((item) => item.id), [1, 2, 3, 4]);
  assert.equal(getDashboardNextPage({ next: "https://example.test/api/entries/?page=7&page_size=48" }), 7);
  assert.equal(getDashboardNextPage({ next: null }), null);
});

test("dashboard keeps initial and load-more states separate with debounce and stale-response guards", () => {
  assert.match(dashboardData, /setTimeout\(\(\) => setDebouncedSearch[\s\S]*300\)/);
  assert.match(dashboardData, /requestGenerationRef/);
  assert.match(dashboardData, /AbortController/);
  assert.match(dashboardData, /generation !== requestGenerationRef\.current/);
  assert.match(dashboardData, /loadingMoreRef\.current/);
  assert.match(dashboardData, /appendUniqueDashboardRecords/);
  assert.match(dashboardData, /setRecords\(\[\]\)/);
  assert.match(dashboardData, /if \(append\) setLoadMoreError/);
  assert.match(dashboardData, /loadedCount: records\.length/);
  assert.match(dashboardData, /totalCount/);
  assert.match(dashboardData, /buildDashboardQueryKey/);
  assert.match(dashboardData, /requestQueryRef\.current/);
  assert.match(dashboardData, /presetColorsRef\.current/);
  assert.doesNotMatch(dashboardData, /useCallback\(async \(\{ page, append, generation \}\) =>[\s\S]*?\}, \[presetColors, requestQuery\]\)/);
});

test("dashboard infinite loading preserves deep links and replaces the all-record import refresh", () => {
  assert.match(dashboard, /new IntersectionObserver/);
  assert.match(dashboard, /rootMargin: "600px 0px"/);
  assert.match(dashboard, /ref=\{loadMoreRef\}/);
  assert.match(dashboard, /api\.get\(`entries\/\$\{entryId\}\/`\)/);
  assert.doesNotMatch(dashboard, /不断加载|pageSize === "all"/);
  assert.match(dashboard, /批量操作仅作用于当前已载入记录|infinite/);
  assert.doesNotMatch(dashboardImport, /entries\/\?page_size=all/);
  assert.match(dashboardImport, /refreshEntries\(\)/);
});
