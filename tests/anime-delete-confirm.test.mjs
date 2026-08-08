import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const modalSource = readFileSync(new URL("../src/components/AnimeModal.jsx", import.meta.url), "utf8");

test("matches the reference delete confirmation warning", () => {
  assert.match(modalSource, /确定删除\$\{quotedTitle\}的私人追番记录吗？/);
  assert.match(modalSource, /如果该作品已提交或已入选精选专栏，对应的精选专栏内容也会同步删除。/);
  assert.match(modalSource, /此操作不可恢复。/);
  assert.match(modalSource, /\^《\.\*》\$/);
  assert.match(modalSource, /window\.confirm\(buildDeleteConfirmMessage\(draft\.title\)\)/);
});
