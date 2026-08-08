import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const controls = readFileSync(new URL("../src/components/admin/AdminSystemPanel.jsx", import.meta.url), "utf8");

test("admin data exports require confirmation before requesting a file", () => {
  assert.match(controls, /const askExport = \(format, kind = "all"\) =>/);
  assert.match(controls, /setExportConfirmation\(\{/);
  assert.match(controls, /confirmLabel: "确认导出"/);
  assert.match(controls, /onConfirm: \(\) => exportData\(format, kind\)/);
  assert.match(controls, /onClick=\{\(\) => askExport\("zip"\)\}/);
  assert.match(controls, /onClick=\{\(\) => askExport\("csv", kind\)\}/);
  assert.doesNotMatch(controls, /onClick=\{\(\) => exportData\("(?:zip|csv)"/);
});

test("admin export buttons use explicit export labels", () => {
  assert.match(controls, />导出完整安全备份</);
  assert.match(controls, />导出 \{\(\{ users: "用户", entries: "番剧记录", columns: "专栏", audit: "审计日志" \}\)\[kind\]\} CSV</);
});
