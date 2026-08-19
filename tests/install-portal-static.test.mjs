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
  assert.match(html, /curl -fsSLo \/tmp\/animemo-install\.sh https:\/\/install\.animemo\.cc\/install\.sh/);
  assert.match(html, /<code id="run-command">sudo sh \/tmp\/animemo-install\.sh<\/code>/);
  assert.match(html, /BLOCKED_PORTABLE_PUBLICATION_AUTHORITY/);
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

test("薄 bootstrap 只接受闭合 transport 参数并拒绝 portable production activation", async () => {
  const shell = await source("install.sh");
  assert.match(shell, /--source/);
  assert.match(shell, /github\|official-mirror\|local-bundle/);
  assert.match(shell, /BLOCKED_PORTABLE_PUBLICATION_AUTHORITY/);
  assert.match(shell, /mktemp/);
  assert.match(shell, /trap .*EXIT/);
  assert.match(shell, /gh attestation verify/);
  assert.match(shell, /OFFICIAL_MIRROR_ROOT="https:\/\/download\.animemo\.app\/github\/yanyuhanyue\/AniMemo\/releases"/);
  assert.match(shell, /\/dev\/tty/);
  assert.match(shell, /main\(\) \{/);
  assert.match(shell, /\nmain "\$@"\s*$/);
  assert.doesNotMatch(shell, /curl[^\n]*\|[^\n]*(?:sh|bash)/);
  assert.doesNotMatch(shell, /eval\s|source\s+\$|\.\s+\$/);
});
