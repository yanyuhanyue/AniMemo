import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

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
