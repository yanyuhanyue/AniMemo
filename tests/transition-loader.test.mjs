import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("startup no longer renders the standalone square spinner", () => {
  const app = read("../src/App.jsx");
  const styles = read("../src/styles.css");

  assert.doesNotMatch(app, /app-auth-bootstrap[^\n]*<span\s*\/>/);
  assert.doesNotMatch(styles, /app-auth-spin/);
  assert.doesNotMatch(styles, /\.app-auth-bootstrap\s+span/);
});

test("page transitions retain the three-color and progress-bar implementations", () => {
  const transition = read("../src/components/PageColorTransition.jsx");
  const styles = read("../src/styles.css");

  assert.match(transition, /page-color-transition/);
  assert.match(transition, /page-progress-transition/);
  assert.match(transition, /app-boot-loader__rail/);
  assert.match(styles, /\.page-color-transition/);
  assert.match(styles, /\.page-progress-transition/);
  assert.match(styles, /\.app-boot-loader__rail/);
});
