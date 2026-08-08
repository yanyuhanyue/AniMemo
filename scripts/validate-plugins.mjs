import { existsSync, readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(process.cwd(), "plugins");
const slugPattern = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
const idPattern = /^[a-z0-9]+(?:[.-][a-z0-9]+)+$/;
const semverPattern = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$/;
const roles = new Set(["reviewer", "user_manager", "operator", "administrator"]);
const runtimes = new Set(["frontend", "backend"]);
const extensions = new Set(["frontend.page", "frontend.navigation", "backend.api", "settings", "hooks", "storage", "catalog.importer", "catalog.metadata"]);
const hooks = new Set(["registration.before_request", "registration.before_complete", "registration.after_complete", "journal.after_create", "journal.after_update", "journal.after_delete", "column.after_publish", "column.after_delete", "user.after_created", "user.before_delete", "user.after_delete"]);
const userScopedHooks = new Set(["journal.after_create", "journal.after_update", "journal.after_delete", "column.after_publish", "column.after_delete"]);

function requireValue(condition, message, errors) { if (!condition) errors.push(message); }

function validateManifest(directory, manifest, { enforceDirectoryName = true } = {}) {
  const errors = [];
  requireValue(manifest.schemaVersion === 2, "schemaVersion 必须为 2", errors);
  requireValue(manifest.sdkApi === 2, "sdkApi 必须为 2", errors);
  requireValue(idPattern.test(manifest.id || ""), "id 必须是稳定的反向域名形式", errors);
  requireValue(slugPattern.test(manifest.slug || ""), "slug 必须为 kebab-case", errors);
  if (enforceDirectoryName) requireValue(directory === manifest.slug, `目录名必须与 slug 一致：${manifest.slug}`, errors);
  for (const key of ["name", "description", "license"]) requireValue(typeof manifest[key] === "string" && manifest[key].length > 0, `${key} 不能为空`, errors);
  requireValue(typeof manifest.author?.name === "string" && manifest.author.name.length > 0, "author.name 不能为空", errors);
  requireValue(["user", "system"].includes(manifest.installationMode), "installationMode 必须为 user 或 system", errors);
  requireValue(semverPattern.test(manifest.version || ""), "version 必须符合 SemVer", errors);
  requireValue(Array.isArray(manifest.runtimes) && manifest.runtimes.every((item) => runtimes.has(item)), "runtimes 只能声明 frontend/backend", errors);
  requireValue(Array.isArray(manifest.extensions) && manifest.extensions.every((item) => extensions.has(item)), "extensions 包含未知扩展", errors);
  requireValue(!("backendPackage" in manifest || "backendEntrypoint" in manifest || "backendUrls" in manifest || "djangoApp" in manifest || "migrations" in manifest), "Unsupported extended backend capability", errors);
  requireValue(!manifest.extensions?.includes("background.task"), "Unsupported extended backend capability", errors);
  if (manifest.runtimes?.includes("frontend")) {
    requireValue(manifest.extensions.includes("frontend.page") || manifest.extensions.includes("frontend.navigation"), "前端 runtime 必须声明前端扩展", errors);
    requireValue(existsSync(resolve(root, directory, "frontend", "plugin.js")), "前端 runtime 缺少已构建的 frontend/plugin.js", errors);
  }
  if (manifest.runtimes?.includes("backend")) {
    requireValue(manifest.backend?.entry === "backend/plugin.py", "后端 runtime 必须使用 backend/plugin.py entry", errors);
    requireValue(manifest.extensions.includes("backend.api"), "后端 runtime 必须声明 backend.api", errors);
    requireValue(existsSync(resolve(root, directory, "backend", "plugin.py")), "后端 runtime 缺少 backend/plugin.py", errors);
  }
  requireValue(Array.isArray(manifest.permissions), "permissions 必须是数组", errors);
  for (const permission of manifest.permissions || []) {
    requireValue(permission.code?.startsWith(`${manifest.slug}.`), `权限 ${permission.code} 未使用插件命名空间`, errors);
    requireValue(Array.isArray(permission.roles) && permission.roles.every((role) => roles.has(role)), `权限 ${permission.code} 使用了未知角色`, errors);
  }
  requireValue(Array.isArray(manifest.hooks) && manifest.hooks.every((hook) => hooks.has(hook)), "hooks 包含未知 Hook", errors);
  if (manifest.installationMode === "user") requireValue((manifest.hooks || []).every((hook) => userScopedHooks.has(hook)), "USER 插件只能声明 journal/column 用户范围 Hook", errors);
  requireValue(Array.isArray(manifest.settings), "settings 必须是数组", errors);
  for (const setting of manifest.settings || []) {
    requireValue(["user", "system"].includes(setting.scope), `配置 ${setting.key || "未命名"} 必须声明 scope=user 或 scope=system`, errors);
  }
  const policy = manifest.dataPolicy;
  for (const key of ["storesPersonalData", "usesExternalNetwork", "acceptsFileUploads", "retainsDataOnDisable"]) requireValue(typeof policy?.[key] === "boolean", `dataPolicy.${key} 必须是布尔值`, errors);
  return errors;
}

const pluginDirectories = readdirSync(root, { withFileTypes: true }).filter((entry) => entry.isDirectory() && !entry.name.startsWith("_")).map((entry) => entry.name);
const candidates = [...pluginDirectories.map((directory) => ({ directory, enforceDirectoryName: true })), ...(existsSync(resolve(root, "_template", "manifest.json")) ? [{ directory: "_template", enforceDirectoryName: false }] : [])];
const failures = [];
for (const { directory, enforceDirectoryName } of candidates) {
  const manifestPath = resolve(root, directory, "manifest.json");
  try {
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
    for (const error of validateManifest(directory, manifest, { enforceDirectoryName })) failures.push(`${directory}: ${error}`);
  } catch (error) { failures.push(`${directory}: manifest.json 无法解析：${error.message}`); }
}
if (failures.length) { console.error("Plugin v2 validation failed:\n\n" + failures.map((item) => `- ${item}`).join("\n")); process.exit(1); }
console.log(`Plugin SDK v2 validation passed (${pluginDirectories.length} installed plugin(s), template checked).`);
