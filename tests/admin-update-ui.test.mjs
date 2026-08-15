import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { compatibilityPresentation, updateStateLabel } from "../src/components/admin/updatePresentation.js";

const component = readFileSync(new URL("../src/components/admin/AdminUpdatePanel.jsx", import.meta.url), "utf8");
const page = readFileSync(new URL("../src/pages/AdminDashboardPage.jsx", import.meta.url), "utf8");
const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");

test("staff update surface preserves Stable default and gates prerelease channels to superusers", () => {
  assert.match(component, /useState\("stable"\)/);
  assert.match(component, /viewer\.is_superuser \? \["rc", "beta"\]/);
  assert.match(component, /Beta 是开发验证通道/);
  assert.match(page, /\["updates", "系统更新", "history", "manage_system"\]/);
});

test("update confirmation, rollback, and progress come from real Agent endpoints", () => {
  assert.match(component, /csrfApi\.post\("staff\/system\/updates\/plan\//);
  assert.match(component, /csrfApi\.post\("staff\/system\/updates\/apply\//);
  assert.match(component, /csrfApi\.post\("staff\/system\/updates\/rollback\//);
  assert.match(component, /api\.get\(`staff\/system\/updates\/operations\/\$\{operation\.id\}\//);
  assert.match(component, /APPLY \{selectedPlanVersion\}/);
  assert.match(component, /ROLLBACK PREVIOUS/);
  assert.match(component, /数据库自动回退/);
  assert.match(component, /永不执行/);
});

test("unsafe downgrade and manual recovery have explicit user-facing states", () => {
  assert.equal(compatibilityPresentation({ allowed: false }).label, "不可切换");
  assert.equal(compatibilityPresentation({ decision: "application_rollback", allowed: true }).label, "应用层可回退");
  assert.equal(updateStateLabel("manual_recovery_required"), "需要人工恢复");
  assert.match(component, /release\.compatibility\?\.allowed === false/);
  assert.match(component, /status\?\.previousCompatibility/);
  assert.match(component, /previousCompatibility\?\.allowed === false/);
  assert.match(component, /status\?\.recoveryBlock/);
  assert.match(component, /recoveryBlocked/);
  assert.match(component, /服务器管理员完成现场对账/);
});

test("staff dashboard does not expose the raw Django admin shortcut", () => {
  assert.doesNotMatch(page, /Django 高级后台/);
  assert.doesNotMatch(page, /\badminUrl\b/);
});

test("frontend identifies its immutable artifact separately from effective updater state", () => {
  assert.match(html, /animemo-artifact-version/);
  assert.match(html, /%VITE_ANIMEMO_VERSION%/);
  assert.match(html, /animemo-artifact-commit/);
  assert.match(html, /animemo-artifact-channel/);
  assert.match(component, /status\?\.current/);
});
