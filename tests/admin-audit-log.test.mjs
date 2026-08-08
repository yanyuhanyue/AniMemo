import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const controls = [
  "../src/components/admin/AdminResourcePanel.jsx",
  "../src/components/admin/AdminControlDialogs.jsx",
  "../src/components/admin/adminControlUtils.js",
].map((path) => readFileSync(new URL(path, import.meta.url), "utf8")).join("\n");
const staffViews = readFileSync(new URL("../backend/journal/staff_resource_views.py", import.meta.url), "utf8");

test("audit records use a dedicated readable list without account status fallbacks", () => {
  assert.match(controls, /kind === "audit" \? <AuditLogList entries=\{pageData\.results\}/);
  assert.match(controls, /function AuditLogList\(\{ entries, onOpen \}\)/);
  assert.match(controls, /管理员/);
  assert.match(controls, /操作对象/);
  assert.match(controls, /时间与来源/);
  assert.doesNotMatch(controls, /kind === "audit" && <button[^>]+>.*差异/);
});

test("audit actions and field changes are translated for operators", () => {
  assert.match(controls, /"plugin\.upgrade": \["升级插件", "pink"\]/);
  assert.match(controls, /"user\.permissions": \["修改用户权限", "coral"\]/);
  assert.match(controls, /site_name: "站点名称"/);
  assert.match(controls, /registration_enabled: "开放用户注册"/);
  assert.match(controls, /email_sender_address: "发件邮箱"/);
  assert.match(controls, /function AuditDiff\(\{ before, after \}\)/);
  assert.match(controls, /\.filter\(\(key\) => JSON\.stringify\(previous\[key\]\) !== JSON\.stringify\(current\[key\]\)\)/);
  assert.match(controls, /字段修改差异/);
  assert.match(controls, /执行管理员/);
});

test("audit API includes request device information", () => {
  assert.match(staffViews, /"user_agent": item\.user_agent/);
});
