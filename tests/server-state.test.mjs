import assert from "node:assert/strict";
import test from "node:test";

import {
  getServerStateRevision,
  invalidateServerState,
  invalidateServerStateForRequest,
  subscribeServerState,
} from "../src/lib/serverState.js";

test("server-state invalidation notifies only subscribed domains", () => {
  const events = [];
  const unsubscribe = subscribeServerState((event) => events.push(event), "journal_entries");
  const before = getServerStateRevision("journal_entries");
  invalidateServerState("settings");
  assert.equal(events.length, 0);
  invalidateServerState("journal_entries", 42);
  assert.equal(getServerStateRevision("journal_entries"), before + 1);
  assert.deepEqual(events.at(-1), { domains: ["journal_entries"], entityId: 42 });
  unsubscribe();
});

test("successful mutation mapping covers journal, history, sync, and settings domains", () => {
  const events = [];
  const unsubscribe = subscribeServerState((event) => events.push(event));
  invalidateServerStateForRequest({ method: "post", url: "entries/7/watch-history/" });
  invalidateServerStateForRequest({ method: "post", url: "external-sync/providers/bangumi/entries/7/apply/" });
  invalidateServerStateForRequest({ method: "post", url: "external-accounts/bangumi/import-apply/" });
  invalidateServerStateForRequest({ method: "patch", url: "settings/me/" });
  invalidateServerStateForRequest({ method: "get", url: "entries/7/" });
  assert.deepEqual(events.map((event) => event.domains), [
    ["watch_history", "journal_entries", "analytics"],
    ["external_sync", "journal_entries", "analytics"],
    ["external_accounts", "journal_entries", "analytics"],
    ["settings", "showcase"],
  ]);
  assert.equal(events[0].entityId, 7);
  assert.equal(events[1].entityId, 7);
  unsubscribe();
});
