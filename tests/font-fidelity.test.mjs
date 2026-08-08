import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("keeps the global Noto Sans SC setup aligned with the reference site", () => {
  for (const weight of [300, 400, 500, 700]) {
    assert.match(styles, new RegExp(`@fontsource/noto-sans-sc/${weight}\\.css`));
  }

  assert.doesNotMatch(styles, /@fontsource\/noto-sans-sc\/(?:800|900)\.css/);
  assert.match(styles, /--font-(?:display|body)-cjk:\s*"Noto Sans SC", sans-serif;/);
  assert.match(styles, /font-synthesis:\s*weight style small-caps;/);
  assert.match(styles, /text-rendering:\s*auto;/);
  assert.doesNotMatch(styles, /body\s*\{[^}]*-webkit-font-smoothing:\s*antialiased;/s);
});
