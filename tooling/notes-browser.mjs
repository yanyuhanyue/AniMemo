import { chromium } from '../.local/tools/browser/node_modules/playwright/index.mjs';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import assert from 'node:assert/strict';

// Default notes and shared memory flows, exercised against the real API.
const origin = process.env.REVIEW_ORIGIN || 'http://127.0.0.1:5177';
const output = process.env.REVIEW_OUTPUT || '.local/output/browser/notes-tests';
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.ANIMEMO_BROWSER || '/usr/bin/chromium', args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
page.setDefaultTimeout(15000);
const report = { checks: [], axe: [], errors: [] };
page.on('pageerror', error => report.errors.push(error.message));
const call = async (method, path, data) => {
  const response = await page.request.fetch(origin + path, { method, headers: { Origin: origin }, data });
  assert.ok(response.ok(), `${method} ${path}: ${response.status()}`);
  return response.status() === 204 ? null : response.json();
};
const click = async locator => { await page.locator('body').ariaSnapshot(); await locator.click(); };
const fill = async (locator, value) => { await page.locator('body').ariaSnapshot(); await locator.fill(value); };
const close = async () => { await page.locator('body').ariaSnapshot(); await page.keyboard.press('Escape'); await page.getByRole('dialog').waitFor({ state: 'detached' }); };
const check = text => { report.checks.push(text); console.log(text); };
const axe = async name => {
  await page.evaluate(await readFile('.local/tools/browser/node_modules/axe-core/axe.min.js', 'utf8'));
  const violations = await page.evaluate(async () => (await window.axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] } })).violations.map(v => ({ id: v.id, nodes: v.nodes.map(n => n.target) })));
  report.axe.push({ name, violations });
  assert.deepEqual(violations, [], name);
};
const capture = async name => {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: `${output}/${name}.png`, fullPage: false });
};
const fits = async () => {
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'page fits viewport');
  for (const el of await page.locator('.dialog, .note-editor, .note-sheet-entry').all()) assert.ok(await el.evaluate(n => n.scrollWidth <= n.clientWidth + 1), 'surface fits viewport');
};
try {
  await call('POST', '/api/v1/auth/register', { email: `notes-${randomUUID()}@example.test`, password: `test-${randomUUID()}`, display_name: '默认札记验收' });
  const entry = await call('POST', '/api/v1/entries', { title: '夏日的来信 · 合成验收' });
  const note = await call('POST', '/api/v1/memory/notes', { title: '没有确切日期的观后感', body: '有些故事结束了，讨论却还没有结束。', anime_id: entry.anime_id, highlight: true });
  await call('POST', '/api/v1/memory/notes', { title: '只记得年份的札记', body: '记忆可以没有具体月日。', anime_id: entry.anime_id, occurred_on: '2024', time_precision: 'year' });
  const longTitle = '这一段很长的札记标题也应该完整换行' .repeat(5);
  await call('POST', '/api/v1/memory/notes', { title: longTitle });
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMISNnyHwAE2AJoEeqx4gAAAABJRU5ErkJggg==', 'base64');
  const cover = await page.request.put(`${origin}/api/v1/entries/${entry.id}/cover?version=${entry.version}`, { headers: { Origin: origin, 'Content-Type': 'image/png' }, data: png });
  assert.ok(cover.ok(), 'upload a real private entry cover');
  const media = await call('POST', '/api/v1/memory/media', { byte_size: png.length });
  assert.ok((await page.request.put(`${origin}/api/v1/memory/media/${media.id}`, { headers: { Origin: origin, 'Content-Type': 'image/png' }, data: png })).ok());
  await call('POST', '/api/v1/memory/notes', { title: '配图札记', body: '图片跟随原有权限读取。', media_ids: [media.id] });
  const spoiler = await call('POST', '/api/v1/memory/notes', { title: '关于结局的想法', body: '主动展开之前，不应该看到这一句。', spoiler: true, media_ids: [media.id], anime_id: entry.anime_id });
  const references = [];
  page.on('request', request => { if (request.url().endsWith('/memory/references')) references.push(request.postDataJSON()); });
  await page.goto(origin + '/memory#notes');
  await page.locator('.note-sheet-work').filter({ hasText: entry.title }).first().waitFor();
  assert.equal(references.length, 1, 'one batch lookup for the note page');
  assert.equal(references[0].items.length, 1, 'duplicate anime references are deduplicated');
  check('Related works use one batched lookup per page, with no per-card request loop.');

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${origin}/memory#notes`);
  await page.locator('.note-sheet-work').filter({ hasText: entry.title }).first().waitFor();
  assert.ok(await page.getByText('2024年', { exact: true }).isVisible());
  assert.ok(await page.getByText('日期未记', { exact: true }).first().isVisible());
  const spoilerCard = page.locator('.note-sheet-entry').filter({ has: page.getByRole('button', { name: spoiler.title, exact: true }) });
  assert.equal(await spoilerCard.locator('.note-sheet-picture img').count(), 0);
  assert.equal(await spoilerCard.getByText(spoiler.body, { exact: true }).count(), 0);
  const poster = page.locator('.note-sheet-entry').filter({ has: page.getByRole('button', { name: note.title, exact: true }) }).locator('.note-sheet-poster img');
  await poster.scrollIntoViewIfNeeded();
  await poster.evaluate(img => img.decode());
  assert.ok((await poster.getAttribute('src')).startsWith(`/api/v1/entries/${entry.id}/cover/`));
  assert.equal(await page.locator('.note-sheet-entry').filter({ has: page.getByRole('button', { name: longTitle, exact: true }) }).locator('.note-sheet-poster').count(), 0, 'unlinked notes have no fabricated poster');
  const picture = page.locator('.note-sheet-entry').filter({ has: page.getByRole('button', { name: '配图札记', exact: true }) }).locator('img');
  await picture.scrollIntoViewIfNeeded();
  await picture.evaluate(img => img.decode());
  assert.ok(await picture.evaluate(img => img.naturalWidth > 0));
  await axe(`默认札记: list`);
  await click(page.getByRole('button', { name: `阅读 ${spoiler.title}`, exact: true }));
  assert.equal(await page.getByText(spoiler.body, { exact: true }).count(), 0, 'spoiler body is not mounted before disclosure');
  await click(page.getByText('展开剧透内容', { exact: true }));
  await page.getByText(spoiler.body, { exact: true }).waitFor({ state: 'visible' });
  await page.getByRole('dialog').locator('img').waitFor();
  await axe(`默认札记: reader`);
  await close();
  assert.ok(await page.getByRole('button', { name: `阅读 ${spoiler.title}`, exact: true }).evaluate(el => el === document.activeElement));

  await fill(page.getByLabel('年份', { exact: true }), '2024');
  await page.getByText('1 篇札记 · 筛选结果', { exact: false }).waitFor();
  assert.equal(await page.locator('.note-sheet-entry').count(), 1);
  await fill(page.getByLabel('年份', { exact: true }), '');
  await click(page.getByLabel('只看珍藏', { exact: false }));
  await page.getByRole('button', { name: `阅读 ${note.title}`, exact: true }).waitFor();
  assert.equal(await page.locator('.note-sheet-entry').count(), 1);
  await click(page.getByLabel('只看珍藏', { exact: false }));
  await fill(page.getByLabel('查找记忆', { exact: true }), '不存在的札记检索词');
  await page.getByRole('heading', { name: '没有找到匹配的记忆' }).waitFor();
  await click(page.getByRole('button', { name: '清除筛选', exact: true }));
  await page.locator('.note-sheet-entry').first().waitFor();

  await click(page.getByRole('button', { name: '写札记', exact: true }));
  await fill(page.getByLabel('记忆标题', { exact: true }), `体验 默认札记`);
  await fill(page.getByLabel('记忆正文', { exact: true }), '札记使用统一的保存逻辑。');
  await axe(`默认札记: composer`);
  for (const width of [390, 320]) { await page.setViewportSize({ width, height: 844 }); await fits(); }
  await click(page.getByRole('button', { name: '保存记忆', exact: true }));
  await page.getByRole('dialog').waitFor({ state: 'detached' });
  const saved = (await call('GET', '/api/v1/memory/notes')).items.find(n => n.title === `体验 默认札记`);
  assert.equal(saved.time_precision, 'unknown'); assert.equal(saved.occurred_on, ''); assert.equal(saved.visibility, 'private');
  await click(page.getByRole('button', { name: `阅读 ${saved.title}`, exact: true }));
  await click(page.getByRole('button', { name: '编辑记忆', exact: true }));
  await fill(page.getByRole('textbox', { name: '记忆正文', exact: true }), `已在 默认札记 中修改`);
  await click(page.getByRole('button', { name: '保存记忆', exact: true }));
  await page.getByRole('dialog').waitFor({ state: 'detached' });
  assert.equal((await call('GET', `/api/v1/memory/notes/${saved.id}`)).body, `已在 默认札记 中修改`);
  for (const width of [390, 320]) { await page.setViewportSize({ width, height: 844 }); await fits(); await capture(`默认札记-${width}`); }
  check(`默认札记: reading, private images, spoiler disclosure, filters, empty state, create/edit persistence, precise dates and 390/320px layouts passed.`);
  await page.waitForLoadState('networkidle');
  const liveEntry = await call('GET', `/api/v1/entries/${entry.id}`);
  await call('DELETE', `/api/v1/entries/${entry.id}/cover?version=${liveEntry.version}`);
  assert.equal((await page.request.get(`${origin}/api/v1/entries/${entry.id}/cover/${liveEntry.cover_revision}`)).status(), 404);
  await page.reload();
  await page.locator('.note-sheet-work').filter({ hasText: entry.title }).first().waitFor();
  const staleCard = page.locator('.note-sheet-entry').filter({ has: page.getByRole('button', { name: note.title, exact: true }) });
  await staleCard.scrollIntoViewIfNeeded();
  assert.equal(await staleCard.locator('.note-sheet-poster').count(), 0, 'a removed cover leaves no empty image frame after refresh');
  assert.ok(await staleCard.getByText(note.body, { exact: true }).isVisible());
  check('A removed cover returns a real 404 and disappears after refresh while the note remains readable.');
  for (const oldDesign of ['review', 'paper', 'classic', 'hybrid']) {
    await page.goto(`${origin}/memory?notes_ui=${oldDesign}#notes`);
    await page.locator('.note-sheet-entry').first().waitFor();
    assert.equal(await page.getByRole('group', { name: '选择札记版式' }).count(), 0);
    assert.equal(await page.getByRole('button', { name: '试用新排版', exact: true }).count(), 0);
    assert.equal(await page.locator('.memory-note-list').count(), 0);
  }
  await page.goto(`${origin}/memory?anime_id=${entry.anime_id}#notes`);
  await page.locator('.note-sheet-entry').first().waitFor();
  assert.equal(await page.locator('.note-sheet-entry').count(), 3);
  await page.getByRole('heading', { name: entry.title, exact: true }).waitFor();
  await click(page.getByRole('button', { name: '写札记', exact: true }));
  assert.equal(await page.getByRole('textbox', { name: '记忆标题', exact: true }).inputValue(), entry.title);
  await close();
  await call('POST', '/api/v1/memory/notes', { kind: 'moment', title: '值得留住的一帧', body: '瞬间继续使用自己的画廊。', media_ids: [media.id] });
  await page.goto(origin + '/memory#notes');
  await page.locator('.note-sheet-entry').first().waitFor();
  await click(page.getByRole('navigation', { name: '记忆分类', exact: true }).getByRole('button', { name: '瞬间', exact: true }));
  await page.locator('.memory-gallery .memory-note-card').first().waitFor();
  assert.equal(await page.locator('.note-sheet').count(), 0);
  await click(page.getByRole('button', { name: '收藏瞬间', exact: true }));
  assert.equal(await page.locator('.dialog.notes-surface').count(), 0);
  await close();
  await page.goBack();
  await page.locator('.note-sheet-entry').first().waitFor();
  await page.reload();
  await page.locator('.note-sheet-entry').first().waitFor();
  assert.equal(new URL(page.url()).searchParams.get('notes_ui'), null);
  check('Default and old preview URLs use one notes layout; scoped works, moments, browser back and refresh remain usable.');
  assert.deepEqual(report.errors, []);
} catch (error) {
  await capture('failure');
  await writeFile(`${output}/failure.txt`, await page.locator('body').ariaSnapshot());
  throw error;
} finally {
  await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2));
  await browser.close();
}
