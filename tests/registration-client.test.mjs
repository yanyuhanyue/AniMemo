import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const authPage = readFileSync(new URL("../src/pages/UserAuthPage.jsx", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("../src/lib/api.js", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");

test("registration UI has email-only request and verified completion stages", () => {
  assert.match(authPage, /authApi\.registerRequest\(form\.email\.trim\(\), turnstileToken\)/);
  assert.match(authPage, /authApi\.verifyRegistration\(registrationToken\)/);
  assert.match(authPage, /authApi\.completeRegistration\(/);
  assert.match(authPage, /completionToken/);
  assert.match(authPage, /readOnly=\{mode === "registerComplete"\}/);
  assert.doesNotMatch(authPage, /authApi\.register\(\{\s*email: form\.email\.trim\(\),\s*password:/);
});

test("registration tokens use the CSRF cookie client and routes are exposed", () => {
  assert.match(apiSource, /registerRequest: \(email, turnstileToken = ""\) => cookiePost\("auth\/register\/request\//);
  assert.match(apiSource, /verifyRegistration: \(token\) => cookiePost\("auth\/register\/verify\//);
  assert.match(apiSource, /completeRegistration: \(payload, turnstileToken = ""\) => cookiePost\("auth\/register\/complete\//);
  assert.match(appSource, /path="\/register" element=\{<UserAuthPage \/>\}/);
  assert.match(appSource, /path="\/register\/verify" element=\{<UserAuthPage \/>\}/);
});

test("completion token is not persisted to browser storage", () => {
  assert.doesNotMatch(authPage, /localStorage\.(setItem|getItem).*completion/i);
  assert.doesNotMatch(authPage, /sessionStorage\.(setItem|getItem).*completion/i);
});
