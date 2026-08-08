import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const dashboard = readFileSync(new URL("../src/pages/DashboardPage.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("profile save uses the dedicated reference-style notification", () => {
  assert.match(dashboard, /flash\("个人资料已更新。",\s*"profile"\)/);
  assert.match(dashboard, /dashboard-profile-toast__icon/);
  assert.match(dashboard, /dashboard-profile-toast__close/);
  assert.match(styles, /\.dashboard-profile-toast\s*\{/);
  assert.match(styles, /background:\s*#4ecdc4/);
  assert.match(styles, /\.dashboard-profile-toast\s*\{[^}]*right:\s*24px[^}]*bottom:\s*24px/);
  assert.doesNotMatch(styles, /\.dashboard-profile-toast\s*\{[^}]*top:/);
});
