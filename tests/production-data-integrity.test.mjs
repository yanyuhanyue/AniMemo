import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const dashboard = [
  "../src/pages/DashboardPage.jsx",
  "../src/pages/useDashboardData.js",
].map((path) => readFileSync(new URL(path, import.meta.url), "utf8")).join("\n");
const showcase = readFileSync(new URL("../src/pages/ShowcasePage.jsx", import.meta.url), "utf8");
const community = readFileSync(new URL("../src/pages/CommunityPages.jsx", import.meta.url), "utf8");
const featured = readFileSync(new URL("../src/pages/FeaturedPage.jsx", import.meta.url), "utf8");
const auth = readFileSync(new URL("../src/pages/UserAuthPage.jsx", import.meta.url), "utf8");
const heroArt = [
  "../src/components/UniverseHeroArt.jsx",
  "../src/components/featured/FeaturedHero.jsx",
].map((path) => readFileSync(new URL(path, import.meta.url), "utf8")).join("\n");
const viteConfig = readFileSync(new URL("../vite.config.mjs", import.meta.url), "utf8");
const productionDemoData = readFileSync(new URL("../src/data/demoData.production.js", import.meta.url), "utf8");

test("all administrator status chips stay content-sized", () => {
  const statusRule = styles.match(/\.admin-status\s*\{([^}]*)\}/)?.[1] || "";

  assert.match(statusRule, /width:\s*max-content/);
  assert.match(statusRule, /min-width:\s*0/);
  assert.match(statusRule, /flex:\s*0\s+0\s+auto/);
  assert.match(statusRule, /justify-self:\s*end/);
});

test("production routes never substitute local demonstration data", () => {
  for (const source of [dashboard, showcase, community, featured, auth]) {
    assert.match(source, /from "@demo-data"/);
    assert.doesNotMatch(source, /import\.meta\.env\.DEV/);
    assert.doesNotMatch(source, /data\/(?:anime|universe|featuredColumns)\.js/);
  }
  assert.match(dashboard, /const isDemo = demoEnabled\s*&&/);
  assert.doesNotMatch(dashboard, /^import .*animeRecords.*data\/anime/m);
  assert.match(dashboard, /catalogRecords=\{demoCatalogRecords\}/);
  assert.match(dashboard, /if \(!demoEnabled && !access\) navigate\("\/login", \{ replace: true \}\)/);
  assert.match(showcase, /return demoEnabled\s*&&\s*localStorage\.getItem\("animemo_demo"\) === "true"/);
  assert.doesNotMatch(showcase, /^import .*getDemoUniverseOwner.*data\/universe/m);
  assert.match(showcase, /if \(demoEnabled\) await applyLocalRecords\(\);\s*else \{\s*setRecords\(\[\]\)/);
  assert.doesNotMatch(community, /^import .*demoUniverseOwners.*data\/universe/m);
  assert.match(community, /navigate\("\/login", \{ state: \{ from: "\/featured\/submit" \} \}\)/);
  assert.doesNotMatch(community, /setOwners\(demoUniverseOwners\);\s*\}\s*finally/);
  assert.doesNotMatch(featured, /^import .*featuredColumns.*data\/featuredColumns/m);
  assert.match(featured, /const \[columns, setColumns\] = useState\(\[\]\)/);
  assert.match(featured, /setSyncError\("精选专栏加载失败，请检查服务器连接。"\)/);
  assert.match(heroArt, /from "@demo-data"/);
  assert.doesNotMatch(heroArt, /data\/anime\.js/);
  assert.match(viteConfig, /mode === "development"[\s\S]*demoData\.development\.js[\s\S]*demoData\.production\.js/);
  assert.match(viteConfig, /"@demo-data": demoDataModule/);
  assert.match(productionDemoData, /export const demoEnabled = false/);
  assert.doesNotMatch(productionDemoData, /本地演示|demo-rabbit|一叠间漫画咖啡屋生活/);
});
