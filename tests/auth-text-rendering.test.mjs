import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const transition = readFileSync(new URL("../src/components/auth/AuthModeTransition.jsx", import.meta.url), "utf8");

test("does not keep the auth form text on a permanent opacity layer", () => {
  const contentRule = styles.match(/\.auth-mode-content\s*\{([^}]*)\}/)?.[1] || "";

  assert.doesNotMatch(contentRule, /will-change:\s*opacity/);
  assert.doesNotMatch(transition, /gsap\.set\(content,\s*\{\s*clearProps:\s*"transform",\s*opacity:\s*1\s*\}\)/);
  assert.match(transition, /gsap\.set\(content,\s*\{\s*clearProps:\s*"transform,opacity"\s*\}\)/);
});
