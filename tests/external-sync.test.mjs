import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  SYNC_STATE_LABELS,
  syncEntryPatch,
  syncUiActions,
  syncValueLabel,
} from "../src/lib/externalSync.js";
import { apiToRecord } from "../src/pages/dashboardData.js";

const panel = readFileSync(new URL("../src/components/dashboard/ExternalCollectionSyncPanel.jsx", import.meta.url), "utf8");
const identityPanel = readFileSync(new URL("../src/components/dashboard/ExternalMediaIdentityPanel.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("sync field labels preserve missing, empty, decimal, and status semantics", () => {
  assert.equal(syncValueLabel("review", { present: false, value: null }), "未设置");
  assert.equal(syncValueLabel("review", { present: true, value: "" }), "空内容");
  assert.equal(syncValueLabel("personal_score", { present: true, value: "8.5" }), "8.5");
  assert.equal(syncValueLabel("watch_status", { present: true, value: "dropped" }), "抛弃");
  assert.equal(SYNC_STATE_LABELS.conflict, "双方冲突");
  assert.equal(SYNC_STATE_LABELS.unsupported, "无法同步");
});

test("frontend exposes only pull and accept-equal actions", () => {
  assert.deepEqual(
    syncUiActions({ recommended_actions: ["pull_remote", "push_local", "accept_equal"] }),
    ["pull_remote", "accept_equal"],
  );
  assert.doesNotMatch(panel, /action:\s*["']push_local["']/);
  assert.doesNotMatch(panel, /同步到 Bangumi/);
  assert.match(panel, /使用 Bangumi/);
  assert.match(panel, /保留 AniMemo/);
  assert.match(panel, /确认当前一致/);
});

test("sync entry refresh maps only the three local collection fields", () => {
  assert.deepEqual(syncEntryPatch({
    watch_status: "completed",
    watch_status_display: "看过",
    personal_score: "8.00",
    review: "完成",
    updated_at: "2026-08-09T12:00:00Z",
    title: "must not be copied",
  }), {
    status: "completed",
    statusLabel: "看过",
    score: 8,
    review: "完成",
    updatedAt: "2026-08-09T12:00:00Z",
  });
});

test("API journal entries remain renderable for the sync panel host", () => {
  const record = apiToRecord({
    id: 42,
    title: "QA",
    tags: ["日常"],
    tag_colors: { 日常: "blue" },
    watch_status: "watching",
    personal_score: "8.5",
    external_identities: [],
  }, {});

  assert.equal(record.id, 42);
  assert.equal(record.status, "watching");
  assert.deepEqual(record.tags, ["日常"]);
});

test("comparison is lazy, signed, server-authoritative, and pull-only", () => {
  assert.match(identityPanel, /ExternalCollectionSyncPanel/);
  assert.match(panel, /api\.get\("external-accounts\/"\)/);
  assert.match(panel, /const openComparison = async \(\) =>/);
  assert.match(panel, /openComparison[\s\S]*refreshPreview\(\)/);
  assert.match(panel, /external-sync\/providers\/\$\{PROVIDER\}\/entries\/\$\{entryId\}\/preview\//);
  assert.match(panel, /external-sync\/providers\/\$\{PROVIDER\}\/entries\/\$\{entryId\}\/apply\//);
  assert.match(panel, /\{ preview_token: preview\.preview_token, actions \}/);
  assert.doesNotMatch(panel, /(?:local_value|remote_value|baseline_value)\s*:/);
  assert.match(panel, /当前只会拉取到 AniMemo，不会修改 Bangumi/);
  assert.match(panel, /provider_unavailable/);
  assert.match(panel, /Bangumi 暂时不可用/);
  assert.match(panel, /collection_sync_pull_available/);
  assert.match(panel, /!provider\?\.collection_write_implemented/);
});

test("stale apply refreshes preview but never retries apply", () => {
  const staleBranch = panel.match(/if \(code === "sync_preview_stale"\) \{([\s\S]*?)\n\s*\} else if/);
  assert.ok(staleBranch);
  assert.match(staleBranch[1], /refreshPreview\(\)/);
  assert.doesNotMatch(staleBranch[1], /applySelection|api\.post/);
  assert.match(panel, /数据已发生变化，请重新确认/);
});

test("sync controls remain keyboard-visible and collapse to one column on mobile", () => {
  assert.match(panel, /aria-pressed=/);
  assert.match(panel, /role="group"/);
  assert.match(panel, /role="alert"/);
  assert.match(styles, /\.external-sync-panel button:focus-visible/);
  assert.match(styles, /\.external-sync-field__values \{ grid-template-columns: 1fr; \}/);
  assert.match(styles, /\.external-sync-field__actions \{ display: grid; grid-template-columns: 1fr; \}/);
});
