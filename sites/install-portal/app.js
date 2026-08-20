import { releaseState } from "./release-state.mjs";

export const RELEASE_AUTHORITY = "GITHUB_IMMUTABLE_RELEASE";
export const RELEASE_STATE_SCHEMA = "animemo.install-portal.release-state/v1";

const TOP_LEVEL_KEYS = Object.freeze([
  "authorityReceiptSha256",
  "officialMirror",
  "release",
  "schema",
  "state",
]);
const RELEASE_KEYS = Object.freeze([
  "assets",
  "commitSha",
  "draft",
  "githubReleaseId",
  "ghcr",
  "immutable",
  "prerelease",
  "publishedAt",
  "repository",
  "tag",
]);
const ASSET_SET_KEYS = Object.freeze([
  "attestation",
  "checksums",
  "deploymentContract",
  "installer",
  "manifest",
]);
const ASSET_KEYS = Object.freeze(["name", "sha256"]);
const GHCR_KEYS = Object.freeze(["api", "web"]);
const MIRROR_KEYS = Object.freeze(["baseUrl", "installerPath", "qualified"]);
const SHA256_PATTERN = /^sha256:[0-9a-f]{64}$/;
const COMMIT_PATTERN = /^[0-9a-f]{40}$/;
const TAG_PATTERN = /^v\d+\.\d+\.\d+(?:-rc\.\d+)?$/;
const OCI_PATTERN = /^ghcr\.io\/yanyuhanyue\/animemo-(?:api|web)@sha256:[0-9a-f]{64}$/;

function requireObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value;
}

function requireExactKeys(value, expected, label) {
  const actual = Object.keys(requireObject(value, label)).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    throw new TypeError(`${label} has missing or unknown fields`);
  }
}

function requireString(value, pattern, label) {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new TypeError(`${label} is invalid`);
  }
}

function validateAsset(asset, label) {
  requireExactKeys(asset, ASSET_KEYS, label);
  requireString(asset.name, /^[A-Za-z0-9][A-Za-z0-9._-]*$/, `${label}.name`);
  requireString(asset.sha256, SHA256_PATTERN, `${label}.sha256`);
}

function validateRelease(release) {
  requireExactKeys(release, RELEASE_KEYS, "release");
  if (!Number.isSafeInteger(release.githubReleaseId) || release.githubReleaseId <= 0) {
    throw new TypeError("release.githubReleaseId is invalid");
  }
  requireString(release.tag, TAG_PATTERN, "release.tag");
  requireString(release.commitSha, COMMIT_PATTERN, "release.commitSha");
  if (release.repository !== "yanyuhanyue/AniMemo") {
    throw new TypeError("release.repository is not canonical");
  }
  if (release.immutable !== true || release.draft !== false) {
    throw new TypeError("release must be immutable and published, never draft");
  }
  if (typeof release.prerelease !== "boolean") {
    throw new TypeError("release.prerelease must be boolean");
  }
  if (typeof release.publishedAt !== "string" || Number.isNaN(Date.parse(release.publishedAt))) {
    throw new TypeError("release.publishedAt is invalid");
  }
  requireExactKeys(release.assets, ASSET_SET_KEYS, "release.assets");
  for (const key of ASSET_SET_KEYS) validateAsset(release.assets[key], `release.assets.${key}`);
  const assetNames = ASSET_SET_KEYS.map((key) => release.assets[key].name);
  if (new Set(assetNames).size !== assetNames.length) {
    throw new TypeError("release asset names must be unique");
  }
  requireExactKeys(release.ghcr, GHCR_KEYS, "release.ghcr");
  requireString(release.ghcr.api, OCI_PATTERN, "release.ghcr.api");
  requireString(release.ghcr.web, OCI_PATTERN, "release.ghcr.web");
  if (!release.ghcr.api.includes("/animemo-api@") || !release.ghcr.web.includes("/animemo-web@")) {
    throw new TypeError("release GHCR repositories are not canonical");
  }
}

function validateMirror(mirror, release) {
  if (mirror === null) return;
  requireExactKeys(mirror, MIRROR_KEYS, "officialMirror");
  if (mirror.qualified !== true) throw new TypeError("officialMirror must be qualified");
  let baseUrl;
  try {
    baseUrl = new URL(mirror.baseUrl);
  } catch {
    throw new TypeError("officialMirror.baseUrl is invalid");
  }
  if (baseUrl.protocol !== "https:" || baseUrl.username || baseUrl.password || baseUrl.search || baseUrl.hash) {
    throw new TypeError("officialMirror.baseUrl must be a credential-free HTTPS URL");
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(mirror.installerPath) || mirror.installerPath.includes("..")) {
    throw new TypeError("officialMirror.installerPath is invalid");
  }
  if (!mirror.installerPath.endsWith(release.assets.installer.name)) {
    throw new TypeError("officialMirror must transport the canonical installer asset");
  }
}

export function validateReleaseState(candidate) {
  requireExactKeys(candidate, TOP_LEVEL_KEYS, "release state");
  if (candidate.schema !== RELEASE_STATE_SCHEMA) throw new TypeError("release state schema is unsupported");
  if (candidate.state === "NO_PUBLIC_RELEASE") {
    if (candidate.release !== null || candidate.officialMirror !== null || candidate.authorityReceiptSha256 !== null) {
      throw new TypeError("NO_PUBLIC_RELEASE must not carry release or mirror metadata");
    }
    return Object.freeze({ ...candidate });
  }
  if (candidate.state !== "REAL_VERIFIED_RELEASE") throw new TypeError("release state is fail-closed");
  requireString(candidate.authorityReceiptSha256, SHA256_PATTERN, "authorityReceiptSha256");
  validateRelease(candidate.release);
  validateMirror(candidate.officialMirror, candidate.release);
  return Object.freeze({ ...candidate });
}

function shellSingleQuote(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`;
}

export function createTransportPlan(candidate, transport) {
  const state = validateReleaseState(candidate);
  if (state.state !== "REAL_VERIFIED_RELEASE") {
    return Object.freeze({
      acquisitionCommand: null,
      authority: RELEASE_AUTHORITY,
      available: false,
      reason: "暂无可安装的正式 Release",
      transport,
      verificationCommands: Object.freeze([]),
    });
  }

  const { release } = state;
  const stageDirectory = "./animemo-stage0";
  const installerPath = `${stageDirectory}/${release.assets.installer.name}`;
  const installerSha256 = release.assets.installer.sha256.slice("sha256:".length);
  const verificationCommands = Object.freeze([
    `gh release verify ${release.tag} --repo ${release.repository}`,
    `gh release verify-asset ${release.tag} ${shellSingleQuote(installerPath)} --repo ${release.repository}`,
    `printf '%s  %s\\n' ${shellSingleQuote(installerSha256)} ${shellSingleQuote(installerPath)} | sha256sum --check --strict -`,
  ]);
  const prepareStage = `install -d -m 0700 ${shellSingleQuote(stageDirectory)}`;

  if (transport === "github") {
    return Object.freeze({
      acquisitionCommand: `${prepareStage} && gh release download ${release.tag} --repo ${release.repository} --pattern ${shellSingleQuote(release.assets.installer.name)} --dir ${shellSingleQuote(stageDirectory)}`,
      authority: RELEASE_AUTHORITY,
      available: true,
      reason: null,
      transport,
      verificationCommands,
    });
  }
  if (transport === "official-mirror") {
    if (state.officialMirror === null) {
      return Object.freeze({
        acquisitionCommand: null,
        authority: RELEASE_AUTHORITY,
        available: false,
        reason: "当前没有已资格认证的 Official Mirror",
        transport,
        verificationCommands,
      });
    }
    const baseUrl = `${state.officialMirror.baseUrl.replace(/\/$/, "")}/`;
    const assetUrl = new URL(state.officialMirror.installerPath, baseUrl);
    return Object.freeze({
      acquisitionCommand: `${prepareStage} && curl --fail --location --proto '=https' --tlsv1.2 --output ${shellSingleQuote(installerPath)} ${shellSingleQuote(assetUrl.href)}`,
      authority: RELEASE_AUTHORITY,
      available: true,
      reason: null,
      transport,
      verificationCommands,
    });
  }
  if (transport === "local-bundle") {
    return Object.freeze({
      acquisitionCommand: null,
      authority: "OFFLINE_PRETRUSTED_VERIFICATION",
      available: false,
      reason: "Local Bundle 必须由 operator 独立预置信任，门户不生成可执行导入命令",
      transport,
      verificationCommands: Object.freeze([]),
    });
  }
  throw new TypeError("unknown transport");
}

function setCopyCommands(plan) {
  const commands = [
    plan.verificationCommands[0] ?? "发布身份尚未获得资格认证。",
    plan.acquisitionCommand ?? "没有可下载的正式安装材料。",
    plan.verificationCommands.slice(1).join(" && ") || "验证命令将在正式 Release 获得完整证明后生成。",
  ];
  for (const [index, id] of ["release-command", "download-command", "run-command"].entries()) {
    const code = document.getElementById(id);
    const button = document.querySelector(`[data-copy-target="${id}"]`);
    if (code) code.textContent = commands[index];
    if (button) button.disabled = !plan.available;
  }
}

function renderPortal() {
  let state;
  try {
    state = validateReleaseState(releaseState);
  } catch {
    state = validateReleaseState({
      authorityReceiptSha256: null,
      officialMirror: null,
      release: null,
      schema: RELEASE_STATE_SCHEMA,
      state: "NO_PUBLIC_RELEASE",
    });
  }

  const releaseStatus = document.querySelector("#release-state");
  const sourceStatus = document.querySelector("#source-status");
  const railName = document.querySelector("#rail-transport-name");
  const copyStatus = document.querySelector("#copy-status");
  const buttons = [...document.querySelectorAll("[data-transport]")];
  const githubPlan = createTransportPlan(state, "github");

  if (state.state === "NO_PUBLIC_RELEASE") {
    releaseStatus.textContent = "暂无可安装的正式 Release";
    sourceStatus.textContent = "暂无正式 Release；所有运输来源均保持不可用，且不会自动回退。";
    for (const button of buttons) button.disabled = true;
    setCopyCommands(githubPlan);
  } else {
    releaseStatus.textContent = `已验证正式 Release：${state.release.tag}`;
    releaseStatus.classList.remove("is-unavailable");
    releaseStatus.classList.add("is-verified");
    const githubButton = document.querySelector('[data-transport="github"]');
    const mirrorButton = document.querySelector('[data-transport="official-mirror"]');
    githubButton.disabled = false;
    mirrorButton.disabled = state.officialMirror === null;
    sourceStatus.textContent = "当前选择：GitHub。不会自动回退到其他运输来源。";
    setCopyCommands(githubPlan);
  }

  for (const button of buttons) {
    button.addEventListener("click", () => {
      if (button.disabled) return;
      const plan = createTransportPlan(state, button.dataset.transport);
      if (!plan.available) return;
      for (const candidate of buttons) {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", String(active));
      }
      setCopyCommands(plan);
      railName.textContent = plan.transport === "github" ? "GitHub transport" : "Official Mirror transport";
      sourceStatus.textContent = plan.transport === "github"
        ? "当前选择：GitHub。不会自动回退到其他运输来源。"
        : "当前选择：AniMemo Official Mirror。GitHub Immutable Release 仍是唯一在线发布权威；镜像失败时不会静默回退。";
    });
  }

  for (const button of document.querySelectorAll("[data-copy-target]")) {
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      const target = document.getElementById(button.dataset.copyTarget);
      if (!target) return;
      try {
        await navigator.clipboard.writeText(target.textContent.trim());
        copyStatus.textContent = "命令已复制";
      } catch {
        copyStatus.textContent = "无法自动复制，请手动选择命令";
      }
      copyStatus.classList.add("is-visible");
      window.setTimeout(() => copyStatus.classList.remove("is-visible"), 1800);
    });
  }
}

if (typeof document !== "undefined") renderPortal();
