import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const srcRoot = join(root, "src");

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return [".js", ".jsx"].includes(extname(entry.name)) ? [path] : [];
  });
}

test("active browser identities use the AniMemo namespace", () => {
  const matches = sourceFiles(srcRoot).flatMap((path) => {
    const source = readFileSync(path, "utf8");
    return [...source.matchAll(/ANIME_JOURNAL_|anime_journal|anime-journal|(?:img\.|media\.)?re-anime\.cc/g)]
      .map((match) => ({ file: relative(root, path).replaceAll("\\", "/"), value: match[0] }));
  });

  assert.deepEqual(matches, [
    { file: "src/lib/webAuthAdapter.js", value: "anime_journal" },
    { file: "src/lib/webAuthAdapter.js", value: "anime_journal" },
  ]);

  const adapter = readFileSync(join(srcRoot, "lib/webAuthAdapter.js"), "utf8");
  assert.match(adapter, /Security denylist: remove obsolete browser-stored tokens without accepting them/);
  assert.match(adapter, /removeItem\(INSECURE_LEGACY_ACCESS_KEY\)/);
  assert.match(adapter, /removeItem\(INSECURE_LEGACY_REFRESH_KEY\)/);
  assert.doesNotMatch(adapter, /getItem\(INSECURE_LEGACY/);
});

test("browser runtime, events and demo storage use AniMemo identities", () => {
  const main = readFileSync(join(srcRoot, "main.jsx"), "utf8");
  const dashboardData = readFileSync(join(srcRoot, "pages/dashboardData.js"), "utf8");
  const showcase = readFileSync(join(srcRoot, "pages/ShowcasePage.jsx"), "utf8");
  const pluginRuntime = readFileSync(join(srcRoot, "plugins/sdk/PluginRuntimeContext.jsx"), "utf8");

  assert.match(main, /globalThis\.__ANIMEMO_REACT_RUNTIME__/);
  assert.match(dashboardData, /"animemo_records_v1"/);
  assert.match(dashboardData, /"animemo_settings_v1"/);
  assert.match(dashboardData, /"animemo_quick_filters_v1"/);
  assert.match(showcase, /"animemo:records-updated"/);
  assert.match(pluginRuntime, /"animemo:plugins-changed"/);
  assert.match(pluginRuntime, /"animemo:site-settings-updated"/);
});
