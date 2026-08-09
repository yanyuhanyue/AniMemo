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
  assert.match(editor, /api\.get\(`entries\/\$\{draft\.id\}\/watch-history\/`\)/);
  assert.match(showcase, /watchHistory:\s*\[\]/);
  assert.match(showcase, /watchHistory:\s*record\.watchHistory\s*\|\|\s*\[\]/);
});

test("edit and read-only anime details expose watch-history controls", () => {
  assert.match(editor, /观看情况/);
  assert.match(editor, /toggleHistoryModule/);
  assert.match(editor, /const opening = module !== "history"/);
  assert.match(editor, /if \(opening\) loadHistory\(\)/);
  assert.match(featuredModal, /activeTab === "history"/);
  assert.match(featuredModal, /观看情况/);
});

test("editable watch-history panel can add and remove manual records", () => {
  assert.match(editor, /添加观看记录/);
  assert.match(editor, /onRemove/);
  assert.match(editor, /api\.post\(`entries\/\$\{draft\.id\}\/watch-history\/`/);
  assert.match(editor, /api\.delete\(`entries\/\$\{draft\.id\}\/watch-history\/\$\{record\.id\}\/`/);
});
