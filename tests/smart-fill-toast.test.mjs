import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

test("keeps the Bangumi smart-fill toast compact at the bottom right", async () => {
  const css = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(css, /\.dashboard-smart-fill-toast\s*\{[^}]*right:\s*16px;[^}]*bottom:\s*16px;[^}]*width:\s*min\(520px/);
  assert.match(css, /\.dashboard-smart-fill-toast\s*\{[^}]*min-height:\s*72px;[^}]*border:\s*3px/);
  assert.match(css, /\.dashboard-smart-fill-toast__icon\s*\{[^}]*width:\s*38px;[^}]*height:\s*38px/);
  assert.match(css, /\.dashboard-smart-fill-toast__close\s*\{[^}]*width:\s*30px;[^}]*height:\s*30px/);
});
