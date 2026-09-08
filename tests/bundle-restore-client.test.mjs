import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import test from "node:test";

import { createAuthSession } from "../src/lib/authSession.js";
import { createWebApiTransport } from "../src/lib/webApiTransport.js";
import { createWebAuthAdapter } from "../src/lib/webAuthAdapter.js";
import {
  BUNDLE_CHUNK_BYTES,
  BundleSha256,
  boundedImportPreview,
  bundleRestoreStorage,
  createBundleRestoreClient,
  hashBundleFile,
  readBundleRestorePointer,
} from "../src/lib/bundleRestore.js";

const sha = (data) => createHash("sha256").update(data).digest("hex");
const owner = { id: 7, username: "restore-owner" };
const other = { id: 8, username: "other-owner" };
const afterMicrotasks = () => new Promise((resolve) => setImmediate(resolve));

function memoryStorage() {
  const values = new Map();
  return { values, getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
}

function fileFor(bytes, name = "完整手账.json") {
  const reads = [];
  const file = {
    name, size: bytes.length, reads,
    text() { throw new Error("The whole file must never be read as text"); },
    arrayBuffer() { throw new Error("The whole file must never be read as a buffer"); },
    slice(start, end) {
      assert.ok(end - start <= BUNDLE_CHUNK_BYTES, "Each File.slice read has a 1 MiB bound");
      reads.push([start, end]);
      return { arrayBuffer: async () => Uint8Array.from(bytes.subarray(start, end)).buffer };
    },
  };
  return file;
}

function serverHarness({ storage = memoryStorage() } = {}) {
  let actor = owner;
  let generation = 5;
  let remote = null;
  let createKey = null;
  let fault = null;
  const calls = [];
  const bytes = [];
  const snapshots = [];
  const response = (data, config) => ({ data: structuredClone(data), config: { ...config, _authGeneration: generation } });
  const api = {
    async get(path, config) {
      calls.push({ method: "get", path, config });
      if (fault?.get) return fault.get(path, config);
      return response(path.endsWith("current/") ? { session: remote && !["completed", "cancelled", "failed", "expired"].includes(remote.state) ? remote : null } : remote, config);
    },
    async post(path, payload, config) {
      calls.push({ method: "post", path, payload, config });
      if (path === "bundle-restores/") {
        if (!remote) {
          createKey = payload.idempotency_key;
          remote = { id: randomUUID(), generation: 1, state: "receiving", expected_bytes: payload.expected_bytes,
            received_bytes: 0, sha256: payload.sha256, chunk_bytes: BUNDLE_CHUNK_BYTES, preview: {}, receipt: {}, error_code: "", cleanup_pending: false };
        } else assert.equal(payload.idempotency_key, createKey, "An uncertain create retries the same key");
        if (fault?.create) return fault.create(path, payload, config);
      } else if (path.endsWith("validate/")) {
        assert.equal(sha(Buffer.concat(bytes)), remote.sha256);
        remote.state = "ready";
        remote.preview = { total: 501, ready: 501, items: Array.from({ length: 501 }, (_, index) => ({ row: index + 1, title: `番剧-${index}`, status: "ready" })) };
        if (fault?.validate) return fault.validate(path, payload, config);
      } else if (path.endsWith("commit/")) {
        remote.state = "completed";
        remote.receipt = { format: "animemo-data-bundle", schema_version: 1, created: 501, total: 501 };
        if (fault?.commit) return fault.commit(path, payload, config);
      } else if (path.endsWith("cancel/")) {
        if (remote.state !== "completed") { remote.state = "cancelled"; remote.generation += 1; }
        if (fault?.cancel) return fault.cancel(path, payload, config);
      } else throw new Error(`Unexpected route: ${path}`);
      return response(remote, config);
    },
    async put(path, payload, config) {
      calls.push({ method: "put", path, bytes: payload.byteLength, config });
      assert.equal(config.headers["Content-Type"], "application/octet-stream");
      assert.equal(config.params.generation, remote.generation);
      assert.equal(config.params.offset, remote.received_bytes);
      assert.equal(payload.byteLength, Math.min(BUNDLE_CHUNK_BYTES, remote.expected_bytes - remote.received_bytes));
      bytes.push(Buffer.from(payload));
      remote.received_bytes += payload.byteLength;
      if (fault?.upload) return fault.upload(path, payload, config);
      return response(remote, config);
    },
  };
  const makeClient = () => createBundleRestoreClient({ api, ownerId: owner.id, getOwnerId: () => actor?.id, storage, createUuid: randomUUID, onChange: (state) => snapshots.push(state) });
  return {
    api, calls, bytes, snapshots, storage, makeClient,
    get remote() { return remote; },
    setFault: (value) => { fault = value; },
    setActor: (value, nextGeneration = generation + 1) => { actor = value; generation = nextGeneration; },
    authSnapshot: () => ({ user: actor, access: actor ? "synthetic-access" : null, generation }),
  };
}

test("incremental SHA-256 matches platform vectors and arbitrary block boundaries", () => {
  for (const input of [Buffer.alloc(0), Buffer.from("abc"), Buffer.from("a".repeat(1000000)), Buffer.from("中文\"\\\n🍀".repeat(30000))]) {
    for (const size of [1, 55, 56, 63, 64, 65, 65537]) {
      const digest = new BundleSha256();
      for (let offset = 0; offset < input.length; offset += size) digest.update(input.subarray(offset, offset + size));
      assert.equal(digest.digest(), sha(input), `length=${input.length}, block=${size}`);
      assert.throws(() => digest.update(new Uint8Array(0)), /finalized/);
    }
  }
});

test("large Unicode files hash through bounded File.slice and can be cancelled", async () => {
  const bytes = Buffer.from("汉".repeat(800000));
  const file = fileFor(bytes);
  const progress = [];
  assert.equal(await hashBundleFile(file, { onProgress: (value) => progress.push(value.loaded) }), sha(bytes));
  assert.deepEqual(file.reads.map(([start, end]) => end - start), [BUNDLE_CHUNK_BYTES, BUNDLE_CHUNK_BYTES, bytes.length - 2 * BUNDLE_CHUNK_BYTES]);
  assert.equal(progress.at(-1), bytes.length);
  const controller = new AbortController();
  await assert.rejects(hashBundleFile(file, { signal: controller.signal, onProgress: ({ loaded }) => { if (loaded) controller.abort(new Error("stop hash")); } }), /stop hash/);
});

test("all JSON goes through create/chunks/validate/commit and stores only a bounded pointer", async () => {
  const harness = serverHarness();
  const client = harness.makeClient();
  const bytes = Buffer.from(`{"description":"${"汉".repeat(800000)}"}`);
  const result = await client.prepare(fileFor(bytes));
  assert.equal(result.state, "ready");
  assert.equal(client.getSnapshot().preview.items.length, 50);
  assert.equal(client.getSnapshot().preview.total, 501);
  assert.equal(client.getSnapshot().preview.items_truncated, true);
  assert.equal(harness.calls.filter((call) => call.method === "put").length, 3);
  assert.equal(harness.calls.find((call) => call.path === "bundle-restores/").payload.sha256, sha(bytes));
  assert.equal(harness.calls.some((call) => call.path === "import/"), false);
  assert.deepEqual(Buffer.concat(harness.bytes), bytes);
  const stored = [...harness.storage.values.values()][0];
  assert.ok(stored.length < 1024);
  assert.deepEqual(Object.keys(JSON.parse(stored)).sort(), ["version", "owner_id", "session_id", "idempotency_key", "expected_bytes", "sha256", "file_name"].sort());
  assert.equal(stored.includes("番剧"), false);
  assert.equal((await client.commit()).state, "completed");
  assert.equal(client.getSnapshot().receipt.created, 501);
  const fresh = harness.makeClient();
  assert.equal((await fresh.discover()).state, "completed", "Refresh recovers a completed receipt by its saved session ID");
});

test("small JSON also uses the session protocol", async () => {
  const harness = serverHarness();
  await harness.makeClient().prepare(fileFor(Buffer.from("{}")));
  assert.equal(harness.calls.filter((call) => call.method === "put").length, 1);
  assert.equal(harness.calls.some((call) => call.path === "import/"), false);
});

test("a removed receipt pointer can discover a new session without clearing another owner's pointer", async () => {
  const harness = serverHarness();
  const client = harness.makeClient();
  await client.prepare(fileFor(Buffer.from("{}")));
  const key = `animemo_bundle_restore_v1:${other.id}`;
  harness.storage.setItem(key, "other-owner-pointer");
  const originalGet = harness.api.get;
  harness.api.get = async (path, config) => {
    if (!path.endsWith("current/")) throw Object.assign(new Error("receipt expired"), { response: { status: 404 } });
    return { data: { session: null }, config: { _authGeneration: 5 } };
  };
  assert.equal(await client.discover(), null);
  assert.equal(readBundleRestorePointer(harness.storage, owner.id), null);
  assert.equal(client.getSnapshot().session, null);
  assert.equal(harness.storage.getItem(key), "other-owner-pointer");
  harness.api.get = originalGet;
});

test("a resumed upload that already completed in another tab returns its receipt", async () => {
  const harness = serverHarness();
  const client = harness.makeClient();
  harness.setFault({ upload: () => { throw new Error("paused upload"); } });
  await assert.rejects(client.prepare(fileFor(Buffer.from("{}"))), /paused upload/);
  harness.setFault(null);
  harness.remote.state = "completed";
  harness.remote.receipt = { format: "animemo-data-bundle", schema_version: 1, total: 501, created: 501 };
  assert.equal((await client.resume()).state, "completed");
  assert.equal(harness.calls.filter((call) => call.path === "bundle-restores/").length, 1);
});

test("lost chunk response resumes after a fresh file hash at authoritative received_bytes", async () => {
  const harness = serverHarness();
  const bytes = Buffer.from("字".repeat(800000));
  const client = harness.makeClient();
  harness.setFault({ upload: () => { throw new Error("connection lost after accepted chunk"); } });
  await assert.rejects(client.prepare(fileFor(bytes)), /connection lost/);
  assert.equal(harness.remote.received_bytes, BUNDLE_CHUNK_BYTES);
  assert.equal(client.getSnapshot().preview, null, "Receiving has no validated count; it is not an empty bundle");
  harness.setFault(null);
  client.dispose();
  const fresh = harness.makeClient();
  assert.equal((await fresh.discover()).received_bytes, BUNDLE_CHUNK_BYTES);
  const mismatch = fileFor(Buffer.alloc(bytes.length, 65));
  await assert.rejects(fresh.prepare(mismatch), { code: "BUNDLE_FILE_MISMATCH" });
  assert.equal(harness.calls.filter((call) => call.method === "put").length, 1);
  const file = fileFor(bytes);
  assert.equal((await fresh.prepare(file)).state, "ready");
  assert.deepEqual(harness.calls.filter((call) => call.method === "put").map((call) => call.config.params.offset), [0, BUNDLE_CHUNK_BYTES, 2 * BUNDLE_CHUNK_BYTES]);
  assert.deepEqual(Buffer.concat(harness.bytes), bytes);
});

test("lost create response retains the idempotency key and can safely cancel its result", async () => {
  const harness = serverHarness();
  const client = harness.makeClient();
  harness.setFault({ create: () => { throw new Error("create response lost"); } });
  await assert.rejects(client.prepare(fileFor(Buffer.from("{}"))), /create response lost/);
  const pointer = readBundleRestorePointer(harness.storage, owner.id);
  assert.equal(pointer.session_id, null);
  assert.ok(pointer.idempotency_key);
  harness.setFault(null);
  assert.equal((await client.cancel()).state, "cancelled");
  assert.equal(harness.calls.filter((call) => call.path === "bundle-restores/").length, 2);
  assert.equal(harness.calls.filter((call) => call.method === "put").length, 0);
});

test("lost commit response reads the same receipt without a second business commit", async () => {
  const harness = serverHarness();
  const client = harness.makeClient();
  await client.prepare(fileFor(Buffer.from("{}")));
  harness.setFault({ commit: () => { throw new Error("commit response lost"); } });
  assert.equal((await client.commit()).state, "completed");
  assert.equal((await client.commit()).state, "completed");
  assert.equal(harness.calls.filter((call) => call.path.endsWith("commit/")).length, 1);
  assert.equal((await client.cancel()).state, "completed", "Late cancellation cannot undo the receipt");
});

test("a failed receipt lookup stays unresolved and does not report success", async () => {
  const harness = serverHarness();
  const client = harness.makeClient();
  await client.prepare(fileFor(Buffer.from("{}")));
  harness.setFault({ commit: () => {
    harness.setFault({ get: () => { throw new Error("status unavailable"); } });
    throw new Error("commit result unknown");
  } });
  await assert.rejects(client.commit(), /commit result unknown/);
  assert.equal(client.getSnapshot().receipt, null);
  assert.notEqual(client.getSnapshot().phase, "completed");
  harness.setFault(null);
  assert.equal((await client.resume()).state, "completed");
});

test("account changes clear private preview and abort a pending request including late failures", async () => {
  for (const outcome of ["resolve", "reject"]) {
    const harness = serverHarness();
    const client = harness.makeClient();
    await client.prepare(fileFor(Buffer.from("{}")));
    let release;
    harness.setFault({ get: (_path, config) => new Promise((resolve, reject) => {
      release = () => outcome === "resolve" ? resolve({ data: harness.remote, config }) : reject(new Error("old response"));
    }) });
    const pending = client.discover();
    await afterMicrotasks();
    harness.setActor(other);
    client.authChanged(harness.authSnapshot());
    assert.equal(harness.calls.at(-1).config.signal.aborted, true);
    assert.equal(client.getSnapshot().preview, null);
    assert.equal(client.getSnapshot().session, null);
    const clean = client.getSnapshot();
    release();
    await assert.rejects(pending);
    assert.equal(client.getSnapshot(), clean, "Old success/catch/finally cannot repopulate a new identity");
    assert.equal(readBundleRestorePointer(harness.storage, other.id), null);
  }
});

test("same-generation token renewal keeps work; a same-owner new login invalidates it", async () => {
  const harness = serverHarness();
  const client = harness.makeClient();
  await client.prepare(fileFor(Buffer.from("{}")));
  client.authChanged(harness.authSnapshot());
  assert.equal(client.getSnapshot().phase, "ready");
  harness.setActor(owner, 6);
  client.authChanged(harness.authSnapshot());
  assert.equal(client.getSnapshot().phase, "authentication_required");
  await assert.rejects(client.commit(), { code: "AUTH_SESSION_CHANGED" });
});

test("real #226 transport rejects a token-loss result without accepting a fake completed response", async () => {
  const auth = createAuthSession();
  auth.store({ access: "synthetic-a", user: owner });
  const { api } = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session: auth });
  let release;
  api.defaults.adapter = (config) => new Promise((resolve) => { release = () => resolve({ config, status: 200, statusText: "OK", headers: {}, data: { session: null } }); });
  const client = createBundleRestoreClient({ api, ownerId: owner.id, getOwnerId: () => auth.getUser()?.id });
  const unsubscribe = auth.subscribe(client.authChanged);
  const pending = client.discover();
  auth.clear();
  release();
  await assert.rejects(pending, { code: "AUTH_SESSION_CHANGED" });
  assert.equal(client.getSnapshot().phase, "authentication_required");
  assert.equal(client.getSnapshot().receipt, null);
  unsubscribe();
});

test("a server 401 with an expired refresh session leaves an authentication failure", async () => {
  const auth = createAuthSession();
  auth.store({ access: "expired-synthetic-access", user: owner });
  const transport = createWebApiTransport({ baseURL: "https://not-contacted.example.test/api/v1", session: auth });
  const unauthorized = (config) => Object.assign(new Error("unauthorized"), { config, response: { status: 401, data: { code: "session_expired", detail: "登录会话已失效。" } } });
  const webAuth = createWebAuthAdapter({ api: transport.api, session: auth, cookieClient: {
    get: async () => ({ data: { csrf_token: "synthetic-csrf" } }),
    post: async () => { throw unauthorized({}); },
  } });
  transport.setUnauthorizedHandler(webAuth.handleUnauthorized);
  transport.api.defaults.adapter = async (config) => { throw unauthorized(config); };
  const client = createBundleRestoreClient({ api: transport.api, ownerId: owner.id, getOwnerId: () => auth.getUser()?.id });
  const unsubscribe = auth.subscribe(client.authChanged);
  await assert.rejects(client.discover(), { code: "AUTH_SESSION_CHANGED" });
  assert.equal(client.getSnapshot().phase, "authentication_required");
  assert.equal(client.getSnapshot().receipt, null);
  assert.equal(client.getSnapshot().preview, null);
  assert.equal(auth.getUser(), null);
  unsubscribe();
});

test("cancel wins over a delayed upload response without old finally restoring busy state", async () => {
  const harness = serverHarness();
  const client = harness.makeClient();
  let release;
  harness.setFault({ upload: (_path, _payload, config) => new Promise((resolve) => {
    release = () => resolve({ data: harness.remote, config });
  }) });
  const upload = client.prepare(fileFor(Buffer.from("{}")));
  await afterMicrotasks();
  assert.equal(harness.calls.at(-1).method, "put");
  assert.equal((await client.cancel()).state, "cancelled");
  const cancelled = client.getSnapshot();
  release();
  await assert.rejects(upload);
  assert.equal(client.getSnapshot(), cancelled);
  assert.equal(client.getSnapshot().busy, false);
  assert.equal(harness.calls.some((call) => call.path.endsWith("validate/")), false);
});

test("malformed progress and completed-without-receipt responses fail closed", async () => {
  const harness = serverHarness();
  const client = harness.makeClient();
  await client.prepare(fileFor(Buffer.from("{}")));
  harness.remote.state = "completed";
  harness.remote.receipt = {};
  await assert.rejects(client.discover(), { code: "BUNDLE_RECEIPT_INVALID" });
  assert.equal(client.getSnapshot().receipt, null);
  harness.remote.state = "receiving";
  harness.remote.received_bytes = 1;
  await assert.rejects(client.discover(), { code: "BUNDLE_PROTOCOL_INVALID" });
});

test("preview retention, owner-bound storage and restricted storage are bounded and nonfatal", async () => {
  assert.equal(boundedImportPreview({}), null);
  assert.equal(boundedImportPreview({ total: 0 }), null);
  assert.equal(boundedImportPreview({ total: 0, ready: 0, items: [] }).total, 0, "A validated empty bundle remains distinct from an unknown preview");
  const preview = boundedImportPreview({ total: 5000, ready: 5000, items: Array.from({ length: 5000 }, (_, row) => ({ row, title: "密".repeat(10000), status: "ready", secret: "not retained" })) });
  assert.equal(preview.items.length, 50);
  assert.equal(preview.items[0].title.length, 200);
  assert.equal(Object.hasOwn(preview.items[0], "secret"), false);
  assert.equal(bundleRestoreStorage({ get localStorage() { throw new Error("denied"); } }), null);
  const storage = { getItem: () => "x".repeat(4097), setItem: () => { throw new Error("quota"); }, removeItem: () => { throw new Error("denied"); } };
  assert.equal(readBundleRestorePointer(storage, owner.id), null);
  const harness = serverHarness({ storage });
  assert.equal((await harness.makeClient().prepare(fileFor(Buffer.from("{}")))).state, "ready");
});
