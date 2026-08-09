import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const dashboardPage = readFileSync(new URL("../src/pages/DashboardPage.jsx", import.meta.url), "utf8");
const dashboardData = readFileSync(new URL("../src/pages/dashboardData.js", import.meta.url), "utf8");
const dashboard = `${dashboardPage}\n${dashboardData}`;
const apiAdapter = dashboardData.slice(
  dashboardData.indexOf("export function apiToRecord"),
  dashboardData.indexOf("export function recordToApi"),
);
const showcase = readFileSync(new URL("../src/pages/ShowcasePage.jsx", import.meta.url), "utf8");
const editor = readFileSync(new URL("../src/components/dashboard/EditAnimeRecordContent.jsx", import.meta.url), "utf8");
const featuredModal = readFileSync(new URL("../src/components/featured/FeaturedAnimeModal.jsx", import.meta.url), "utf8");

test("entry DTOs keep only history summary and load full history lazily", () => {
  assert.match(apiAdapter, /watchHistory:\s*\[\]/);
  assert.match(apiAdapter, /watchHistoryCount:\s*Number\(item\.watch_history_count/);
  assert.doesNotMatch(apiAdapter, /item\.watch_history\s*\|\|/);
  assert.match(editor, /api\.get\(`entries\/\$\{draft\.id\}\/watch-history\/`, \{ params: \{ page, page_size: 100 \} \}\)/);
  assert.match(showcase, /watchHistory:\s*\[\]/);
  assert.match(showcase, /watchHistory:\s*record\.watchHistory\s*\|\|\s*\[\]/);
});

test("entry hub and read-only anime details expose watch-history controls", () => {
  assert.match(editor, /ENTRY HUB \/ 作品中心/);
  assert.match(editor, /\["overview", "概览", "list"\]/);
  assert.match(editor, /\["history", "观看记录", "history"\]/);
  assert.match(editor, /\["statistics", "统计", "chart"\]/);
  assert.match(editor, /\["external", "外部资料", "link"\]/);
  assert.match(editor, /toggleHistoryModule/);
  assert.match(editor, /const opening = hubTab !== "history"/);
  assert.match(editor, /role="tablist" aria-label="作品中心"/);
  assert.match(featuredModal, /activeTab === "history"/);
  assert.match(featuredModal, /观看情况/);
});

test("editable watch-history panel can add, edit, remove, and paginate records", () => {
  assert.match(editor, /记录观看/);
  assert.match(editor, /onEdit=/);
  assert.match(editor, /onRemove/);
  assert.match(editor, /api\.post\(`entries\/\$\{draft\.id\}\/watch-history\/`/);
  assert.match(editor, /api\.patch\(`entries\/\$\{draft\.id\}\/watch-history\/\$\{editing\.id\}\/`/);
  assert.match(editor, /api\.delete\(`entries\/\$\{draft\.id\}\/watch-history\/\$\{record\.id\}\/`/);
  assert.match(editor, /historyNextPage/);
});
