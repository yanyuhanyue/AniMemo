import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const page = readFileSync(new URL("../plugins/watch-history-importer/frontend/WatchHistoryImporterPage.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("../plugins/watch-history-importer/frontend/styles.css", import.meta.url), "utf8");
const bundle = readFileSync(new URL("../plugins/watch-history-importer/frontend/plugin.js", import.meta.url), "utf8");

test("watch-history review can confirm the current Bangumi candidate directly", () => {
  assert.match(page, /Number\(subjectDrafts\[groupIndex\] \?\? fallbackBangumiId\)/);
  assert.match(page, /subjectDrafts\[group\.index\] \?\? String\(group\.resolution\?\.bangumi_id \|\| ""\)/);
  assert.match(page, /selectSubject\(group\.index, group\.resolution\?\.bangumi_id\)/);
  assert.match(page, /"直接确认"/);
});

test("Bangumi candidates expose an accessible external title link", () => {
  assert.match(page, /href=\{resolution\.source_url\}/);
  assert.match(page, /target="_blank"/);
  assert.match(page, /rel="noreferrer"/);
  assert.match(page, /aria-label=\{`在 Bangumi 查看/);
  assert.match(styles, /\.ajp-watch-import__bangumi-link:focus-visible/);
});

test("watch-history preview supports per-item and filtered bulk exclusion", () => {
  assert.match(page, /excludedGroupIndices/);
  assert.match(page, /type="checkbox"/);
  assert.match(page, /当前结果全部导入/);
  assert.match(page, /当前结果全部排除/);
  assert.match(page, /excluded_group_indices/);
  assert.match(styles, /\.ajp-watch-import__selection/);
  assert.match(styles, /\.ajp-watch-import__preview-row\.is-excluded/);
});

test("watch-history stays inside the Plugin SDK v2 boundary", () => {
  assert.match(page, /host\?\.auth\?\.isAuthenticated\(\)/);
  assert.doesNotMatch(page, /host\.auth\.isStaff\(\)/);
  assert.doesNotMatch(page, /target_user_id|导入目标账号/);
  assert.match(page, /const client = pluginApi/);
  assert.doesNotMatch(page, /\.\.\/\.\.\/\.\.\/src\/|getStoredTokens|src\/lib\/api\.js/);
  assert.doesNotMatch(bundle, /getStoredTokens|let accessToken|accessToken =|import\.meta\.env|VITE_API_BASE_URL|VITE_API_URL|src\/lib\/api\.js/);
});
