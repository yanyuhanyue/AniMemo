import assert from "node:assert/strict";
import test from "node:test";

import { createAuthSession } from "../src/lib/authSession.js";
import { createWebApiTransport } from "../src/lib/webApiTransport.js";

const userA = { id: 1, username: "member-a" };
const userB = { id: 2, username: "member-b" };
const intent = { review: "prior-user-intent" };

const calls = {
  patch: (api) => api.patch("entries/17/", intent),
  post: (api) => api.post("entries/", intent),
  put: (api) => api.put("entries/17/", intent),
  delete: (api) => api.delete("entries/17/", { data: intent }),
  request: (api) => api.request({ url: "entries/17/", method: "patch", data: intent }),
  callable: (api) => api({ url: "entries/17/", method: "patch", data: intent }),
};

for (const [method, invoke] of Object.entries(calls)) {
  for (const switchTiming of ["same turn", "next microtask"]) {
    test(`${method} binds the initiating identity before a ${switchTiming} account switch`, async () => {
      const session = createAuthSession();
      session.store({ access: "synthetic-a", user: userA });
      const originalGeneration = session.getGeneration();
      const sent = [];
      let release;
      const response = new Promise((resolve) => { release = resolve; });
      let mutations = 0;
      const { api } = createWebApiTransport({
        baseURL: "https://not-contacted.example.test/api/v1",
        session,
        onMutationSuccess: () => { mutations += 1; },
      });
      // Axios still performs configuration, interception and dispatch; only I/O is deferred.
      api.defaults.adapter = async (config) => {
        sent.push({ generation: config._authGeneration, authorization: config.headers.Authorization, data: JSON.parse(config.data) });
        await response;
        return { config, data: { private: "member-a" }, headers: {}, status: 200, statusText: "OK" };
      };
      const pending = invoke(api).then((value) => ({ value }), (error) => ({ error }));
      const switchAccount = () => session.store({ access: "synthetic-b", user: userB });
      if (switchTiming === "same turn") switchAccount();
      else await new Promise((resolve) => queueMicrotask(() => { switchAccount(); resolve(); }));
      release();
      const result = await pending;

      assert.deepEqual(sent, [{ generation: originalGeneration, authorization: "Bearer synthetic-a", data: intent }]);
      assert.equal(result.value, undefined);
      assert.equal(result.error?.code, "AUTH_SESSION_CHANGED");
      assert.equal(mutations, 0);
      assert.deepEqual(session.getUser(), userB);
      assert.equal(session.getAccessToken(), "synthetic-b");
    });
  }
}

test("an explicitly stale retry is rejected before Axios dispatch", async () => {
  const session = createAuthSession();
  session.store({ access: "synthetic-a", user: userA });
  const staleGeneration = session.getGeneration();
  session.store({ access: "synthetic-b", user: userB });
  const { api } = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session });
  let dispatched = 0;
  api.defaults.adapter = async () => { dispatched += 1; throw new Error("Must not dispatch"); };

  await assert.rejects(api.patch("entries/17/", intent, { _authGeneration: staleGeneration }), { code: "AUTH_SESSION_CHANGED" });
  assert.equal(dispatched, 0);
  assert.deepEqual(session.getUser(), userB);
});
