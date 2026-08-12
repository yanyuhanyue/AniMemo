import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";


const validator = readFileSync(new URL("../scripts/validate-plugins.mjs", import.meta.url), "utf8");


test("plugin source validation ignores only named managed runtime directories", () => {
  for (const directory of [".locks", "packages", "previews", "runtime", "staging"]) {
    assert.match(validator, new RegExp(`managedRuntimeDirectories[\\s\\S]*["']${directory.replace(".", "\\.")}["']`));
  }
  assert.match(validator, /!managedRuntimeDirectories\.has\(entry\.name\)/);
  assert.doesNotMatch(validator, /existsSync\([^\n]*manifest\.json[^\n]*\)\)\s*\.map/);
});
