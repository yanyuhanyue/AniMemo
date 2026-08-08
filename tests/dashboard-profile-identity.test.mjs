import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const dashboard = [
  "../src/pages/DashboardPage.jsx",
  "../src/pages/DashboardDialogs.jsx",
].map((path) => readFileSync(new URL(path, import.meta.url), "utf8")).join("\n");

test("uses nickname as the primary profile identity everywhere", () => {
  assert.doesNotMatch(dashboard, /settings\.email\s*\|\|\s*settings\.nickname/);
  assert.match(dashboard, /draft\.nickname\.trim\(\)\s*\|\|\s*settings\.email\s*\|\|\s*"未设置昵称"/);
  assert.match(dashboard, /\$\{settings\.nickname\.trim\(\)\s*\|\|\s*settings\.email\s*\|\|\s*"当前账户"\}的番剧手账房/);
});
