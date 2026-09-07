import assert from "node:assert/strict";
import test from "node:test";

import { CanceledError } from "axios";

import { createAuthSession } from "../src/lib/authSession.js";
import { createWebApiTransport } from "../src/lib/webApiTransport.js";

const userA = { id: 1, username: "member-a" };
const userB = { id: 2, username: "member-b" };
const response = (config) => ({ config, data: {}, headers: {}, status: 200, statusText: "OK" });

function observeSignal() {
  const controller = new AbortController();
  const listeners = new Set();
  const add = controller.signal.addEventListener.bind(controller.signal);
  const remove = controller.signal.removeEventListener.bind(controller.signal);
  controller.signal.addEventListener = (name, listener, options) => {
    if (name === "abort") listeners.add(listener);
    return add(name, listener, options);
  };
  controller.signal.removeEventListener = (name, listener, options) => {
    if (name === "abort") listeners.delete(listener);
    return remove(name, listener, options);
  };
  return { controller, listeners };
}

for (const clientName of ["api", "cookieClient"]) {
  for (const transition of ["advance", "store", "clear"]) {
    test(`${transition} synchronously aborts old ${clientName} requests and preserves new requests`, async () => {
      const session = createAuthSession();
      session.store({ access: "synthetic-a", user: userA });
      const { [clientName]: client } = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session });
      const caller = observeSignal();
      const dispatched = [];
      const releases = [];
      client.defaults.adapter = (config) => new Promise((resolve) => {
        dispatched.push(config);
        releases.push(() => resolve(response(config)));
      });
      const oldGeneration = session.getGeneration();
      const prior = client.get("probe/", { signal: caller.controller.signal }).catch((error) => error);
      assert.equal(caller.listeners.size, 1);
      if (transition === "advance") session.advanceGeneration();
      else if (transition === "store") session.store({ access: "synthetic-b", user: userB });
      else session.clear();
      assert.equal(dispatched[0].signal.aborted, true, "Abort must happen before transition returns");
      assert.equal(caller.controller.signal.aborted, false, "Session cleanup does not own the caller's controller");
      assert.equal(caller.listeners.size, 0);

      const current = client.get("probe/", { signal: caller.controller.signal }).catch((error) => error);
      assert.equal(dispatched[1].signal.aborted, false);
      assert.equal(caller.listeners.size, 1);
      session.clear(oldGeneration);
      assert.equal(dispatched[1].signal.aborted, false, "An old finally cannot abort the new session");
      releases[0]();
      assert.equal((await prior).code, "AUTH_SESSION_CHANGED");
      assert.equal(caller.listeners.size, 1, "Old request cleanup leaves the new listener intact");
      releases[1]();
      await current;
      assert.equal(caller.listeners.size, 0);
      session.advanceGeneration();
      assert.equal(dispatched[1].signal.aborted, false, "Settled requests are removed from cancellation tracking");
    });
  }

  test(`${clientName} preserves caller cancellation without clearing the current session`, async () => {
    const session = createAuthSession();
    session.store({ access: "synthetic-a", user: userA });
    const { [clientName]: client } = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session });
    const caller = observeSignal();
    client.defaults.adapter = (config) => new Promise((_resolve, reject) => {
      config.signal.addEventListener("abort", () => reject(new CanceledError("caller canceled", config)), { once: true });
    });
    const pending = client.get("probe/", { signal: caller.controller.signal }).catch((error) => error);
    caller.controller.abort();
    assert.equal((await pending).code, "ERR_CANCELED");
    assert.equal(caller.listeners.size, 0);
    assert.deepEqual(session.getUser(), userA);
  });

  test(`${clientName} removes caller listeners after a current-session failure`, async () => {
    const session = createAuthSession();
    session.store({ access: "synthetic-a", user: userA });
    const { [clientName]: client } = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session });
    const caller = observeSignal();
    let config;
    client.defaults.adapter = async (value) => {
      config = value;
      throw Object.assign(new Error("synthetic network failure"), { config });
    };
    await assert.rejects(client.get("probe/", { signal: caller.controller.signal }), /synthetic network failure/);
    assert.equal(caller.listeners.size, 0);
    session.advanceGeneration();
    assert.equal(config.signal.aborted, false);
  });
}

test("a retry uses the original caller signal and releases both request controllers", async () => {
  const session = createAuthSession();
  session.store({ access: "synthetic-a", user: userA });
  const transport = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session });
  const caller = observeSignal();
  const attempts = [];
  transport.api.defaults.adapter = async (config) => {
    attempts.push(config);
    assert.equal(caller.listeners.size, 1);
    if (!config._retry) throw Object.assign(new Error("expired"), { config, response: { status: 401 } });
    return response(config);
  };
  transport.setUnauthorizedHandler(async ({ request, client }) => {
    assert.equal(caller.listeners.size, 0);
    session.setAccessToken("refreshed-a");
    return client(request);
  });
  await transport.api.get("probe/", { signal: caller.controller.signal });
  assert.equal(attempts.length, 2);
  assert.notEqual(attempts[0].signal, attempts[1].signal);
  assert.equal(caller.listeners.size, 0);
  session.store({ access: "synthetic-b", user: userB });
  assert.equal(attempts[0].signal.aborted, false);
  assert.equal(attempts[1].signal.aborted, false);
});

test("same-generation token and profile updates do not cancel the active request", async () => {
  const session = createAuthSession();
  session.store({ access: "synthetic-a", user: userA });
  const { api } = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session });
  let config;
  let finish;
  api.defaults.adapter = (value) => new Promise((resolve) => { config = value; finish = () => resolve(response(config)); });
  const pending = api.get("probe/");
  session.setAccessToken("refreshed-a");
  session.mergeUser({ is_staff: true });
  session.store({ access: "refreshed-again-a", user: userA }, session.getGeneration());
  assert.equal(config.signal.aborted, false);
  finish();
  await pending;
});
