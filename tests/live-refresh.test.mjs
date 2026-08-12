import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createLiveRefreshController } from "../src/lib/liveRefresh.js";

const adminUpdatePanelSource = readFileSync(new URL("../src/components/admin/AdminUpdatePanel.jsx", import.meta.url), "utf8");

function createEventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    removeEventListener(type, listener) {
      if (listeners.get(type) === listener) listeners.delete(type);
    },
    dispatch(type) {
      listeners.get(type)?.();
    },
    has(type) {
      return listeners.has(type);
    },
  };
}

test("keeps staff review queues fresh without overlapping requests", async () => {
  const windowTarget = createEventTarget();
  const documentTarget = { ...createEventTarget(), visibilityState: "visible" };
  let intervalCallback;
  let clearedInterval;
  let calls = 0;
  let release;

  const controller = createLiveRefreshController({
    refresh: () => {
      calls += 1;
      return new Promise((resolve) => { release = resolve; });
    },
    intervalMs: 4000,
    windowTarget: {
      ...windowTarget,
      setInterval(callback) {
        intervalCallback = callback;
        return 73;
      },
      clearInterval(id) {
        clearedInterval = id;
      },
    },
    documentTarget,
  });

  const first = controller.refreshNow();
  intervalCallback();
  windowTarget.dispatch("focus");
  assert.equal(calls, 1, "an in-flight refresh must absorb duplicate triggers");

  release();
  await first;
  intervalCallback();
  assert.equal(calls, 2, "the periodic trigger refreshes once the prior request completes");
  release();
  await Promise.resolve();

  documentTarget.visibilityState = "hidden";
  intervalCallback();
  assert.equal(calls, 2, "hidden tabs do not poll unnecessarily");

  documentTarget.visibilityState = "visible";
  documentTarget.dispatch("visibilitychange");
  assert.equal(calls, 3, "returning to the admin tab refreshes immediately");
  release();
  await Promise.resolve();

  controller.dispose();
  assert.equal(clearedInterval, 73);
  assert.equal(windowTarget.has("focus"), false);
  assert.equal(documentTarget.has("visibilitychange"), false);
});

test("routes update operation polling through the shared live refresh controller", () => {
  assert.match(adminUpdatePanelSource, /createLiveRefreshController\(\{/);
  assert.match(adminUpdatePanelSource, /intervalMs:\s*2500/);
  assert.doesNotMatch(adminUpdatePanelSource, /window\.setInterval\(async/);
});

test("keeps active update operation polling visible, serialized, and terminal-aware", async () => {
  const windowTarget = createEventTarget();
  const documentTarget = { ...createEventTarget(), visibilityState: "visible" };
  let intervalCallback;
  let clearCalls = 0;
  let calls = 0;
  let releaseFirst;
  let releaseSecond;
  const loadStatusCalls = [];
  const updates = [
    new Promise((resolve) => { releaseFirst = resolve; }),
    new Promise((resolve) => { releaseSecond = resolve; }),
  ];

  const controller = createLiveRefreshController({
    refresh: async () => {
      const { data } = await {
        get(path) {
          calls += 1;
          assert.equal(path, "staff/system/updates/operations/operation-123/");
          return updates[calls - 1].then((next) => ({ data: next }));
        },
      }.get("staff/system/updates/operations/operation-123/");
      assert.equal(data.status, calls === 1 ? "applying" : "succeeded");
      if (data.status !== "applying") loadStatusCalls.push({ silent: true });
    },
    windowTarget: {
      ...windowTarget,
      setInterval(callback, intervalMs) {
        intervalCallback = callback;
        assert.equal(intervalMs, 2500);
        return 17;
      },
      clearInterval(id) {
        clearCalls += 1;
        assert.equal(id, 17);
      },
    },
    documentTarget,
    intervalMs: 2500,
  });

  const first = controller.refreshNow();
  intervalCallback();
  windowTarget.dispatch("focus");
  assert.equal(calls, 1, "a 3-second response must not overlap the 2.5-second poll");

  releaseFirst({ status: "applying" });
  await first;

  documentTarget.visibilityState = "hidden";
  intervalCallback();
  windowTarget.dispatch("focus");
  assert.equal(calls, 1, "hidden update tabs must not poll or focus-refresh");

  documentTarget.visibilityState = "visible";
  documentTarget.dispatch("visibilitychange");
  assert.equal(calls, 2, "returning to the update tab must refresh immediately");
  releaseSecond({ status: "succeeded" });
  await controller.refreshNow();
  assert.deepEqual(loadStatusCalls, [{ silent: true }], "terminal operation state must refresh the authoritative status");

  controller.dispose();
  assert.equal(clearCalls, 1);
});
