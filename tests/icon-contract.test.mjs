import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("Font Awesome icon sizing is bundled under production CSP", () => {
  const icon = read("../src/components/Icon.jsx");
  const styles = read("../src/styles.css");

  assert.match(icon, /@fortawesome\/fontawesome-svg-core\/styles\.css/);
  assert.match(icon, /config\.autoAddCss\s*=\s*false/);
  assert.match(styles, /\.svg-inline--fa\s*\{[^}]*flex-shrink:\s*0/s);
  assert.doesNotMatch(styles, /(?:^|[},])\s*svg\s*\{[^}]*width:\s*100%/m);
});

test("production distinguishes immutable hashed assets from SPA HTML", () => {
  const nginx = read("../deploy/nginx.conf");
  const vite = read("../vite.config.mjs");

  assert.match(nginx, /\/assets\/.*public, max-age=31536000, immutable/s);
  assert.match(nginx, /default\s+"no-cache"/);
  assert.match(vite, /emptyOutDir:\s*true/);
});
