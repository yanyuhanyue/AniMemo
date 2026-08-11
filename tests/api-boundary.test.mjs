import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  API_ROOT_PATH,
  AUTH_ENDPOINTS,
  isAuthInfrastructureRequest,
  normalizeAntiAbuseChallenge,
  resolveApiBaseUrl,
  withAntiAbuseChallenge,
} from "../src/lib/apiCore.js";
import { createAuthSession } from "../src/lib/authSession.js";
import { createWebApiTransport } from "../src/lib/webApiTransport.js";
import { createWebAuthAdapter } from "../src/lib/webAuthAdapter.js";


test("API Core and Auth Session run without browser or transport globals", () => {
  const coreSource = readFileSync(new URL("../src/lib/apiCore.js", import.meta.url), "utf8");
  const sessionSource = readFileSync(new URL("../src/lib/authSession.js", import.meta.url), "utf8");
  const sharedSource = `${coreSource}\n${sessionSource}`;

  assert.equal(API_ROOT_PATH, "/api/v1");
  assert.equal(resolveApiBaseUrl({ origin: "https://example.test" }), "https://example.test/api/v1");
  assert.equal(resolveApiBaseUrl({ configuredBaseUrl: "https://api.example.test/custom/" }), "https://api.example.test/custom");
  assert.equal(isAuthInfrastructureRequest("token/"), true);
  assert.equal(isAuthInfrastructureRequest("/api/v1/token/refresh/"), true);
  assert.equal(isAuthInfrastructureRequest("https://example.test/api/v1/auth/logout/"), true);
  assert.equal(isAuthInfrastructureRequest("entries/"), false);
  assert.doesNotMatch(sharedSource, /\bwindow\b|\bdocument\b|localStorage|sessionStorage|axios|cookie/i);
});

test("Auth Session keeps access in memory and merges refreshed user claims", () => {
  const session = createAuthSession();
  const snapshots = [];
  const unsubscribe = session.subscribe((snapshot) => snapshots.push(snapshot));

  session.store({ access: "first", user: { id: 1, is_staff: true, role: "administrator" } });
  const merged = session.mergeUser({ username: "admin" });

  assert.equal(session.getAccessToken(), "first");
  assert.deepEqual(merged, { id: 1, is_staff: true, role: "administrator", username: "admin" });
  assert.equal(snapshots.at(-1).user.role, "administrator");
  unsubscribe();
  session.clear();
  assert.equal(session.getAccessToken(), null);
});

test("anti-abuse challenge uses a provider-neutral contract with a Turnstile compatibility alias", () => {
  assert.deepEqual(normalizeAntiAbuseChallenge(" token "), { provider: "turnstile", token: "token" });
  assert.deepEqual(
    withAntiAbuseChallenge({ username: "user" }, { provider: "turnstile", token: "abc" }),
    {
      username: "user",
      challenge: { provider: "turnstile", token: "abc" },
      "cf-turnstile-response": "abc",
    },
  );
  assert.deepEqual(
    withAntiAbuseChallenge({ username: "user" }, { provider: "app-attestation", token: "proof" }),
    { username: "user", challenge: { provider: "app-attestation", token: "proof" } },
  );
});

test("Web Auth Adapter shares one refresh request and keeps refresh credentials out of the session", async () => {
  const session = createAuthSession();
  const removed = [];
  let csrfRequests = 0;
  let refreshRequests = 0;
  const cookieClient = {
    get: async (path) => {
      assert.equal(path, AUTH_ENDPOINTS.csrf);
      csrfRequests += 1;
      return { data: { csrf_token: "csrf" } };
    },
    post: async (path, _data, config) => {
      assert.equal(path, AUTH_ENDPOINTS.refresh);
      assert.equal(config.headers["X-CSRFToken"], "csrf");
      refreshRequests += 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return { data: { access: "access", user: { id: 1 } } };
    },
  };
  const adapter = createWebAuthAdapter({
    api: { get: async () => ({ data: { username: "member" } }) },
    cookieClient,
    session,
    browser: {
      localStorage: { removeItem: (key) => removed.push(["local", key]) },
      sessionStorage: { removeItem: (key) => removed.push(["session", key]) },
    },
  });

  const [first, second] = await Promise.all([adapter.refreshAccessToken(), adapter.refreshAccessToken()]);

  assert.equal(first, "access");
  assert.equal(second, "access");
  assert.equal(csrfRequests, 1);
  assert.equal(refreshRequests, 1);
  assert.deepEqual(adapter.getStoredTokens(), { access: "access", refresh: null });
  assert.equal(removed.length >= 4, true);
});

test("Web Auth Adapter preserves staff claims when initializeAuth merges profile data", async () => {
  const session = createAuthSession();
  const adapter = createWebAuthAdapter({
    api: { get: async () => ({ data: { username: "admin" } }) },
    cookieClient: {
      get: async () => ({ data: { csrf_token: "csrf" } }),
      post: async () => ({ data: { access: "access", user: { id: 1, role: "administrator", is_staff: true } } }),
    },
    session,
  });

  const user = await adapter.initializeAuth();

  assert.deepEqual(user, { id: 1, role: "administrator", is_staff: true, username: "admin" });
  assert.deepEqual(session.getUser(), user);
});

test("Web Auth Adapter rotates CSRF after login and authenticates logout before clearing memory", async () => {
  const session = createAuthSession();
  const posts = [];
  let csrfRequests = 0;
  const adapter = createWebAuthAdapter({
    api: {},
    cookieClient: {
      get: async () => ({ data: { csrf_token: `csrf-${++csrfRequests}` } }),
      post: async (path, data, config) => {
        posts.push({ path, data, headers: config.headers });
        return { data: { access: "login-access" } };
      },
    },
    session,
  });

  await adapter.authApi.login("member", "secret", "proof");
  assert.deepEqual(posts[0].data.challenge, { provider: "turnstile", token: "proof" });
  assert.equal(posts[0].data["cf-turnstile-response"], "proof");
  assert.equal(csrfRequests, 2);

  session.store({ access: "current-access", user: { id: 1 } });
  await adapter.authApi.logout();

  const logout = posts.at(-1);
  assert.equal(logout.path, AUTH_ENDPOINTS.logout);
  assert.equal(logout.headers.Authorization, "Bearer current-access");
  assert.equal(session.getAccessToken(), null);
  assert.equal(session.getUser(), null);
});

test("Web API Transport retries one non-auth 401 through the installed auth adapter", async () => {
  const session = createAuthSession();
  session.setAccessToken("old-access");
  const transport = createWebApiTransport({ baseURL: "https://example.test/api/v1", session });
  let adapterAttempts = 0;
  let unauthorizedCalls = 0;

  transport.api.defaults.adapter = async (config) => {
    adapterAttempts += 1;
    if (adapterAttempts === 1) {
      const error = new Error("expired");
      error.config = config;
      error.response = { status: 401 };
      throw error;
    }
    return {
      config,
      data: { ok: true },
      headers: {},
      request: {},
      status: 200,
      statusText: "OK",
    };
  };
  transport.setUnauthorizedHandler(async ({ request, client }) => {
    unauthorizedCalls += 1;
    session.setAccessToken("new-access");
    request.headers.Authorization = "Bearer new-access";
    return client(request);
  });

  const response = await transport.api.get("entries/");

  assert.deepEqual(response.data, { ok: true });
  assert.equal(adapterAttempts, 2);
  assert.equal(unauthorizedCalls, 1);
  assert.equal(response.config._retry, true);
  assert.equal(response.config.headers.Authorization, "Bearer new-access");
});
