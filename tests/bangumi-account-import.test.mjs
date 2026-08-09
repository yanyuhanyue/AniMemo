import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { buildImportItems, importResultCount, initialImportAction } from "../src/lib/externalAccounts.js";

const accountPanel = readFileSync(new URL("../src/components/dashboard/ExternalAccountPanel.jsx", import.meta.url), "utf8");
const importDialog = readFileSync(new URL("../src/components/dashboard/BangumiImportDialog.jsx", import.meta.url), "utf8");
const profileDialog = readFileSync(new URL("../src/pages/DashboardDialogs.jsx", import.meta.url), "utf8");
const dashboard = readFileSync(new URL("../src/pages/DashboardPage.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("all rows require explicit selection and possible matches default to skip", () => {
  assert.deepEqual(initialImportAction({ match_state: "already_bound" }), {
    selected: false,
    mode: "SKIP",
    local_entry_id: "",
    apply_fields: [],
  });
  assert.deepEqual(initialImportAction({ match_state: "possible_local_match", possible_local_matches: [{ id: 42 }] }), {
    selected: false,
    mode: "SKIP",
    local_entry_id: 42,
    apply_fields: [],
  });
  assert.deepEqual(initialImportAction({ match_state: "unbound" }), {
    selected: false,
    mode: "CREATE_NEW",
    local_entry_id: "",
    apply_fields: [],
  });
});

test("apply request sends only identity, mode, target, and explicit conflict choices", () => {
  const items = buildImportItems({
    100: { selected: true, mode: "CREATE_NEW", apply_fields: [] },
    200: { selected: true, mode: "BIND_EXISTING", local_entry_id: 8, apply_fields: ["watch_status"] },
    300: { selected: false, mode: "CREATE_NEW", apply_fields: [] },
    400: { selected: true, mode: "SKIP", apply_fields: [] },
  });
  assert.deepEqual(items, [
    { external_id: "100", mode: "CREATE_NEW", local_entry_id: null, apply_fields: [] },
    { external_id: "200", mode: "BIND_EXISTING", local_entry_id: 8, apply_fields: ["watch_status"] },
  ]);
  assert.equal(importResultCount({ 100: { selected: true, mode: "CREATE_NEW" }, 200: { selected: true, mode: "SKIP" } }), 1);
  assert.doesNotMatch(JSON.stringify(items), /title|comment|rating|remote_status/);
});

test("account settings exposes OAuth, password token input, verify, import, and guarded disconnect", () => {
  assert.match(profileDialog, /dashboard-external-tab/);
  assert.match(accountPanel, /type="password"/);
  assert.match(accountPanel, /external-accounts\/bangumi\/authorize\//);
  assert.match(accountPanel, /external-accounts\/bangumi\/verify\//);
  assert.match(accountPanel, /断开后不会删除已导入的番剧、评分、评论或外部作品绑定/);
  assert.match(accountPanel, /Bangumi 账号连接暂不可用/);
  assert.match(accountPanel, /setToken\(""\)/);
  assert.doesNotMatch(accountPanel, /localStorage|sessionStorage/);
});

test("import dialog supports preview filters, modes, conflicts, pagination, and partial result summary", () => {
  for (const label of ["全部", "想看", "在看", "看过", "冲突", "已存在", "新建记录", "绑定到本地", "导入选定字段", "确认导入", "失败"]) {
    assert.match(importDialog, new RegExp(label));
  }
  assert.match(importDialog, /import-preview\/\$\{id\}/);
  assert.match(importDialog, /apply_fields/);
  assert.match(importDialog, /aria-modal="true"/);
});

test("OAuth callback query is consumed and removed without browser credential storage", () => {
  assert.match(dashboard, /params\.get\("external_account_status"\)/);
  assert.match(dashboard, /params\.get\("external_account_provider"\)/);
  assert.doesNotMatch(dashboard, /params\.get\("bangumi"\)/);
  assert.match(dashboard, /history\.replaceState/);
  assert.doesNotMatch(dashboard, /bangumi.*localStorage|localStorage.*bangumi/i);
});

test("mobile import rows collapse to cards without horizontal page overflow", () => {
  assert.match(styles, /@media \(max-width: 680px\)[\s\S]*\.bangumi-import-row \{ grid-template-columns: 25px 50px minmax\(0, 1fr\)/);
  assert.match(styles, /\.bangumi-import-dialog \{ width: 100%; height: calc\(100dvh - 16px\)/);
});
