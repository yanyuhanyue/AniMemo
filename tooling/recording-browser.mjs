import { chromium } from "../.local/tools/browser/node_modules/playwright/index.mjs";
import { mkdir, writeFile, readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import assert from "node:assert/strict";
const origin = process.env.REVIEW_ORIGIN || "http://127.0.0.1:5177";
const dir = process.env.REVIEW_OUTPUT || ".local/output/browser/recording";
await mkdir(dir, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.ANIMEMO_BROWSER || "/usr/bin/chromium",
  args: ["--no-sandbox"],
});
const page = await browser.newPage({
  viewport: { width: 1440, height: 1000 },
  locale: "zh-CN",
});
page.setDefaultTimeout(15000);
const report = { origin, checks: [], errors: [] };
page.on("pageerror", (e) => report.errors.push(e.message));
const api = async (method, path, data) => {
  const r = await page.request.fetch(origin + path, {
    method,
    headers: { Origin: origin },
    data,
  });
  assert.ok(
    r.ok(),
    `${method} ${path}: ${r.status()} ${(await r.text()).slice(0, 150)}`,
  );
  return r.status() === 204 ? null : r.json();
};
const click = async (locator) => {
  await page.locator("body").ariaSnapshot();
  await locator.click();
};
const mark = (s) => report.checks.push(s);
const axe = async (name) => {
  await page.evaluate(
    await readFile(
      ".local/tools/browser/node_modules/axe-core/axe.min.js",
      "utf8",
    ),
  );
  const result = await page.evaluate(() =>
    window.axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] },
    }),
  );
  const errors = result.violations.map((v) => ({
    id: v.id,
    targets: v.nodes.map((n) => n.target),
  }));
  assert.deepEqual(errors, [], name);
};
try {
  await api("POST", "/api/v1/auth/register", {
    email: `recording-${randomUUID()}@example.test`,
    password: `test-${randomUUID()}`,
    display_name: "旧番回忆验收",
  });
  await page.goto(origin);
  await click(page.getByRole("button", { name: "记下看过的番", exact: true }));
  await page.getByRole("dialog").waitFor();
  assert.equal(
    await page
      .getByRole("dialog")
      .locator("input:visible,textarea:visible,select:visible")
      .count(),
    2,
  );
  await page.getByLabel("番剧名称").fill("夏日放学路上 · 合成验收");
  await page
    .getByLabel("留一点感想")
    .fill("高中时看过，具体是哪一天已经记不清了。");
  await page.screenshot({ path: dir + "/minimal-entry.png", fullPage: false });
  await axe("minimal entry");
  await click(page.getByRole("button", { name: "保存记录", exact: true }));
  await page.getByRole("dialog").waitFor({ state: "detached" });
  let e = (await api("GET", "/api/v1/entries")).items[0];
  assert.equal(e.status, "recorded");
  assert.equal(e.total_episodes, 0);
  assert.equal(e.watched_episodes, 0);
  assert.equal(e.visibility, "private");
  assert.equal(e.score, null);
  assert.equal((await api("GET", "/api/v1/history/page")).total, 0);
  mark(
    "title and optional recollection create a private record without invented details",
  );
  await page.screenshot({ path: dir + "/journal.png", fullPage: true });
  await axe("journal");
  const pickerRequests = [];
  const listener = (r) => {
    if (/memory\/(characters|episodes)/.test(r.url()))
      pickerRequests.push(r.url());
  };
  page.on("request", listener);
  const trigger = page.getByRole("button", { name: "留点回忆", exact: true });
  await click(trigger);
  await page.getByRole("dialog").waitFor();
  assert.ok(
    await page
      .getByLabel("记忆正文", { exact: true })
      .evaluate((el) => el === document.activeElement),
  );
  assert.equal(
    await page.getByLabel("日期精度", { exact: true }).isVisible(),
    false,
  );
  await page
    .getByLabel("记忆正文", { exact: true })
    .fill("放学后和朋友一起看的。多年以后，仍然记得那段配乐。");
  await page.screenshot({ path: dir + "/quick-memory.png", fullPage: false });
  await axe("quick memory");
  await click(page.getByRole("button", { name: "保存记忆", exact: true }));
  await page.getByRole("dialog").waitFor({ state: "detached" });
  assert.ok(await trigger.evaluate((el) => el === document.activeElement));
  page.off("request", listener);
  assert.deepEqual(pickerRequests, []);
  const note = (await api("GET", "/api/v1/memory/notes")).items[0];
  assert.equal(note.anime_id, e.anime_id);
  assert.equal(note.time_precision, "unknown");
  assert.equal(note.occurred_on, "");
  assert.equal(note.episode_id, "");
  assert.equal(note.watch_id, "");
  assert.equal((await api("GET", "/api/v1/stats")).watch_records, 0);
  mark(
    "card recollection is undated, preserves anime reference and avoids optional catalog requests",
  );
  await click(
    page.getByRole("button", { name: `查看 ${e.title}`, exact: true }),
  );
  await click(page.getByRole("button", { name: "记一次观看", exact: true }));
  assert.equal(
    await page.getByLabel("日期记得多清楚", { exact: true }).inputValue(),
    "unknown",
  );
  assert.equal(
    await page.getByLabel("从第几话", { exact: true }).inputValue(),
    "",
  );
  assert.equal(
    await page.getByLabel("看到第几话", { exact: true }).inputValue(),
    "",
  );
  assert.equal(await page.getByLabel("第几次观看").inputValue(), "");
  await page
    .getByLabel("日期记得多清楚", { exact: true })
    .selectOption("month");
  assert.equal(
    await page.getByLabel("观看日期", { exact: true }).inputValue(),
    "",
  );
  await page.keyboard.press("Escape");
  mark("precise viewing never pre-fills today or next episode");
  e = await api("PATCH", "/api/v1/entries/" + e.id, {
    version: e.version,
    score: 8.5,
    tags: ["学生时代"],
    details: {
      studio: "合成制作公司",
      airing_period: "未知",
      description: "资料由用户提供",
      reference_url: "https://example.test/work",
    },
    status: "completed",
    total_episodes: 12,
    airing_state: "finished",
  });
  await page.reload();
  await click(
    page.getByRole("button", { name: `查看 ${e.title}`, exact: true }),
  );
  await click(page.getByRole("button", { name: "编辑记录", exact: true }));
  await page
    .getByLabel("留一点感想")
    .fill("补充了记忆，原来的评分和资料仍应保留。");
  await click(page.getByRole("button", { name: "保存修改", exact: true }));
  await page.getByRole("dialog").waitFor({ state: "detached" });
  const after = await api("GET", "/api/v1/entries/" + e.id);
  assert.equal(after.score, 8.5);
  assert.deepEqual(after.tags, e.tags);
  assert.deepEqual(after.details, e.details);
  assert.equal(after.status, "completed");
  assert.equal(after.total_episodes, 12);
  mark("collapsed optional fields preserve existing values on edit");
  await page.goto(origin + "/memory");
  await page.getByRole("button", { name: "写札记", exact: true }).waitFor();
  assert.equal(
    await page.getByRole("button", { name: "角色", exact: true }).isVisible(),
    false,
  );
  await click(page.locator(".memory-organize summary"));
  await page.getByRole("button", { name: "角色", exact: true }).waitFor();
  await click(page.getByRole("button", { name: "角色", exact: true }));
  await page.reload();
  await page
    .getByRole("button", { name: "记住一个角色", exact: true })
    .waitFor();
  assert.ok(await page.locator(".memory-organize").evaluate((el) => el.open));
  mark("optional tools stay available and deep links reopen their group");
  await page.goto(origin + "/memory#search");
  await page.getByLabel("搜索全部记忆", { exact: true }).fill("多年以后");
  await page
    .locator(".memory-thread")
    .getByText(note.title, { exact: true })
    .waitFor();
  mark("free-text recollection remains searchable");
  for (const width of [320, 390, 768]) {
    await page.setViewportSize({ width, height: 844 });
    assert.ok(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: dir + "/memory-mobile.png", fullPage: true });
  await axe("memory mobile");
  await page.goto(origin);
  await page
    .getByRole("button", { name: "记下看过的番", exact: true })
    .waitFor();
  for (const width of [320, 390, 768]) {
    await page.setViewportSize({ width, height: 844 });
    assert.ok(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await click(page.getByRole("button", { name: "记下看过的番", exact: true }));
  await page.screenshot({ path: dir + "/minimal-mobile.png", fullPage: false });
  await page.keyboard.press("Escape");
  mark("320/390/768 mobile widths and keyboard dismissal");
  assert.deepEqual(report.errors, []);
  report.status = "PASS";
} catch (e) {
  report.status = "FAIL";
  report.failure = String(e);
  await page.screenshot({ path: dir + "/failure.png", fullPage: true });
  throw e;
} finally {
  await writeFile(dir + "/report.json", JSON.stringify(report, null, 2));
  await browser.close();
}
