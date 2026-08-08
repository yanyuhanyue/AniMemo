import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const community = readFileSync(new URL("../src/pages/CommunityPages.jsx", import.meta.url), "utf8");
const icon = readFileSync(new URL("../src/components/Icon.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("matches the public-universe owner count badge anatomy", () => {
  assert.match(community, /className="universe-owner-count"><Icon name="users-viewfinder" \/>/);
  assert.match(icon, /faUsersViewfinder/);
  assert.match(icon, /"users-viewfinder": faUsersViewfinder/);

  const rule = styles.match(/\.universe-owner-count\s*\{([^}]*)\}/)?.[1] || "";
  assert.match(rule, /display:\s*inline-flex/);
  assert.match(rule, /border:\s*4px solid var\(--ink\)/);
  assert.match(rule, /padding:\s*8px 16px/);
  assert.match(rule, /box-shadow:\s*4px 4px 0 var\(--ink\)/);
  assert.match(rule, /font-size:\s*14px/);
  assert.match(rule, /line-height:\s*20px/);
  assert.doesNotMatch(rule, /transform:/);
});

test("keeps the reference badge scale on narrow screens", () => {
  const mobileRules = styles.match(/@media \(max-width: 720px\)[\s\S]*$/)?.[0] || "";
  const mobileBadgeRule = mobileRules.match(/\.universe-owner-count\s*\{([^}]*)\}/)?.[1] || "";

  assert.doesNotMatch(mobileBadgeRule, /font-size:/);
  assert.doesNotMatch(mobileBadgeRule, /padding:/);
});
