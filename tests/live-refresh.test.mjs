import test from "node:test";
import assert from "node:assert/strict";
import { createLiveRefreshController } from "../src/lib/liveRefresh.js";

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
