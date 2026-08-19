import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const portal = path.join(root, "install.animemo.cc");

async function source(name) {
  return readFile(path.join(portal, name), "utf8");
}

test("安装入口完整说明权威、运输、Fresh、升级与恢复路径", async () => {
  const html = await source("index.html");
  for (const phrase of [
    "AniMemo 安装入口",
    "推荐安装",
    "全新安装",
    "升级现有安装",
    "GitHub",
    "AniMemo Official Mirror",
    "Portable / Offline Bundle",
    "Doctor",
    "备份与恢复",
    "Release Authority",
  ]) {
    assert.match(html, new RegExp(phrase));
  }
  assert.match(html, /gh release verify &lt;EXACT_TAG&gt;/);
  assert.match(html, /gh release download/);
  assert.match(html, /gh release verify-asset/);
  assert.doesNotMatch(html, /sudo sh|curl[^<]*install\.sh/);
  assert.match(html, /independently pretrusted verifier/);
});

test("页面来源选择显式且没有隐式地理切换或最快源承诺", async () => {
  const html = await source("index.html");
  const script = await source("app.js");
  const combined = `${html}\n${script}`;
  assert.match(combined, /data-transport="github"/);
  assert.match(combined, /data-transport="official-mirror"/);
  assert.match(combined, /data-transport="local-bundle"/);
  assert.doesNotMatch(combined, /geolocation|country detection|fastest mirror|自动最快|自动切换/i);
  assert.doesNotMatch(script, /fetch\s*\(|XMLHttpRequest|WebSocket|EventSource/);
});

test("页面依赖均为同源静态资源并提供无障碍复制反馈", async () => {
  const html = await source("index.html");
  assert.match(html, /href="\.\/styles\.css"/);
  assert.match(html, /src="\.\/app\.js"/);
  assert.doesNotMatch(html, /(?:src|href)="https?:\/\//);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /data-copy-target=/);
  assert.match(html, /<main\b/);
  assert.match(html, /<nav\b[^>]*aria-label=/);
  assert.doesNotMatch(html, /<button[^>]+role="listitem"/);
});

test("远程 bootstrap 已退役且不再承担任何执行或权威职责", async () => {
  const shell = await source("install.sh");
  assert.match(shell, /REMOTE_BOOTSTRAP_EXECUTION_DISABLED/);
  assert.match(shell, /exit 78/);
  assert.doesNotMatch(shell, /sudo|apt-get|systemctl|docker|curl|gh release|python3|tar\s|eval\s/);
});
