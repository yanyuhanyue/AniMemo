import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  SNAPSHOT_STALE_SECONDS,
  analyticsSnapshotPresentation,
  bytesLabel,
  formatLocalDateTime,
  storageStateLabel,
} from "../src/components/admin/mediaStoragePresentation.js";

const component = readFileSync(new URL("../src/components/admin/AdminMediaStoragePanel.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("../src/admin.css", import.meta.url), "utf8");

test("media storage form keeps editable controls and secure field types", () => {
  assert.match(component, /<label><span>存储名称/);
  assert.match(component, /唯一标识（Slug）/);
  assert.match(component, /<span>优先级<\/span><input type="number"/);
  assert.match(component, /R2 存储桶名称（Bucket）/);
  assert.match(component, /R2 接口地址（Endpoint）/);
  assert.match(component, /公共访问地址（Public URL）/);
  assert.match(component, /访问密钥 ID（Access Key ID）/);
  assert.match(component, /访问密钥（Secret Access Key）<\/span><input type="password"/);
  assert.match(component, /Analytics API 令牌<\/span><input type="password"/);
  assert.match(component, /保存存储配置/);
  assert.match(component, /select disabled=\{Boolean\(form\.id\)\}/);
});

test("media storage form has an explicit admin input visual contract", () => {
  const storageRule = styles.match(/\.admin-storage-fields :is\(input, select\) \{[\s\S]*?\n\}/)?.[0] || "";
  assert.match(storageRule, /min-height: var\(--admin-control-height\)/);
  assert.match(storageRule, /border:/);
  assert.match(storageRule, /background: var\(--admin-input-bg\)/);
  assert.match(storageRule, /padding:/);
  assert.match(storageRule, /color: var\(--admin-color-text\)/);
  assert.match(styles, /\.admin-storage-fields :is\(input, select\):focus/);
  assert.match(styles, /\.admin-storage-fields select:disabled/);
  assert.match(styles, /\.admin-storage-fields :is\(input, select\)::placeholder/);
});

test("media storage admin labels use Chinese display text", () => {
  assert.doesNotMatch(component, /<span>Priority<\/span>/);
  assert.doesNotMatch(component, /<span>Managed<\/span>/);
  assert.doesNotMatch(component, /<span>Objects<\/span>/);
  assert.doesNotMatch(component, /<span>Disk free/);
  assert.doesNotMatch(component, /<span>Account usage<\/span>/);
  assert.doesNotMatch(component, /<span>Write limit/);
  assert.match(component, /数字越小优先级越高/);
  assert.match(component, /已配置的密钥不会回显/);
});

test("storage machine states use the backend contract with a future-safe fallback", () => {
  assert.equal(storageStateLabel("AVAILABLE"), "可用");
  assert.equal(storageStateLabel("WARNING"), "容量预警");
  assert.equal(storageStateLabel("OFFLINE"), "离线");
  assert.equal(storageStateLabel("WRITE_BLOCKED"), "已停止新写入");
  assert.equal(storageStateLabel("DISABLED"), "已停用");
  assert.equal(storageStateLabel("FUTURE_STATUS"), "未知（FUTURE_STATUS）");
});

test("usage presentation distinguishes no data from an exact zero", () => {
  assert.equal(bytesLabel(null), "暂无统计数据");
  assert.equal(bytesLabel(0), "0.00 GB");
  assert.equal(analyticsSnapshotPresentation({ usage_refreshed_at: null, usage: { actual_bytes: null } }).status, "NO_DATA");
  assert.equal(analyticsSnapshotPresentation({
    usage_refreshed_at: "2026-08-10T05:31:46.255363Z",
    usage: { actual_bytes: 0, snapshot_age_seconds: 0 },
  }).status, "FRESH");
});

test("analytics snapshot becomes stale after two hours", () => {
  const stale = analyticsSnapshotPresentation({
    usage_refreshed_at: "2026-08-10T03:00:00Z",
    usage: { actual_bytes: 10, snapshot_age_seconds: SNAPSHOT_STALE_SECONDS },
  });
  assert.equal(stale.status, "STALE");
  assert.equal(stale.label, "快照已过期");
});

test("sync timestamps use the browser local timezone without a fixed Shanghai offset", () => {
  const timestamp = "2026-08-10T05:31:46.255363Z";
  const expected = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZoneName: "short",
  }).format(new Date(timestamp));
  assert.equal(formatLocalDateTime(timestamp, "zh-CN"), expected);
  assert.doesNotMatch(readFileSync(new URL("../src/components/admin/mediaStoragePresentation.js", import.meta.url), "utf8"), /Asia\/Shanghai|UTC\+8|\+08:00/);
});

test("R2 card separates Analytics from S3 writes and keeps refresh feedback", () => {
  assert.match(component, /账户级上限<\/dt><dd>\{item\.account\.write_limit_bytes == null \? "未设置"/);
  assert.doesNotMatch(component, /\/ 无限制/);
  assert.match(component, /Analytics 状态/);
  assert.match(component, /最近同步/);
  assert.match(component, /S3 写入连接/);
  assert.match(component, /Analytics 查询/);
  assert.match(component, /refreshFeedback/);
  assert.match(component, /\{ \.\.\.candidate, \.\.\.(?:data|response)\.storage \}/);
  assert.match(component, /正在刷新\.\.\./);
});
