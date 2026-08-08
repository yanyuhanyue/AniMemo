import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const dashboard = [
  "../src/pages/DashboardPage.jsx",
  "../src/pages/dashboardData.js",
].map((path) => readFileSync(new URL(path, import.meta.url), "utf8")).join("\n");
const showcase = readFileSync(new URL("../src/pages/ShowcasePage.jsx", import.meta.url), "utf8");
const editor = readFileSync(new URL("../src/components/dashboard/EditAnimeRecordContent.jsx", import.meta.url), "utf8");
const featuredModal = readFileSync(new URL("../src/components/featured/FeaturedAnimeModal.jsx", import.meta.url), "utf8");

test("catalog records retain imported watch history", () => {
  assert.match(dashboard, /watchHistory:\s*(?:item\.watchHistory\s*\|\|\s*)?item\.watch_history\s*\|\|\s*\[\]/);
  assert.match(showcase, /watchHistory:\s*item\.watch_history\s*\|\|\s*\[\]/);
  assert.match(showcase, /watchHistory:\s*record\.watchHistory\s*\|\|\s*\[\]/);
});

test("edit and read-only anime details expose watch-history controls", () => {
  assert.match(editor, /观看情况/);
  assert.match(editor, /toggleHistoryModule/);
  assert.match(editor, /current === "history"\s*\?\s*previousModuleRef\.current\s*:\s*"history"/);
  assert.match(featuredModal, /activeTab === "history"/);
  assert.match(featuredModal, /观看情况/);
});

test("editable watch-history panel can add and remove manual records", () => {
  assert.match(editor, /添加观看记录/);
  assert.match(editor, /onRemove/);
  assert.match(editor, /watchHistory:/);
  assert.match(dashboard, /watch_history:/);
});
