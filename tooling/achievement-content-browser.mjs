import { chromium } from "../.local/tools/browser/node_modules/playwright/index.mjs";
import { mkdir, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { setTimeout } from "node:timers/promises";
import assert from "node:assert/strict";

// Real API + worker; only creates a new, private synthetic account.
const origin = process.env.REVIEW_ORIGIN || "http://127.0.0.1:5177";
const output =
  process.env.REVIEW_OUTPUT || ".local/output/browser/achievement-content";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.ANIMEMO_BROWSER || "/usr/bin/chromium",
  args: ["--no-sandbox"],
});
const page = await browser.newPage({
  viewport: { width: 1440, height: 1080 },
  locale: "zh-CN",
  reducedMotion: "reduce",
});
const report = { origin, checks: [], layouts: [], scans: [], errors: [] };
page.on("pageerror", (e) => report.errors.push(e.message));
const api = async (method, route, data) => {
  const response = await page.request.fetch(origin + route, {
    method,
    headers: { Origin: origin },
    data,
  });
  assert.ok(response.ok(), `${method} ${route}: ${response.status()}`);
  return response.status() === 204 ? null : response.json();
};
const center = () => api("GET", "/api/v1/memory/achievements");
const waitForAwards = async (count) => {
  for (let attempt = 0; attempt < 40; attempt++) {
    const data = await center();
    if (data.items.filter((a) => a.granted).length === count) return data.items;
    await setTimeout(250);
  }
  throw new Error(`Worker did not grant ${count} awards within 10 seconds`);
};
const mark = (message) => {
  report.checks.push(message);
  console.log(message);
};
try {
  await api("POST", "/api/v1/auth/register", {
    email: `keepsake-${randomUUID()}@example.test`,
    password: `synthetic-${randomUUID()}`,
    display_name: "星见 · 纪念图鉴验收",
  });
  const entry = await api("POST", "/api/v1/entries", {
    title: "夏目友人帐 · 只记得那个夏天",
  });
  const note = await api("POST", "/api/v1/memory/notes", {
    title: "雨后回家的路",
    body: "已经想不起是哪一年，但那份温柔一直留在记忆里。",
    anime_id: entry.anime_id,
  });
  const collection = await api("POST", "/api/v1/memory/collections", {
    title: "温柔的故事",
  });
  let yearly = await api("POST", "/api/v1/memory/yearly", {
    title: "写给未来的自己",
    year: 2026,
  });
  const first = await waitForAwards(2);
  assert.equal(first.length, 12);
  for (const metric of [
    "memory_collections",
    "yearly_albums",
    "watch_records",
    "distinct_anime",
  ]) {
    assert.ok(
      first
        .filter((a) => a.metric === metric)
        .every((a) => !a.granted && a.value === 0),
      metric,
    );
  }
  assert.equal(note.time_precision, "unknown");
  assert.equal(note.occurred_on, "");
  assert.equal(entry.status, "recorded");
  assert.equal(entry.watched_episodes, 0);
  mark(
    "title-only work and undated note unlock two keepsakes; empty collections/albums and absent viewing facts do not",
  );
  await api("PUT", `/api/v1/memory/collections/${collection.id}`, {
    version: collection.version,
    title: collection.title,
    items: [{ kind: "note", id: note.id }],
  });
  const albumBody = () => ({
    version: yearly.version,
    year: yearly.year,
    timezone: yearly.timezone,
    title: yearly.title,
    note_ids: [note.id],
  });
  yearly = await api("PUT", `/api/v1/memory/yearly/${yearly.id}`, albumBody());
  const awards = (await waitForAwards(4)).filter((a) => a.granted);
  const albumAward = awards.find((a) => a.metric === "yearly_albums");
  yearly = await api("PUT", `/api/v1/memory/yearly/${yearly.id}`, albumBody());
  yearly = await api("PUT", `/api/v1/memory/yearly/${yearly.id}`, albumBody());
  const frozen = await api("GET", `/api/v1/memory/yearly/${yearly.id}`);
  assert.equal(frozen.revisions[0].items[0].time_precision, "unknown");
  assert.equal(frozen.revisions[0].items[0].occurred_on, "");
  await api("PUT", "/api/v1/memory/achievements/showcase", {
    unlock_ids: awards.map((a) => a.unlock_id),
  });
  await api("POST", "/api/v1/memory/achievements/acknowledge");
  mark(
    "nonempty collection and annual selection unlock automatically; annual snapshot preserves unknown dates",
  );
  await page.goto(origin + "/memory#achievements");
  await page
    .getByRole("heading", { name: "属于你的徽章", exact: true })
    .waitFor();
  await page
    .locator('.achievement-card[data-state="earned"]')
    .first()
    .waitFor();
  assert.equal(await page.locator(".achievement-card").count(), 12);
  assert.equal(
    await page.locator('.achievement-card[data-state="earned"]').count(),
    4,
  );
  assert.equal(await page.locator(".achievement-showcase-item").count(), 4);
  const filter = page.getByLabel("纪念系列", { exact: true });
  for (const [series, count] of [
    ["recorded-worlds", 2],
    ["memory-pages", 3],
    ["memory-shelves", 2],
    ["yearly-albums", 1],
  ]) {
    await page.locator("body").ariaSnapshot();
    await filter.selectOption(series);
    assert.equal(
      await page.locator(".achievement-card").count(),
      count,
      series,
    );
  }
  await page.locator(".achievement-rule-help summary").click();
  assert.ok(
    (await page.locator(".achievement-rule-help").textContent()).includes(
      "多次保存同一本仍计为一本",
    ),
  );
  assert.ok(
    (await page.locator(".achievement-condition").textContent()).includes(
      "1 本",
    ),
  );
  await filter.selectOption("memory-shelves");
  assert.ok(
    (
      await page.locator(".achievement-condition").first().textContent()
    ).includes("1 个"),
  );
  await filter.selectOption("");
  mark(
    "12-badge catalog filters all four recording families with correct units and readable rule explanations",
  );
  for (const width of [1440, 390, 320, 768]) {
    await page.setViewportSize({ width, height: 1080 });
    assert.ok(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      `overflow at ${width}`,
    );
    report.layouts.push({ width, fits: true });
    if (width === 1440 || width === 390) {
      await page.addScriptTag({
        path: ".local/tools/browser/node_modules/axe-core/axe.min.js",
      });
      const scan = await page.evaluate(() =>
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
      await page.screenshot({
        path: `${output}/catalog-${width}.png`,
        fullPage: true,
      });
    }
  }
  const finalAlbum = (await center()).items.find(
    (a) => a.metric === "yearly_albums",
  );
  assert.equal(finalAlbum.value, 1);
  assert.equal(finalAlbum.unlock_id, albumAward.unlock_id);
  assert.equal(finalAlbum.unlocked_at, albumAward.unlocked_at);
  assert.deepEqual(report.errors, []);
  mark(
    "320/390/768/1440 widths fit, desktop/mobile axe scans pass, album edits preserve the original unlock",
  );
  await page.setViewportSize({ width: 1440, height: 1080 });
  await page
    .locator(".achievement-library")
    .screenshot({ path: `${output}/recording-keepsakes.png` });
} catch (error) {
  report.failure = String(error);
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
  throw error;
} finally {
  await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2));
  await browser.close();
}
