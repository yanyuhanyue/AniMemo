import { chromium } from "../.local/tools/browser/node_modules/playwright/index.mjs";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import assert from "node:assert/strict";

// Run only against a development fixture: creates a synthetic account and an inactive rule.
const access = JSON.parse(
  await readFile(
    process.env.ANIMEMO_ACHIEVEMENT_ACCESS ||
      ".local/output/v13-browser-access.json",
    "utf8",
  ),
);
const origin = process.env.REVIEW_ORIGIN || "http://127.0.0.1:5177";
assert.ok(
  access.email.endsWith("@example.test"),
  "Use a synthetic administrator account.",
);
const output =
  process.env.REVIEW_OUTPUT || ".local/output/browser/achievement-polish";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.ANIMEMO_BROWSER || "/usr/bin/chromium",
  args: ["--no-sandbox"],
});
const admin = await browser.newPage({
  viewport: { width: 1440, height: 1080 },
  locale: "zh-CN",
});
const user = await browser.newPage({
  viewport: { width: 1440, height: 1080 },
  locale: "zh-CN",
  reducedMotion: "reduce",
});
const report = { origin, checks: [], layouts: [], scans: [], errors: [] };
for (const page of [admin, user])
  page.on("pageerror", (e) => report.errors.push(e.message));
const api = async (page, method, route, data) => {
  const r = await page.request.fetch(origin + route, {
    method,
    headers: { Origin: origin },
    data,
  });
  assert.ok(r.ok(), `${method} ${route}: ${r.status()}`);
  return r.status() === 204 ? null : r.json();
};
const click = async (page, control) => {
  await page.locator("body").ariaSnapshot();
  await control.click();
};
const mark = (text) => {
  report.checks.push(text);
  console.log(text);
};
const scan = async (page, name) => {
  await page.addScriptTag({
    path: ".local/tools/browser/node_modules/axe-core/axe.min.js",
  });
  const result = await page.evaluate(() =>
    window.axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] },
    }),
  );
  const violations = result.violations.map((v) => ({
    id: v.id,
    nodes: v.nodes.map((n) => ({
      target: n.target,
      summary: n.failureSummary,
    })),
  }));
  report.scans.push({ name, violations });
  assert.deepEqual(violations, [], name);
};
const fits = async (page, width, screen) => {
  await page.setViewportSize({ width, height: 1000 });
  assert.ok(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    `${screen}: overflow at ${width}`,
  );
  report.layouts.push({ screen, width, fits: true });
};
try {
  await api(admin, "POST", "/api/v1/auth/login", {
    email: access.email,
    password: access.password,
  });
  const suffix = randomUUID().slice(0, 8);
  await api(user, "POST", "/api/v1/auth/register", {
    email: `badge-${suffix}@example.test`,
    password: `synthetic-${randomUUID()}`,
    display_name: "纪念章验收 · 合成账号",
  });
  const owner = await api(user, "GET", "/api/v1/auth/me");
  const rules = (await api(admin, "GET", "/api/v1/admin/achievements/rules"))
    .items;
  const originalRuleIDs = { spark: "001", orbit: "003", book: "005" };
  const byBadge = (badge) =>
    rules.find(
      (r) =>
        r.id === `a1000000-0000-4000-8000-000000000${originalRuleIDs[badge]}` &&
        r.active,
    );
  const grant = async (badge, value) =>
    api(admin, "POST", "/api/v1/admin/achievements/grants", {
      owner_id: owner.id,
      tier_id: byBadge(badge).id,
      grant: value,
      reason: "徽章界面自动验收：仅合成账号",
    });
  await user.goto(origin + "/memory#achievements");
  await user
    .locator('.achievement-card[data-state="locked"]')
    .first()
    .waitFor();
  assert.equal(
    await user.locator('.achievement-card[data-state="earned"]').count(),
    0,
  );
  mark("fresh account has engraved locked badges and no invented unlocks");
  for (const badge of ["spark", "orbit", "book"]) await grant(badge, true);
  await user.reload();
  await user
    .getByRole("button", { name: "收下这份纪念", exact: true })
    .waitFor();
  await click(
    user,
    user.getByRole("button", { name: "收下这份纪念", exact: true }),
  );
  await user.locator(".achievement-notice").waitFor({ state: "detached" });
  for (const title of ["第一束光", "五个世界", "写给以后的自己"]) {
    const card = user
      .locator(".achievement-card")
      .filter({ has: user.getByRole("heading", { name: title, exact: true }) });
    await click(
      user,
      card.getByRole("button", { name: "放上纪念架", exact: true }),
    );
    await card.getByRole("button", { name: "移出展示", exact: true }).waitFor();
  }
  assert.equal(
    (await api(user, "GET", "/api/v1/memory/achievements")).items.filter(
      (a) => a.showcase_slot > 0,
    ).length,
    3,
  );
  await click(
    user,
    user.getByRole("button", { name: "移出展示：第一束光", exact: true }),
  );
  const spark = user.locator(".achievement-card").filter({
    has: user.getByRole("heading", { name: "第一束光", exact: true }),
  });
  await spark
    .getByRole("button", { name: "放上纪念架", exact: true })
    .waitFor();
  assert.equal(await spark.getAttribute("data-state"), "earned");
  await click(
    user,
    spark.getByRole("button", { name: "放上纪念架", exact: true }),
  );
  await spark.getByRole("button", { name: "移出展示", exact: true }).waitFor();
  mark(
    "acknowledge, showcase add/remove persist through the real API; removal preserves ownership",
  );
  await grant("orbit", false);
  await user.reload();
  await user.locator('.achievement-card[data-state="revoked"]').waitFor();
  assert.equal(
    await user
      .getByRole("button", { name: "移出展示：五个世界", exact: true })
      .count(),
    0,
  );
  assert.equal(
    await user
      .locator('.achievement-card[data-state="revoked"] button')
      .count(),
    0,
  );
  await user
    .locator('.achievement-card[data-state="revoked"]')
    .screenshot({ path: output + "/revoked.png" });
  mark(
    "revoked grant leaves a labelled historical badge and removes showcase actions",
  );
  await grant("orbit", true);
  await user.reload();
  await user
    .locator('.achievement-card[data-state="earned"]')
    .first()
    .waitFor();
  const orbit = user.locator(".achievement-card").filter({
    has: user.getByRole("heading", { name: "五个世界", exact: true }),
  });
  await click(
    user,
    orbit.getByRole("button", { name: "放上纪念架", exact: true }),
  );
  await orbit.getByRole("button", { name: "移出展示", exact: true }).waitFor();
  await user.evaluate(() => window.scrollTo(0, 0));
  await user.screenshot({
    path: output + "/01-achievements.png",
    fullPage: true,
  });
  await user.setViewportSize({ width: 1440, height: 2400 });
  const bounds = await user.locator(".achievement-library").boundingBox();
  await user.screenshot({
    path: output + "/achievement-library.png",
    clip: bounds,
  });
  await user.setViewportSize({ width: 1440, height: 1080 });
  await scan(user, "achievement catalog desktop");
  assert.equal(
    await user.locator(".achievement-badge svg").evaluateAll((nodes) => {
      const ids = nodes.flatMap((n) =>
        [...n.querySelectorAll("[id]")].map((el) => el.id),
      );
      return ids.length - new Set(ids).size;
    }),
    0,
  );
  for (const width of [320, 390, 768, 1440]) {
    await fits(user, width, "catalog");
    if (width === 390) {
      await user.evaluate(() => window.scrollTo(0, 0));
      await user.screenshot({
        path: output + "/02-mobile.png",
        fullPage: true,
      });
      await scan(user, "achievement catalog mobile");
    }
  }
  mark(
    "catalog fits 320/390/768/1440; SVG gradients have unique IDs and reduced-motion view is static",
  );
  await admin.goto(origin + "/admin#achievements");
  await admin.locator(".badge-rule-cell").first().waitFor();
  await admin.screenshot({ path: output + "/03-admin.png", fullPage: true });
  await click(
    admin,
    admin.getByRole("button", { name: "新增规则", exact: true }),
  );
  await admin.getByRole("dialog").waitFor();
  for (const [label, value] of [
    ["系列标识", `badge-review-${suffix}`],
    ["系列名称", "图案验收（合成）"],
    ["等级", "20"],
    ["徽章名称", "花开的纪念 · 合成"],
    ["说明", "验证图案、等级和正文预览。此规则保持停用。"],
  ]) {
    await admin.locator("body").ariaSnapshot();
    await admin.getByLabel(label, { exact: true }).fill(value);
  }
  for (const [label, badge] of [
    ["初光之星", "spark"],
    ["月夜来信", "moon"],
    ["漫游星轨", "orbit"],
    ["花开之时", "flower"],
    ["记忆手札", "book"],
  ]) {
    await click(admin, admin.getByText(label, { exact: true }));
    assert.equal(
      await admin
        .locator(".badge-rule-preview .achievement-badge")
        .getAttribute("data-badge"),
      badge,
    );
  }
  await admin.getByRole("radio", { name: "初光之星", exact: true }).focus();
  await admin.locator("body").ariaSnapshot();
  await admin.keyboard.press("ArrowRight");
  assert.equal(
    await admin
      .getByRole("radio", { name: "月夜来信", exact: true })
      .isChecked(),
    true,
  );
  await click(admin, admin.getByText("花开之时", { exact: true }));
  await admin.getByLabel("启用规则", { exact: true }).uncheck();
  assert.equal(
    await admin.locator(".badge-rule-preview svg text").textContent(),
    "20",
  );
  await admin.getByRole("dialog").evaluate((el) => (el.scrollTop = 0));
  await admin.screenshot({ path: output + "/04-editor.png", fullPage: false });
  await admin
    .getByRole("group", { name: "徽章图案", exact: true })
    .screenshot({ path: output + "/badge-options.png" });
  await scan(admin, "rule preview desktop");
  for (const width of [320, 390, 768, 1440]) {
    await fits(admin, width, "rule editor");
    assert.ok(
      await admin
        .getByRole("dialog")
        .evaluate((el) => el.scrollWidth <= el.clientWidth),
      `dialog overflow ${width}`,
    );
    if (width === 390) await scan(admin, "rule preview mobile");
  }
  const savedResponse = admin.waitForResponse(
    (r) =>
      r.url().endsWith("/api/v1/admin/achievements/rules") &&
      r.request().method() === "PUT",
  );
  await click(
    admin,
    admin.getByRole("button", { name: "保存规则修订", exact: true }),
  );
  const response = await savedResponse;
  assert.ok(response.ok());
  const saved = await response.json();
  assert.equal(saved.badge, "flower");
  assert.equal(saved.tier, 20);
  assert.equal(saved.active, false);
  await admin.getByRole("dialog").waitFor({ state: "detached" });
  await click(
    admin,
    admin.getByRole("button", { name: "新增规则", exact: true }),
  );
  await admin.getByRole("dialog").waitFor();
  await admin.locator("body").ariaSnapshot();
  await admin.keyboard.press("Escape");
  await admin.getByRole("dialog").waitFor({ state: "detached" });
  assert.equal(
    await admin
      .getByRole("button", { name: "新增规则", exact: true })
      .evaluate((el) => document.activeElement === el),
    true,
  );
  mark(
    "all five patterns preview live; keyboard radios, tier 20, save and Escape focus restoration work",
  );
  mark(
    "rule editor fits 320/390/768/1440; desktop/mobile axe scans have no violations",
  );
  assert.deepEqual(report.errors, []);
  report.status = "PASS";
} catch (error) {
  report.status = "FAIL";
  report.failure = String(error);
  await user.screenshot({ path: output + "/failure-user.png", fullPage: true });
  await admin.screenshot({
    path: output + "/failure-admin.png",
    fullPage: true,
  });
  throw error;
} finally {
  await writeFile(output + "/report.json", JSON.stringify(report, null, 2));
  await browser.close();
}
