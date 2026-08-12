import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";


const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const setupPageSource = readFileSync(new URL("../src/pages/SetupPage.jsx", import.meta.url), "utf8");
const adminLoginSource = readFileSync(new URL("../src/pages/AdminLoginPage.jsx", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("../src/lib/webAuthAdapter.js", import.meta.url), "utf8");
const gitignoreSource = readFileSync(new URL("../.gitignore", import.meta.url), "utf8");
const browserHarnessSources = [
  "auth-field-focus-e2e.mjs",
  "dashboard-initial-request-e2e.mjs",
  "dashboard-mutation-e2e.mjs",
  "performance-frontend-e2e.mjs",
].map((filename) => [filename, readFileSync(new URL(filename, import.meta.url), "utf8")]);


test("uninitialized installations are gated to the browser first-run route", () => {
  assert.match(appSource, /setupApi\.status\(\)/);
  assert.match(appSource, /installation\?\.state !== "initialized"/);
  assert.match(appSource, /setInstallation\(\{ state: "unavailable", accepting_setup: false \}\)/);
  assert.match(appSource, /path="\/setup"/);
  assert.match(appSource, /<Navigate to="\/setup" replace \/>/);
});

test("setup page collects only the one-time code and required first-admin fields", () => {
  for (const field of ["code", "username", "email", "password", "passwordConfirm"]) {
    assert.match(setupPageSource, new RegExp(`form\\.${field}`));
  }
  assert.match(setupPageSource, /setupApi\.complete\(/);
  assert.match(setupPageSource, /password_confirm: form\.passwordConfirm/);
  assert.match(setupPageSource, /onInitialized\?\.\(\)/);
  assert.doesNotMatch(setupPageSource, /localStorage|sessionStorage/);
  assert.doesNotMatch(setupPageSource, /setup_code_hash|data\?\.code/);
  assert.match(apiSource, /complete: \(payload\) => cookiePost\(INSTALLATION_ENDPOINTS\.complete, payload\)/);
});

test("successful setup hands an explicit completion message to staff login", () => {
  assert.match(appSource, /state=\{\{ message: "初始化已完成，请登录管理员账号。" \}\}/);
  assert.match(adminLoginSource, /location\.state\?\.message/);
  assert.match(adminLoginSource, /form-message success/);
});

test("local first-run private state cannot be added to Git accidentally", () => {
  assert.match(gitignoreSource, /^\/runtime\/private\/$/m);
});

test("browser harnesses declare an initialized installation instead of bypassing the gate", () => {
  for (const [filename, source] of browserHarnessSources) {
    assert.match(source, /path === "setup\/status\/"[\s\S]*state: "initialized"/, filename);
  }
});
