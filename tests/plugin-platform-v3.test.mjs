import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const page = readFileSync(new URL("../src/pages/PluginPlatformPage.jsx", import.meta.url), "utf8");
const admin = readFileSync(new URL("../src/components/admin/PluginManagementPanel.jsx", import.meta.url), "utf8");
const routes = readFileSync(new URL("../backend/journal/urls.py", import.meta.url), "utf8");

test("plugin platform exposes Marketplace, Installed and My Plugins workflows", () => {
  assert.match(page, /插件市场/);
  assert.match(page, /已安装/);
  assert.match(page, /我的插件/);
  assert.match(page, /plugins\/marketplace\/\$\{plugin\.slug\}\/install/);
  assert.match(page, /plugins\/my\/\$\{project\.id\}\/versions/);
  assert.match(page, /versionAction\(version, "preview"\)/);
  assert.match(page, /versionAction\(version, "submit"\)/);
});

test("administrator plugin panel exposes review and publish as separate operations", () => {
  assert.match(admin, /staff\/plugins\/review/);
  assert.match(admin, /approve/);
  assert.match(admin, /staff\/plugins\/versions\/\$\{version\.id\}\/publish/);
});

test("host routes keep user installation and staff review endpoints separate", () => {
  assert.match(routes, /plugin-marketplace-installation/);
  assert.match(routes, /staff-plugin-review-queue/);
  assert.match(routes, /staff-plugin-publish/);
  assert.match(routes, /staff-plugin-revoke/);
});
