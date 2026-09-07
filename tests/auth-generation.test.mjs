import assert from "node:assert/strict";
import test from "node:test";

import { AUTH_ENDPOINTS } from "../src/lib/apiCore.js";
import { createAuthSession } from "../src/lib/authSession.js";
import { createWebAuthAdapter } from "../src/lib/webAuthAdapter.js";
import { createWebApiTransport } from "../src/lib/webApiTransport.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const userA = { id: 1, username: "member-a", is_staff: true };
const userB = { id: 2, username: "member-b", is_staff: false };
const responseFor = (user, access = `access-${user.id}`) => ({ data: { access, user } });
const afterMicrotasks = () => new Promise((done) => setImmediate(done));

function harness({ post, get, csrf } = {}) {
  const session = createAuthSession();
  const adapter = createWebAuthAdapter({
    session,
    api: { get: get || (async () => ({ data: { username: "member-a" } })) },
    cookieClient: {
      get: csrf || (async () => ({ data: { csrf_token: "synthetic-csrf" } })),
      post: post || (async () => responseFor(userA)),
    },
  });
  return { adapter, session };
}

for (const outcome of ["success", "failure"]) {
  for (const transition of ["logout", "account switch"]) {
    test(`late refresh ${outcome} cannot undo ${transition}`, async () => {
      const refresh = deferred();
      const started = deferred();
      const { adapter, session } = harness({ post: (path) => {
        if (path !== AUTH_ENDPOINTS.refresh) return Promise.resolve({ data: {} });
        started.resolve();
        return refresh.promise;
      } });
      adapter.storeTokens({ access: "access-a", user: userA });
      const snapshots = [];
      session.subscribe((snapshot) => snapshots.push(snapshot));
      const pending = adapter.refreshAccessToken().catch(() => null);
      await started.promise;
      if (transition === "logout") await adapter.authApi.logout();
      else adapter.storeTokens({ access: "access-b", user: userB });
      const notificationCount = snapshots.length;
      if (outcome === "success") refresh.resolve(responseFor(userA));
      else refresh.reject(new Error("old refresh failed"));
      await pending;
      assert.equal(session.getAccessToken(), transition === "logout" ? null : "access-b");
      assert.deepEqual(session.getUser(), transition === "logout" ? null : userB);
      assert.equal(snapshots.length, notificationCount, "A stale callback must not notify the new session");
    });
  }
}

test("old refresh finally cannot clear the new generation's shared request", async () => {
  const requests = [];
  const { adapter, session } = harness({ post: () => {
    const request = deferred();
    requests.push(request);
    return request.promise;
  } });
  adapter.storeTokens({ access: "access-a", user: userA });
  const oldRequest = adapter.refreshAccessToken().catch(() => null);
  await afterMicrotasks();
  adapter.storeTokens({ access: "access-b", user: userB });
  const newRequest = adapter.refreshAccessToken();
  await afterMicrotasks();
  assert.equal(requests.length, 2, "A different session must have its own refresh");
  requests[0].resolve(responseFor(userA));
  await oldRequest;
  assert.equal(adapter.refreshAccessToken(), newRequest, "Old finally must leave the new in-flight request intact");
  requests[1].resolve(responseFor(userB, "refreshed-b"));
  assert.equal(await newRequest, "refreshed-b");
  assert.deepEqual(session.getUser(), userB);
});

for (const outcome of ["success", "failure"]) {
  test(`late me ${outcome} cannot merge into or clear a different user`, async () => {
    const profile = deferred();
    const started = deferred();
    const { adapter, session } = harness({ get: () => { started.resolve(); return profile.promise; } });
    const pending = adapter.initializeAuth();
    await started.promise;
    adapter.storeTokens({ access: "access-b", user: userB });
    if (outcome === "success") profile.resolve({ data: { username: "old-private-profile", email: "a@example.test" } });
    else profile.reject(new Error("old profile failed"));
    await pending;
    assert.equal(session.getAccessToken(), "access-b");
    assert.deepEqual(session.getUser(), userB);
  });
}

test("StrictMode initialization shares refresh and profile work within one generation", async () => {
  const profile = deferred();
  const started = deferred();
  let profileCalls = 0;
  let refreshCalls = 0;
  const { adapter } = harness({
    get: () => { profileCalls += 1; started.resolve(); return profile.promise; },
    post: async () => { refreshCalls += 1; return responseFor(userA); },
  });
  const first = adapter.initializeAuth();
  const second = adapter.initializeAuth();
  await started.promise;
  await afterMicrotasks();
  assert.equal(refreshCalls, 1);
  assert.equal(profileCalls, 1);
  profile.resolve({ data: { username: userA.username } });
  assert.deepEqual(await first, userA);
  assert.deepEqual(await second, userA);
});

for (const outcome of ["success", "failure"]) {
  test(`late logout ${outcome} cannot clear an intervening login`, async () => {
    const logout = deferred();
    const started = deferred();
    const { adapter, session } = harness({ post: () => { started.resolve(); return logout.promise; } });
    adapter.storeTokens({ access: "access-a", user: userA });
    const pending = adapter.authApi.logout().catch(() => null);
    await started.promise;
    adapter.storeTokens({ access: "access-b", user: userB });
    if (outcome === "success") logout.resolve({ data: {} });
    else logout.reject(new Error("old logout failed"));
    await pending;
    assert.equal(session.getAccessToken(), "access-b");
    assert.deepEqual(session.getUser(), userB);
  });
}

for (const endpoint of ["login", "staffLogin"]) {
  test(`an old ${endpoint} response cannot be stored after logout`, async () => {
    const login = deferred();
    const started = deferred();
    const { adapter, session } = harness({ post: (path) => {
      if (path === AUTH_ENDPOINTS.logout) return Promise.resolve({ data: {} });
      started.resolve();
      return login.promise;
    } });
    const pending = adapter.authApi[endpoint]("member-a", "synthetic-password").catch(() => null);
    await started.promise;
    await adapter.authApi.logout();
    login.resolve(responseFor(userA));
    const response = await pending;
    if (response) adapter.storeTokens(response.data);
    assert.equal(session.getAccessToken(), null);
    assert.equal(session.getUser(), null);
  });
}

test("a returned login response stays bound to its session until its caller stores it", async () => {
  const { adapter, session } = harness();
  const response = await adapter.authApi.login("member-a", "synthetic-password");
  adapter.storeTokens({ access: "access-b", user: userB });
  adapter.storeTokens(response.data);
  assert.equal(session.getAccessToken(), "access-b");
  assert.deepEqual(session.getUser(), userB);
});

test("late CSRF cannot post or cache credentials for a new session", async () => {
  const csrf = deferred();
  const started = deferred();
  const posts = [];
  let csrfCalls = 0;
  const { adapter, session } = harness({
    csrf: () => {
      csrfCalls += 1;
      if (csrfCalls === 1) { started.resolve(); return csrf.promise; }
      return Promise.resolve({ data: { csrf_token: "new-csrf" } });
    },
    post: async (_path, _data, config) => { posts.push(config.headers); return responseFor(userB); },
  });
  const oldRequest = adapter.refreshAccessToken().catch(() => null);
  await started.promise;
  adapter.storeTokens({ access: "access-b", user: userB });
  const newRequest = adapter.refreshAccessToken();
  await afterMicrotasks();
  csrf.resolve({ data: { csrf_token: "old-csrf" } });
  await oldRequest;
  await newRequest;
  assert.deepEqual(posts, [{ "X-CSRFToken": "new-csrf" }]);
  assert.deepEqual(session.getUser(), userB);
});

for (const status of [200, 401, 403]) {
  test(`an old transport ${status} response is not delivered or retried with a new identity`, async () => {
    const session = createAuthSession();
    session.store({ access: "access-a", user: userA });
    const transport = createWebApiTransport({ baseURL: "https://example.test/api/v1", session });
    const started = deferred();
    const response = deferred();
    let attempts = 0;
    let unauthorizedCalls = 0;
    transport.api.defaults.adapter = async (config) => {
      attempts += 1;
      started.resolve(config);
      await response.promise;
      if (status !== 200) {
        const error = new Error("Old identity rejected");
        error.config = config;
        error.response = { status };
        throw error;
      }
      return { config, data: { private: "member-a" }, headers: {}, status: 200, statusText: "OK" };
    };
    transport.setUnauthorizedHandler(async ({ request, client }) => { unauthorizedCalls += 1; return client(request); });
    const pending = transport.api.get("staff/dashboard/").then((value) => ({ value }), (error) => ({ error }));
    await started.promise;
    session.store({ access: "access-b", user: userB });
    response.resolve();
    const result = await pending;
    assert.equal(result.value, undefined, "Do not return the old user's response to a new session");
    assert.equal(result.error?.code, "AUTH_SESSION_CHANGED");
    assert.equal(result.error?.response, undefined, "Old 401/403 must not invoke a page's current-user cleanup");
    assert.equal(attempts, 1);
    assert.equal(unauthorizedCalls, 0);
  });
}

test("concurrent current-session 401s share one refresh and each retry once", async () => {
  const session = createAuthSession();
  session.store({ access: "expired-a", user: userA });
  const transport = createWebApiTransport({ baseURL: "https://example.test/api/v1", session });
  let refreshCalls = 0;
  const attempts = new Map();
  const adapter = createWebAuthAdapter({
    session, api: transport.api,
    cookieClient: {
      get: async () => ({ data: { csrf_token: "synthetic-csrf" } }),
      post: async () => { refreshCalls += 1; await afterMicrotasks(); return responseFor(userA, "refreshed-a"); },
    },
  });
  transport.setUnauthorizedHandler(adapter.handleUnauthorized);
  transport.api.defaults.adapter = async (config) => {
    attempts.set(config.url, (attempts.get(config.url) || 0) + 1);
    if (!config._retry) {
      const error = new Error("Expired");
      error.config = config;
      error.response = { status: 401 };
      throw error;
    }
    assert.equal(config.headers.Authorization, "Bearer refreshed-a");
    return { config, data: {}, headers: {}, status: 200, statusText: "OK" };
  };
  await Promise.all([transport.api.get("entries/"), transport.api.get("settings/")]);
  assert.equal(refreshCalls, 1);
  assert.deepEqual([...attempts.values()], [2, 2]);
});

test("a refresh 401 arriving after account switch cannot reach current-user error cleanup", async () => {
  const session = createAuthSession();
  session.store({ access: "expired-a", user: userA });
  const transport = createWebApiTransport({ baseURL: "https://example.test/api/v1", session });
  const refresh = deferred();
  const started = deferred();
  const adapter = createWebAuthAdapter({
    session, api: transport.api,
    cookieClient: {
      get: async () => ({ data: { csrf_token: "synthetic-csrf" } }),
      post: () => { started.resolve(); return refresh.promise; },
    },
  });
  transport.setUnauthorizedHandler(adapter.handleUnauthorized);
  transport.api.defaults.adapter = async (config) => {
    const error = new Error("Expired");
    error.config = config;
    error.response = { status: 401 };
    throw error;
  };
  const pending = transport.api.get("staff/dashboard/").catch((error) => {
    if (error.response?.status === 401 || error.response?.status === 403) adapter.clearTokens();
    return error;
  });
  await started.promise;
  adapter.storeTokens({ access: "access-b", user: userB });
  const oldError = new Error("Old refresh rejected");
  oldError.response = { status: 401 };
  refresh.reject(oldError);
  const error = await pending;
  assert.equal(error.code, "AUTH_SESSION_CHANGED");
  assert.equal(error.response, undefined);
  assert.deepEqual(session.getUser(), userB);
});
