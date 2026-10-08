import { chromium } from "../.local/tools/browser/node_modules/playwright/index.mjs";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { setTimeout } from "node:timers/promises";
import assert from "node:assert/strict";

// Development fixture only: publishes a synthetic rule, retires it in finally.
const access = JSON.parse(
  await readFile(
    process.env.ANIMEMO_ACHIEVEMENT_ACCESS ||
      ".local/output/v13-browser-access.json",
    "utf8",
  ),
);
assert.ok(
  access.email.endsWith("@example.test"),
  "Use a synthetic administrator",
);
const origin = process.env.REVIEW_ORIGIN || "http://127.0.0.1:5177";
const output =
  process.env.REVIEW_OUTPUT || ".local/output/browser/achievement-art";
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
});
const report = { checks: [], layouts: [], scans: [], errors: [] };
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
const mark = (message) => {
  report.checks.push(message);
  console.log(message);
};
let saved;
try {
  await api(admin, "POST", "/api/v1/auth/login", {
    email: access.email,
    password: access.password,
  });
  await api(user, "POST", "/api/v1/auth/register", {
    email: `art-${randomUUID()}@example.test`,
    password: `synthetic-${randomUUID()}`,
    display_name: "图案验收 · 合成账号",
  });
  await api(user, "POST", "/api/v1/entries", {
    title: "发布成就之前就记下的作品",
  });
  await admin.goto(origin + "/admin#achievements");
  await admin.getByRole("button", { name: "新增规则", exact: true }).click();
  const dialog = admin.getByRole("dialog");
  await dialog.waitFor();
  const seriesID = `art-review-${randomUUID().slice(0, 8)}`;
  for (const [label, value] of [
    ["系列标识", seriesID],
    ["系列名称", "星见的记忆"],
    ["徽章名称", "珍藏的光 · 合成"],
    ["等级", "3"],
  ]) {
    await admin.locator("body").ariaSnapshot();
    await admin.getByLabel(label, { exact: true }).fill(value);
  }
  await admin.getByRole("radio", { name: "记忆手札", exact: true }).check();
  const guide = admin.getByLabel("徽章边框等级预览");
  assert.deepEqual(
    await guide
      .locator(".achievement-badge")
      .evaluateAll((nodes) => nodes.map((n) => n.dataset.grade)),
    ["bronze", "silver", "gold", "platinum", "prismatic"],
  );
  assert.equal(await guide.locator(".badge-laurels").count(), 3);
  assert.equal(await guide.locator(".badge-crown").count(), 2);
  assert.equal(await guide.locator(".badge-gems").count(), 1);
  const metals = await guide
    .locator(".achievement-badge")
    .evaluateAll((nodes) =>
      nodes.map((n) => getComputedStyle(n).getPropertyValue("--badge-metal")),
    );
  assert.equal(new Set(metals).size, 5);
  await guide.screenshot({ path: output + "/tier-progression.png" });
  assert.equal(
    await admin
      .getByRole("group", { name: "徽章图案", exact: true })
      .getByRole("radio")
      .count(),
    8,
  );
  for (const [name, id] of [
    ["故事入场券", "ticket"],
    ["回忆收藏柜", "shelf"],
    ["时光纪念册", "album"],
  ]) {
    await admin.getByRole("radio", { name, exact: true }).check();
    assert.equal(
      await admin
        .locator(".badge-rule-preview .achievement-badge")
        .getAttribute("data-badge"),
      id,
    );
  }
  await admin
    .getByRole("group", { name: "徽章图案", exact: true })
    .screenshot({ path: output + "/motifs.png" });
  mark(
    "five distinct metal frames, tier-specific ornaments and eight live motif previews",
  );
  // Small transparent synthetic test image; no user's artwork is modified.
  const fixture = await admin.evaluate(() => {
    const c = document.createElement("canvas");
    c.width = c.height = 128;
    const g = c.getContext("2d");
    g.fillStyle = "#547ab4";
    g.beginPath();
    g.arc(64, 64, 47, 0, Math.PI * 2);
    g.fill();
    g.fillStyle = "#e9f9ff";
    g.beginPath();
    g.moveTo(64, 20);
    g.lineTo(77, 50);
    g.lineTo(109, 64);
    g.lineTo(77, 77);
    g.lineTo(64, 109);
    g.lineTo(51, 77);
    g.lineTo(20, 64);
    g.lineTo(51, 50);
    g.closePath();
    g.fill();
    return c.toDataURL("image/png").split(",")[1];
  });
  const imagePath = output + "/synthetic-art.png";
  await writeFile(imagePath, Buffer.from(fixture, "base64"));
  const response = admin.waitForResponse(
    (r) =>
      r.url().endsWith("/api/v1/admin/achievements/images") &&
      r.request().method() === "POST",
  );
  await admin
    .getByLabel("上传系列主图", { exact: true })
    .setInputFiles(imagePath);
  const uploaded = await response;
  assert.equal(uploaded.status(), 201);
  const preview = admin.locator(".badge-rule-preview svg image");
  await preview.waitFor();
  const image = { id: (await preview.getAttribute("href")).split("/").at(-1) };
  assert.match(image.id, /^[a-f0-9]{64}$/);
  assert.equal(
    await admin
      .locator(".badge-rule-preview .achievement-badge")
      .getAttribute("data-grade"),
    "gold",
  );
  for (const width of [1440, 390, 320, 768]) {
    await admin.setViewportSize({ width, height: 1080 });
    assert.ok(
      await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth),
      `editor overflow at ${width}`,
    );
    assert.ok(
      await admin.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    report.layouts.push({ width, fits: true });
    if (width === 1440 || width === 390) {
      await admin.addScriptTag({
        path: ".local/tools/browser/node_modules/axe-core/axe.min.js",
      });
      const scan = await admin.evaluate(() =>
        window.axe.run(document, {
          runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] },
        }),
      );
      const violations = scan.violations.map((v) => ({
        id: v.id,
        nodes: v.nodes.map((n) => n.target),
      }));
      report.scans.push({ width, violations });
      assert.deepEqual(violations, []);
    }
  }
  await admin.setViewportSize({ width: 1440, height: 1080 });
  await admin
    .locator(".badge-rule-preview")
    .screenshot({ path: output + "/uploaded-preview.png" });
  const saveResponse = admin.waitForResponse(
    (r) =>
      r.url().endsWith("/api/v1/admin/achievements/rules") &&
      r.request().method() === "PUT",
  );
  await admin
    .getByRole("button", { name: "保存规则修订", exact: true })
    .click();
  const result = await saveResponse;
  assert.ok(result.ok());
  saved = (await api(admin, "GET", "/api/v1/admin/achievements/rules")).items.find((r) => r.series_id === seriesID);
  assert.equal(saved.badge_image_id, image.id);
  assert.equal(saved.active, true);
  mark(
    "uploaded PNG is saved as immutable center art, preserving the gold frame; responsive editor and axe scans pass",
  );
  let award;
  for (let attempt = 0; attempt < 120; attempt++) {
    const data = await api(user, "GET", "/api/v1/memory/achievements");
    award = data.items.find((a) => a.id === saved.id);
    if (award?.granted) break;
    await setTimeout(500);
  }
  assert.ok(
    award?.granted,
    "Existing eligible user was not automatically awarded",
  );
  assert.equal(award.value, 1);
  assert.equal(award.badge_image_id, image.id);
  await user.goto(origin + "/memory#achievements");
  await user
    .getByLabel("纪念系列", { exact: true })
    .selectOption(saved.series_id);
  const card = user.locator(".achievement-card");
  await card.locator("svg image").waitFor();
  assert.equal(await card.getAttribute("data-state"), "earned");
  assert.ok(
    (await card.locator(".achievement-card-tier").textContent()).includes(
      "黄金",
    ),
  );
  const loaded = await user.request.get(
    origin + "/api/v1/memory/achievement-images/" + image.id,
  );
  assert.equal(loaded.status(), 200);
  assert.equal(loaded.headers()["content-type"], "image/png");
  await card.screenshot({ path: output + "/earned-custom.png" });
  const before = await api(user, "GET", "/api/v1/entries");
  assert.equal(before.total, 1);
  mark(
    "an already eligible idle user automatically earns the newly published badge, including its uploaded image",
  );
  assert.deepEqual(report.errors, []);
} catch (error) {
  report.failure = String(error);
  await admin.screenshot({
    path: output + "/failure-admin.png",
    fullPage: true,
  });
  throw error;
} finally {
  if (saved) {
    await api(admin, "PUT", "/api/v1/admin/achievements/rules", {
      ...saved,
      active: false,
    });
    report.retiredSyntheticRule = true;
  }
  await writeFile(output + "/report.json", JSON.stringify(report, null, 2));
  await browser.close();
}
