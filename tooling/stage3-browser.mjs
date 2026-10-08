import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const root = process.cwd();
const { origin, email, password } = JSON.parse(await readFile(process.env.ANIMEMO_REVIEW_ACCESS || '.local/output/stage3-review-access.json', 'utf8'));
const { chromium } = await import(pathToFileURL(process.env.ANIMEMO_PLAYWRIGHT || path.join(root, '.local/tools/browser/node_modules/playwright/index.mjs')));
const browser = await chromium.launch({ headless: true, executablePath: process.env.ANIMEMO_BROWSER || '/usr/bin/chromium', args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1080 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
const directory = '.local/output/browser/stage3'; await mkdir(directory, { recursive: true });
const report = { status: 'RUNNING', checks: [], errors: [], stage: 'login' };
const mark = text => { report.checks.push(text); console.log(text); };
page.on('pageerror', e => report.errors.push(e.message)); page.setDefaultTimeout(15000);
async function screenshot(name) { await page.screenshot({ path: `${directory}/${name}.png`, fullPage: name === 'journal-desktop' }); }
async function fits() { assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'horizontal page overflow'); }
try {
  await page.goto(origin); await page.getByRole('button', { name: '已有账号', exact: true }).click(); await screenshot('login');
  await page.getByLabel('邮箱地址').fill(email); await page.getByLabel(/^密码/).fill(password); await page.getByRole('button', { name: '进入我的手账' }).click();
  await page.getByRole('button', { name: '加入番剧', exact: true }).waitFor(); await page.waitForLoadState('networkidle'); await fits();
  const art = await page.request.get(origin + '/images/memory-sky.png'); assert.equal(art.status(), 200); assert.match(art.headers()['content-type'], /^image\/png/);
  await screenshot('journal-desktop'); mark('desktop login, real API cards, original anime hero and no overflow');
  report.stage = 'dialog-and-memory'; await page.getByRole('button', { name: '加入番剧', exact: true }).click();
  const dialog = page.getByRole('dialog'); await dialog.waitFor(); await page.getByLabel(/番剧名称/).fill(`浏览器验收-${randomUUID().slice(0,8)}`);
  await page.getByLabel('播出状态').selectOption('airing'); await page.getByLabel(/^总话数/).fill('2'); await screenshot('entry-editor');
  await page.getByRole('button', { name: '加入手账', exact: true }).click(); await dialog.waitFor({ state: 'hidden' });
  let entries = await (await page.request.get(origin + '/api/v1/entries')).json(); let added = entries.items.find(e => e.title.startsWith('浏览器验收-')); assert.ok(added);
  const card = page.locator('.entry-card').filter({ hasText: added.title }); await card.getByRole('button', { name: '记一次观看' }).click();
  await page.getByLabel('日期记得多清楚').selectOption('month'); await page.getByLabel('观看日期').fill('2026-09'); await page.getByLabel('看到第几话').fill('2');
  await page.getByRole('button', { name: '保存这次观看' }).click(); await dialog.waitFor({ state: 'hidden' }); await page.waitForLoadState('networkidle');
  added = await (await page.request.get(origin + `/api/v1/entries/${added.id}`)).json(); assert.equal(added.status, 'caught_up');
  const history = await (await page.request.get(origin + `/api/v1/entries/${added.id}/history`)).json(); assert.equal(history.items[0].time_precision, 'month');
  await card.getByRole('button').first().click(); await dialog.waitFor(); await page.getByText('记忆的变化', { exact: false }).first().click(); await screenshot('memory-revisions');
  await page.keyboard.press('Escape'); await dialog.waitFor({ state: 'hidden' }); mark('accessible form saves; month precision and ongoing caught-up state survive API round trip; revision dialog closes with Escape');
  report.stage = 'mobile'; await page.setViewportSize({ width: 390, height: 844 }); await page.evaluate(() => { document.activeElement?.blur(); scrollTo(0, 0); }); await fits(); await screenshot('journal-mobile');
  await page.getByRole('button', { name: '导入与备份', exact: true }).click(); await dialog.waitFor(); await fits(); await screenshot('transfer-mobile'); await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '观看足迹', exact: true }).click(); await page.getByRole('heading', { name: '观看足迹与统计' }).waitFor(); await fits(); await screenshot('history-mobile'); mark('390px journal, transfer and date-aware history layouts');
  report.stage = 'admin'; await page.setViewportSize({ width: 1440, height: 1080 }); await page.goto(origin + '/admin'); await page.getByRole('button', { name: '插件', exact: true }).click(); await page.getByText('官方随附', { exact: false }).first().waitFor(); await screenshot('official-extensions');
  await fits(); mark('admin extension inventory renders publisher identity and separate activation');
  assert.deepEqual(report.errors, []); report.status = 'PASS';
} catch (e) { report.status = 'FAIL'; report.error = e.message; await screenshot('failure'); throw e; }
finally { await writeFile(`${directory}/report.json`, JSON.stringify(report, null, 2) + '\n'); await browser.close(); }
