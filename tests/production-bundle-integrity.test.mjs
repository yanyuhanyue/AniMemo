import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const clientDirectory = fileURLToPath(new URL("../dist/client/", import.meta.url));
const indexPath = path.join(clientDirectory, "index.html");

function resolveAsset(reference, parentPath = indexPath) {
  if (reference.startsWith("/assets/")) return path.join(clientDirectory, reference.slice(1));
  if (reference.startsWith("assets/")) return path.join(clientDirectory, reference);
  if (reference.startsWith("./")) return path.resolve(path.dirname(parentPath), reference);
  return null;
}

function collectCurrentJavaScript() {
  const index = readFileSync(indexPath, "utf8");
  const pending = [...index.matchAll(/(?:src|href)=["']([^"']+\.js)["']/g)].map((match) => resolveAsset(match[1]));
  const visited = new Set();
  const sources = [];

  while (pending.length) {
    const assetPath = pending.pop();
    if (!assetPath || visited.has(assetPath) || !existsSync(assetPath)) continue;
    visited.add(assetPath);
    const source = readFileSync(assetPath, "utf8");
    sources.push(source);
    for (const match of source.matchAll(/["']([^"']+\.js)["']/g)) {
      const dependency = resolveAsset(match[1], assetPath);
      if (dependency && !visited.has(dependency)) pending.push(dependency);
    }
  }

  return sources.join("\n");
}

test("production JavaScript excludes development demonstration records", () => {
  assert.equal(existsSync(indexPath), true, "run npm run build before the site test suite");
  const javascript = collectCurrentJavaScript();

  assert.doesNotMatch(javascript, /一叠间漫画咖啡屋生活/);
  assert.doesNotMatch(javascript, /本地演示：注册信息已校验/);
  assert.doesNotMatch(javascript, /demo-rabbit/);
});
