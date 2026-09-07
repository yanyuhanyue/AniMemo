import assert from "node:assert/strict";
import test from "node:test";

import { AUTH_ENDPOINTS } from "../src/lib/apiCore.js";
import { createAuthSession } from "../src/lib/authSession.js";
import { createWebApiTransport } from "../src/lib/webApiTransport.js";
import { createWebAuthAdapter } from "../src/lib/webAuthAdapter.js";

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

const userA = { id: 1, username: "A" };
const userB = { id: 2, username: "B" };
const response = (config, data) => ({ config, data, headers: {}, status: 200, statusText: "OK" });

for (const loginMethod of ["login", "staffLogin"]) {
  test(`${loginMethod} commit invalidates requests started while the login was in progress`, async () => {
    const session = createAuthSession();
    session.store({ access: "synthetic-a", user: userA });
    const transport = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session });
    const adapter = createWebAuthAdapter({ session, api: transport.api, cookieClient: transport.cookieClient });
    const loginStarted = deferred();
    const loginResponse = deferred();
    transport.cookieClient.defaults.adapter = async (config) => {
      if (config.url === AUTH_ENDPOINTS.csrf) return response(config, { csrf_token: "synthetic-csrf" });
      loginStarted.resolve();
      await loginResponse.promise;
      return response(config, { access: "synthetic-b", user: userB });
    };
    const privateResponse = deferred();
    transport.api.defaults.adapter = async (config) => {
      await privateResponse.promise;
      return response(config, { private: config.headers.Authorization === "Bearer synthetic-a" ? "A" : "B" });
    };
    const login = adapter.authApi[loginMethod]("B", "synthetic-password");
    await loginStarted.promise;
    const prior = transport.api.get("entries/").then((value) => ({ value }), (error) => ({ error }));
    loginResponse.resolve();
    assert.equal(adapter.storeTokens((await login).data), true);
    privateResponse.resolve();
    const result = await prior;
    assert.equal(result.value, undefined);
    assert.equal(result.error?.code, "AUTH_SESSION_CHANGED");
    assert.deepEqual(session.getUser(), userB);
  });
}

for (const phase of ["in flight", "returned but not stored"]) {
  test(`a login ${phase} excludes competing cookie mutations and refresh`, async () => {
    const session = createAuthSession();
    session.store({ access: "synthetic-a", user: userA });
    const transport = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session });
    const adapter = createWebAuthAdapter({ session, api: transport.api, cookieClient: transport.cookieClient });
    const started = deferred();
    const finish = deferred();
    const dispatched = [];
    transport.cookieClient.defaults.adapter = async (config) => {
      dispatched.push(config.url);
      if (config.url === AUTH_ENDPOINTS.csrf) return response(config, { csrf_token: "synthetic-csrf" });
      if (config.url === AUTH_ENDPOINTS.refresh) return response(config, { access: "synthetic-b", user: userB });
      started.resolve();
      await finish.promise;
      return response(config, { access: "synthetic-b", user: userB });
    };
    transport.api.defaults.adapter = async (config) => {
      dispatched.push(config.url);
      return response(config, {});
    };
    const login = adapter.authApi.login("B", "synthetic-password");
    await started.promise;
    if (phase === "returned but not stored") { finish.resolve(); await login; }
    const requestCount = dispatched.length;
    for (const operation of [
      () => adapter.refreshAccessToken(),
      () => adapter.authApi.registerRequest("synthetic@example.test"),
      () => adapter.authApi.changePassword({ password: "synthetic" }),
      () => adapter.authApi.deleteAccount({ current_password: "synthetic" }),
      () => adapter.csrfApi.post("protected/", {}),
      () => transport.api.post("staff/security/two-factor/", { action: "regenerate" }),
      () => transport.api.patch("entries/17/", { review: "prior identity" }),
    ]) {
      await assert.rejects(Promise.resolve().then(operation), { code: "AUTH_SESSION_CHANGED" });
    }
    assert.equal(dispatched.length, requestCount, "A pending identity change excludes all Bearer calls, including cookie rotations outside authApi");
    assert.deepEqual(session.getUser(), userA);
    finish.resolve();
    assert.equal(adapter.storeTokens((await login).data), true);
    assert.equal(await adapter.refreshAccessToken(), "synthetic-b");
    assert.deepEqual(session.getUser(), userB);
  });
}

test("a failed current login releases its pending state and clears its own account snapshot", async () => {
  const session = createAuthSession();
  session.store({ access: "synthetic-a", user: userA });
  const transport = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session });
  const adapter = createWebAuthAdapter({ session, api: transport.api, cookieClient: transport.cookieClient });
  transport.cookieClient.defaults.adapter = async (config) => {
    if (config.url === AUTH_ENDPOINTS.csrf) return response(config, { csrf_token: "synthetic-csrf" });
    if (config.url === AUTH_ENDPOINTS.refresh) return response(config, { access: "refreshed-a", user: userA });
    throw Object.assign(new Error("synthetic invalid credentials"), { config, response: { status: 401 } });
  };
  await assert.rejects(adapter.authApi.login("B", "synthetic-password"), /synthetic invalid credentials/);
  assert.equal(session.getUser(), null);
  assert.equal(await adapter.refreshAccessToken(), "refreshed-a");
});

test("login cookie success followed by CSRF failure cannot mix the old account with a later refresh", async () => {
  const session = createAuthSession();
  session.store({ access: "synthetic-a", user: userA });
  const transport = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session });
  const adapter = createWebAuthAdapter({ session, api: transport.api, cookieClient: transport.cookieClient });
  const postStarted = deferred();
  const postResponse = deferred();
  const privateResponse = deferred();
  let csrfCalls = 0;
  transport.cookieClient.defaults.adapter = async (config) => {
    if (config.url === AUTH_ENDPOINTS.csrf) {
      csrfCalls += 1;
      if (csrfCalls === 2) throw Object.assign(new Error("synthetic post-login CSRF failure"), { config });
      return response(config, { csrf_token: "synthetic-csrf-b" });
    }
    if (config.url === AUTH_ENDPOINTS.refresh) return response(config, { access: "synthetic-b", user: userB });
    postStarted.resolve();
    await postResponse.promise;
    return response(config, { access: "synthetic-b", user: userB });
  };
  transport.api.defaults.adapter = async (config) => {
    await privateResponse.promise;
    return response(config, { private: "A" });
  };
  const login = adapter.authApi.staffLogin("B", "synthetic-password").catch((error) => error);
  await postStarted.promise;
  const old = transport.api.get("entries/").then((value) => ({ value }), (error) => ({ error }));
  postResponse.resolve();
  assert.match((await login).message, /post-login CSRF failure/);
  const userAfterFailedLogin = session.getUser();
  assert.equal(await adapter.refreshAccessToken(), "synthetic-b");
  privateResponse.resolve();
  const result = await old;
  assert.equal(result.value, undefined);
  assert.equal(result.error?.code, "AUTH_SESSION_CHANGED");
  assert.equal(userAfterFailedLogin, null);
  assert.deepEqual(session.getUser(), userB);
});

test("an old login's failure cannot release a newer login's cookie exclusion", async () => {
  const session = createAuthSession();
  const transport = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session });
  const adapter = createWebAuthAdapter({ session, api: transport.api, cookieClient: transport.cookieClient });
  const started = [deferred(), deferred()];
  const finishes = [deferred(), deferred()];
  let loginCount = 0;
  transport.cookieClient.defaults.adapter = async (config) => {
    if (config.url === AUTH_ENDPOINTS.csrf) return response(config, { csrf_token: "synthetic-csrf" });
    const index = loginCount++;
    started[index].resolve();
    await finishes[index].promise;
    return response(config, { access: index ? "synthetic-b" : "synthetic-a", user: index ? userB : userA });
  };
  const old = adapter.authApi.login("A", "synthetic-password").catch((error) => error);
  await started[0].promise;
  const current = adapter.authApi.login("B", "synthetic-password");
  await started[1].promise;
  finishes[0].resolve();
  assert.equal((await old).code, "AUTH_SESSION_CHANGED");
  await assert.rejects(Promise.resolve().then(() => adapter.refreshAccessToken()), { code: "AUTH_SESSION_CHANGED" });
  assert.equal(loginCount, 2);
  finishes[1].resolve();
  assert.equal(adapter.storeTokens((await current).data), true);
  assert.deepEqual(session.getUser(), userB);
});
