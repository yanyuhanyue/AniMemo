import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const panel = readFileSync(new URL("../src/components/admin/AdminExternalServicesPanel.jsx", import.meta.url), "utf8");
const dashboard = readFileSync(new URL("../src/pages/AdminDashboardPage.jsx", import.meta.url), "utf8");

test("staff navigation exposes focused external service configuration", () => {
  assert.match(dashboard, /\["services", "外部服务", "link", "manage_system"\]/);
  assert.match(dashboard, /<AdminExternalServicesPanel onNotice=\{flash\} onError=\{setError\}/);
});

test("Bangumi provider UI follows the masked provider API contract", () => {
  assert.match(panel, /api\.get\("staff\/external-providers\/bangumi\/"\)/);
  assert.match(panel, /api\.patch\("staff\/external-providers\/bangumi\/", body\)/);
  assert.match(panel, /api\.delete\("staff\/external-providers\/bangumi\/client-secret\/"\)/);
  assert.match(panel, /type="password"/);
  assert.match(panel, /autoComplete="new-password"/);
  assert.doesNotMatch(panel, /client_secret\s*[:=]\s*provider\./);
  assert.doesNotMatch(panel, /masked|ciphertext|encrypted_client_secret/i);
});

test("callback remains read-only and secret clearing only targets the database override", () => {
  assert.match(panel, /readOnly value=\{provider\.oauth_callback/);
  assert.match(panel, /provider\.client_secret_source !== "database"/);
  assert.match(panel, /body\.client_secret = clientSecret\.trim\(\)/);
  assert.match(panel, /if \(enabledDirty\) body\.enabled = enabled/);
  assert.match(panel, /setClientSecret\(""\)/);
});
