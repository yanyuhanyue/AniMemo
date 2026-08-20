import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

import {
  createTransportPlan,
  RELEASE_AUTHORITY,
  RELEASE_STATE_SCHEMA,
  validateReleaseState,
} from "../sites/install-portal/app.js";
import { releaseState } from "../sites/install-portal/release-state.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const portal = path.join(root, "sites", "install-portal");

async function source(name) {
  return readFile(path.join(portal, name), "utf8");
}

function digest(character) {
  return `sha256:${character.repeat(64)}`;
}

function verifiedReleaseState() {
  return {
    authorityReceiptSha256: digest("a"),
    officialMirror: {
      baseUrl: "https://mirror.invalid.example/animemo/releases/",
      installerPath: "qualified/v9.9.9-rc.1/installer-materials.tar",
      qualified: true,
    },
    release: {
      assets: {
        attestation: { name: "release-attestation.json", sha256: digest("b") },
        checksums: { name: "SHA256SUMS", sha256: digest("c") },
        deploymentContract: { name: "deployment-contract.json", sha256: digest("d") },
        installer: { name: "installer-materials.tar", sha256: digest("e") },
        manifest: { name: "manifest.json", sha256: digest("f") },
      },
      commitSha: "1".repeat(40),
      draft: false,
      githubReleaseId: 999999,
      ghcr: {
        api: `ghcr.io/yanyuhanyue/animemo-api@${digest("2")}`,
        web: `ghcr.io/yanyuhanyue/animemo-web@${digest("3")}`,
      },
      immutable: true,
      prerelease: true,
      publishedAt: "2030-01-01T00:00:00Z",
      repository: "yanyuhanyue/AniMemo",
      tag: "v9.9.9-rc.1",
    },
    schema: RELEASE_STATE_SCHEMA,
    state: "REAL_VERIFIED_RELEASE",
  };
}

test("默认页面严格呈现 NO_PUBLIC_RELEASE 并禁用可执行命令", async () => {
  const html = await source("index.html");
  const state = validateReleaseState(releaseState);
  assert.equal(state.state, "NO_PUBLIC_RELEASE");
  assert.match(html, /暂无可安装的正式 Release/);
  assert.doesNotMatch(html, /&lt;EXACT_TAG&gt;|<EXACT_TAG>|<ASSET>|gh release download/);
  assert.equal((html.match(/data-copy-target=/g) ?? []).length, 3);
  assert.equal((html.match(/data-copy-target="[^"]+" disabled/g) ?? []).length, 3);
  assert.match(html, /data-transport="official-mirror"[^>]+disabled/);
  assert.match(html, /data-transport="local-bundle"[^>]+disabled/);
});

test("闭合 schema 对缺字段、额外字段、draft 与非 immutable Release 失败关闭", () => {
  const missing = verifiedReleaseState();
  delete missing.release.commitSha;
  assert.throws(() => validateReleaseState(missing), /missing or unknown fields/);

  const extra = verifiedReleaseState();
  extra.runtimeLatest = true;
  assert.throws(() => validateReleaseState(extra), /missing or unknown fields/);

  const draft = verifiedReleaseState();
  draft.release.draft = true;
  assert.throws(() => validateReleaseState(draft), /never draft/);

  const mutable = verifiedReleaseState();
  mutable.release.immutable = false;
  assert.throws(() => validateReleaseState(mutable), /must be immutable/);
});

test("GitHub 与已资格镜像 acquisition 确实不同但验证命令和 authority 完全相同", () => {
  const state = verifiedReleaseState();
  const github = createTransportPlan(state, "github");
  const mirror = createTransportPlan(state, "official-mirror");
  assert.equal(github.available, true);
  assert.equal(mirror.available, true);
  assert.notEqual(github.acquisitionCommand, mirror.acquisitionCommand);
  assert.match(github.acquisitionCommand, /^install -d -m 0700 .* && gh release download /);
  assert.match(mirror.acquisitionCommand, /^install -d -m 0700 .* && curl --fail --location /);
  assert.deepEqual(github.verificationCommands, mirror.verificationCommands);
  assert.match(github.verificationCommands.at(-1), /sha256sum --check --strict -/);
  assert.doesNotMatch(github.verificationCommands.at(-1), /checksums\.txt/);
  assert.equal(github.authority, RELEASE_AUTHORITY);
  assert.equal(mirror.authority, RELEASE_AUTHORITY);
  assert.equal(mirror.authority, "GITHUB_IMMUTABLE_RELEASE");
});

test("未资格镜像与 Local Bundle 不会获得在线 Release Authority fallback", () => {
  const withoutMirror = verifiedReleaseState();
  withoutMirror.officialMirror = null;
  const mirror = createTransportPlan(withoutMirror, "official-mirror");
  const local = createTransportPlan(withoutMirror, "local-bundle");
  assert.equal(mirror.available, false);
  assert.equal(mirror.acquisitionCommand, null);
  assert.equal(mirror.authority, RELEASE_AUTHORITY);
  assert.equal(local.available, false);
  assert.equal(local.acquisitionCommand, null);
  assert.equal(local.authority, "OFFLINE_PRETRUSTED_VERIFICATION");
  assert.match(local.reason, /独立预置信任/);
});

test("页面来源选择显式且没有 runtime latest、隐式切换或外部 fetch", async () => {
  const html = await source("index.html");
  const script = await source("app.js");
  const combined = `${html}\n${script}`;
  assert.match(combined, /data-transport="github"/);
  assert.match(combined, /data-transport="official-mirror"/);
  assert.match(combined, /data-transport="local-bundle"/);
  assert.doesNotMatch(combined, /geolocation|country detection|fastest mirror|自动最快|自动切换/i);
  assert.doesNotMatch(script, /fetch\s*\(|XMLHttpRequest|WebSocket|EventSource|releases\/latest/i);
});

test("页面依赖均为同源静态资源并提供无障碍反馈", async () => {
  const html = await source("index.html");
  assert.match(html, /href="\.\/styles\.css"/);
  assert.match(html, /type="module" src="\.\/app\.js"/);
  assert.doesNotMatch(html, /(?:src|href)="https?:\/\//);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /<main\b/);
  assert.match(html, /<nav\b[^>]*aria-label=/);
});

test("Cloudflare Pages headers 建立最小同源静态安全合同", async () => {
  const headers = await source("_headers");
  for (const directive of [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "connect-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'none'",
    "X-Content-Type-Options: nosniff",
    "Referrer-Policy: no-referrer",
    "Permissions-Policy:",
    "Cross-Origin-Opener-Policy: same-origin",
    "Cache-Control: no-store",
  ]) assert.match(headers, new RegExp(directive.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.doesNotMatch(headers, /unsafe-eval|script-src[^\n]*https:|style-src[^\n]*https:|connect-src[^\n]*\*/);
});

test("源码身份与公开域名解耦且 remote bootstrap 保持 fail closed", async () => {
  const readme = await source("README.md");
  const shell = await source("install.sh");
  for (const line of [
    "COMPONENT_ID=animemo-install-portal",
    "SOURCE_DIRECTORY=sites/install-portal",
    "DEFAULT_PUBLIC_ORIGIN=https://install.animemo.cc",
    "SOURCE_DIRECTORY_DOMAIN_INDEPENDENT=YES",
    "RELEASE_AUTHORITY=GITHUB_IMMUTABLE_RELEASE",
    "PORTAL_ROLE=BOOTSTRAP_TRANSPORT_AND_INSTALLATION_UX",
    "PORTAL_IS_RELEASE_AUTHORITY=NO",
  ]) assert.match(readme, new RegExp(line.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.match(shell, /https:\/\/install\.animemo\.cc\//);
  assert.match(shell, /REMOTE_BOOTSTRAP_EXECUTION_DISABLED/);
  assert.match(shell, /exit 78/);
  assert.doesNotMatch(shell, /sudo|apt-get|systemctl|docker|curl|gh release|python3|tar\s|eval\s/);
});
