import assert from "node:assert/strict";
import test from "node:test";

import { createAuthSession } from "../src/lib/authSession.js";
import { createWebAuthAdapter } from "../src/lib/webAuthAdapter.js";

const legacyKeys = ["anime_journal_access", "anime_journal_refresh"];

for (const unavailableStorage of ["localStorage", "sessionStorage"]) {
  test(`auth starts and clears memory when ${unavailableStorage} cannot be read`, () => {
    const removed = [];
    const browser = {};
    for (const name of ["localStorage", "sessionStorage"]) {
      Object.defineProperty(browser, name, {
        get() {
          if (name === unavailableStorage) throw new DOMException("Storage disabled", "SecurityError");
          return {
            getItem() { assert.fail("Legacy credentials must never be accepted"); },
            removeItem(key) { removed.push([name, key]); },
          };
        },
      });
    }
    const session = createAuthSession();
    const adapter = createWebAuthAdapter({ session, browser });
    adapter.storeTokens({ access: "current", user: { id: 1 } });
    assert.deepEqual(adapter.getStoredTokens(), { access: "current", refresh: null });
    adapter.clearTokens();
    assert.equal(session.getAccessToken(), null);
    assert.equal(session.getUser(), null);
    const availableStorage = unavailableStorage === "localStorage" ? "sessionStorage" : "localStorage";
    assert.deepEqual(removed, Array.from({ length: 3 }, () => legacyKeys.map((key) => [availableStorage, key])).flat());
  });
}

for (const failedStorage of ["localStorage", "sessionStorage"]) {
  for (const failedKey of legacyKeys) {
    test(`legacy cleanup continues after ${failedStorage} refuses ${failedKey}`, () => {
      const removed = [];
      const browser = Object.fromEntries(["localStorage", "sessionStorage"].map((name) => [name, {
        getItem() { assert.fail("Legacy credentials must never be accepted"); },
        removeItem(key) {
          removed.push([name, key]);
          if (name === failedStorage && key === failedKey) throw new DOMException("Storage disabled", "SecurityError");
        },
      }]));
      const session = createAuthSession();
      const notifications = [];
      session.subscribe((snapshot) => notifications.push(snapshot));
      const adapter = createWebAuthAdapter({ session, browser });
      adapter.storeTokens({ access: "current", user: { id: 1 } });
      adapter.clearTokens();
      assert.equal(session.getAccessToken(), null);
      assert.equal(session.getUser(), null);
      assert.equal(notifications.at(-1).user, null);
      assert.deepEqual(removed, Array.from({ length: 3 }, () => ["localStorage", "sessionStorage"].flatMap((name) => legacyKeys.map((key) => [name, key]))).flat());
    });
  }
}

test("auth remains usable when both browser storages are unavailable", () => {
  const browser = {};
  for (const name of ["localStorage", "sessionStorage"]) {
    Object.defineProperty(browser, name, { get() { throw new DOMException("Storage disabled", "SecurityError"); } });
  }
  const session = createAuthSession();
  const adapter = createWebAuthAdapter({ session, browser });
  adapter.storeTokens({ access: "memory-only", user: { id: 1 } });
  assert.equal(session.getAccessToken(), "memory-only");
  adapter.clearTokens();
  assert.deepEqual(adapter.getStoredTokens(), { access: null, refresh: null });
});
