import { chromium } from '../.local/tools/browser/node_modules/playwright/index.mjs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import assert from 'node:assert/strict';

// Real API acceptance for the reading/editing boundary, using an isolated account.
const origin = process.env.REVIEW_ORIGIN || 'http://127.0.0.1:5177';
const output = process.env.REVIEW_OUTPUT || '.local/output/browser/reading';
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.ANIMEMO_BROWSER || '/usr/bin/chromium', args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
page.setDefaultTimeout(15000);
const report = { origin, checks: [], axe: [], errors: [] };
page.on('pageerror', error => report.errors.push(error.message));
const call = async (method, path, data) => {
  const response = await page.request.fetch(origin + path, { method, headers: { Origin: origin }, data });
  assert.ok(response.ok(), `${method} ${path}: ${response.status()} ${(await response.text()).slice(0, 180)}`);
  return response.status() === 204 ? null : response.json();
};
const click = async locator => { await page.locator('body').ariaSnapshot(); await locator.click(); };
const fill = async (locator, value) => { await page.locator('body').ariaSnapshot(); await locator.fill(value); };
const mark = text => { report.checks.push(text); console.log(text); };
const capture = async name => {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: `${output}/${name}.png`, fullPage: false });
  await writeFile(`${output}/${name}.txt`, await page.locator('body').ariaSnapshot());
};
const axe = async name => {
  await page.evaluate(await readFile('.local/tools/browser/node_modules/axe-core/axe.min.js', 'utf8'));
  const violations = await page.evaluate(async () => (await window.axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] } })).violations.map(v => ({ id: v.id, nodes: v.nodes.map(n => n.target) })));
  report.axe.push({ name, violations });
  assert.deepEqual(violations, [], name);
};
const noOverflow = async () => {
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'page fits viewport');
  for (const el of await page.locator('.dialog, .collection-pages, .note-editor').all()) {
    assert.ok(await el.evaluate(node => node.scrollWidth <= node.clientWidth + 1), 'reading surface fits viewport');
  }
};
const close = async () => { await page.locator('body').ariaSnapshot(); await page.keyboard.press('Escape'); await page.getByRole('dialog').waitFor({ state: 'detached' }); };
try {
  await call('POST', '/api/v1/auth/register', { email: `reading-${randomUUID()}@example.test`, password: `test-${randomUUID()}`, display_name: '阅读交互验收' });
  const entry = await call('POST', '/api/v1/entries', { title: '夏天的故事 · 阅读验收', notes: '那段一起看动画的日子，仍然闪闪发光。' });
  const note = await call('POST', '/api/v1/memory/notes', { title: '放学路上的片尾曲', body: '那时候的风、晚霞和音乐，如今想起仍然清晰。', anime_id: entry.anime_id, time_precision: 'unknown', visibility: 'private' });
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMISNnyHwAE2AJoEeqx4gAAAABJRU5ErkJggg==', 'base64');
  const media = await call('POST', '/api/v1/memory/media', { byte_size: png.length });
  assert.ok((await page.request.put(`${origin}/api/v1/memory/media/${media.id}`, { headers: { Origin: origin, 'Content-Type': 'image/png' }, data: png })).ok());
  const spoiler = await call('POST', '/api/v1/memory/notes', { kind: 'moment', title: '最后一幕的秘密', body: '仅在主动展开后阅读的剧情内容。', spoiler: true, media_ids: [media.id] });
  const character = await call('POST', '/api/v1/memory/characters', { name: '故事里的老朋友' });
  const removedNote = await call('POST', '/api/v1/memory/notes', { title: '准备移出的札记' });
  const collection = await call('POST', '/api/v1/memory/collections', { title: '把夏天装订成册', description: '作品、札记与那些值得留住的片刻。', items: [{ kind: 'note', id: note.id }, { kind: 'moment', id: spoiler.id }, { kind: 'anime', id: entry.anime_id }, { kind: 'character', id: character.id }, { kind: 'note', id: removedNote.id }] });
  await call('DELETE', `/api/v1/memory/notes/${removedNote.id}?version=${removedNote.version}`);
  await page.goto(origin + '/memory#notes');
  await click(page.getByRole('navigation', { name: '记忆分类', exact: true }).getByRole('button', { name: '收藏', exact: true }));
  assert.equal(new URL(page.url()).hash, '#collections');
  const more = page.getByRole('button', { name: '更多整理工具', exact: true });
  await click(more);
  await page.getByRole('menu', { name: '更多整理工具' }).waitFor();
  await axe('memory navigation menu');
  await page.locator('body').ariaSnapshot(); await page.keyboard.press('Escape');
  await page.getByRole('menu').waitFor({ state: 'detached' });
  assert.ok(await more.evaluate(el => el === document.activeElement));
  await click(more);
  await click(page.getByRole('menuitem', { name: '角色', exact: true }));
  await page.getByRole('heading', { name: '记住那些角色', exact: true }).waitFor();
  assert.equal(new URL(page.url()).hash, '#characters');
  assert.equal(await more.innerText(), '角色');
  await page.goBack();
  await page.getByRole('heading', { name: '收藏小册', exact: true }).waitFor();
  mark('Text navigation switches sections and supports browser history; the tools menu restores focus and identifies the active tool.');
  await page.goto(origin + '/memory#collections');
  const book = page.getByRole('button', { name: `翻开 ${collection.title}`, exact: true });
  await book.waitFor();
  await capture('01-shelf');
  await axe('collection shelf');
  await page.locator('body').ariaSnapshot(); await book.focus(); await page.keyboard.press('Enter');
  await page.getByRole('heading', { name: '翻开这些故事' }).waitFor();
  await page.getByText('这份内容已不在记忆库', { exact: true }).waitFor();
  assert.equal(await page.getByRole('dialog').locator('input:visible,select:visible,textarea:visible').count(), 0);
  await capture('02-collection-reader');
  await axe('collection reader');
  const liveBook = (await call('GET', '/api/v1/memory/collections')).items.find(c => c.id === collection.id);
  assert.equal(liveBook.version, collection.version);
  await click(page.locator('.collection-contents').getByRole('button').filter({ hasText: note.title }));
  await page.getByText(note.body, { exact: true }).waitFor();
  await capture('03-collection-note');
  await click(page.getByRole('button', { name: '返回小册目录', exact: true }));
  await click(page.locator('.collection-contents').getByRole('button').filter({ hasText: spoiler.title }));
  await page.getByText('展开剧透内容', { exact: true }).waitFor();
  assert.equal(await page.getByText(spoiler.body, { exact: true }).isVisible(), false);
  await click(page.getByText('展开剧透内容', { exact: true }));
  assert.equal(await page.getByText(spoiler.body, { exact: true }).isVisible(), true);
  await close();
  assert.ok(await book.evaluate(el => el === document.activeElement), 'Escape restores the book trigger focus');
  mark('Opening and reading a collection makes no edits, respects spoilers, handles missing references and restores keyboard focus.');

  await click(book);
  await click(page.getByRole('button', { name: '编辑小册', exact: true }));
  await fill(page.getByLabel('这本小册的故事'), '这本小册经过编辑，顺序也重新整理过。');
  await click(page.locator('.memory-selected-items li').last().getByRole('button', { name: '移出', exact: true }));
  await click(page.getByRole('button', { name: '下移第 1 项', exact: true }));
  await click(page.getByRole('button', { name: '保存收藏夹', exact: true }));
  await page.getByRole('dialog').waitFor({ state: 'detached' });
  const edited = (await call('GET', '/api/v1/memory/collections')).items.find(c => c.id === collection.id);
  assert.equal(edited.items[0].id, spoiler.id);
  assert.equal(edited.items[1].id, note.id);
  assert.equal(edited.description, '这本小册经过编辑，顺序也重新整理过。');
  await click(book);
  await page.locator('.collection-contents').getByRole('button').first().waitFor();
  assert.ok((await page.locator('.collection-contents li').first().innerText()).includes(spoiler.title));
  await close();
  mark('The explicit editor persists description and item order, and the reader reflects saved changes.');

  await page.goto(origin + '/memory#notes');
  await click(page.getByRole('button', { name: '写札记', exact: true }));
  assert.equal(await page.getByRole('dialog').locator('input:visible,textarea:visible,select:visible').count(), 2);
  await fill(page.getByLabel('记忆标题', { exact: true }), '写给未来的自己');
  await fill(page.getByLabel('记忆正文', { exact: true }), '不需要记得具体日期，也能把这一页好好留下。');
  await capture('04-note-composer');
  await axe('note composer');
  await click(page.getByRole('button', { name: '保存记忆', exact: true }));
  await page.getByRole('dialog').waitFor({ state: 'detached' });
  const saved = (await call('GET', '/api/v1/memory/notes')).items.find(n => n.title === '写给未来的自己');
  assert.equal(saved.time_precision, 'unknown'); assert.equal(saved.occurred_on, ''); assert.equal(saved.visibility, 'private');
  mark('The paper-like composer retains labelled inputs and saves private, undated recollections without invented facts.');

  await page.goto(origin);
  await click(page.getByRole('button', { name: `查看 ${entry.title}`, exact: true }));
  await page.getByText('观看细节还没有补记', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '搜索并绑定 Bangumi', exact: true }).isVisible(), false);
  await capture('05-entry-detail');
  await axe('entry detail');
  await click(page.locator('.detail-tools > summary'));
  assert.equal(await page.getByRole('button', { name: '搜索并绑定 Bangumi', exact: true }).isVisible(), true);
  await click(page.getByRole('button', { name: '修改评分与记录', exact: true }));
  await page.getByLabel('番剧名称').waitFor();
  assert.equal(await page.getByLabel('番剧名称').inputValue(), entry.title);
  await close();
  mark('Entry details lead with recollections; sharing/source settings remain accessible and direct editing still opens.');

  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto(origin + '/memory#collections'); await book.waitFor(); await noOverflow();
    await click(more); await page.getByRole('menu').waitFor(); await noOverflow();
    await click(page.getByRole('menuitem', { name: '角色', exact: true }));
    await page.getByRole('heading', { name: '记住那些角色', exact: true }).waitFor();
    await click(page.getByRole('navigation', { name: '记忆分类', exact: true }).getByRole('button', { name: '收藏', exact: true }));
    await click(book); await page.getByRole('heading', { name: '翻开这些故事' }).waitFor(); await noOverflow();
    await capture(`06-collection-mobile-${width}`);
    if (width === 390) await axe('mobile collection reader');
    await close();
    await page.goto(origin + '/memory#notes');
    await click(page.getByRole('button', { name: '写札记', exact: true }));
    await fill(page.getByLabel('记忆标题', { exact: true }), '在手机上也能安心写下回忆');
    await noOverflow(); await capture(`07-composer-mobile-${width}`);
    if (width === 390) await axe('mobile note composer');
    await close();
    await page.goto(origin); await click(page.getByRole('button', { name: `查看 ${entry.title}`, exact: true }));
    await page.getByText('观看细节还没有补记', { exact: true }).waitFor(); await noOverflow();
    await capture(`08-detail-mobile-${width}`); await close();
  }
  mark('Collection reading, note writing and entry details fit 390 px and 320 px viewports.');

  await page.goto(origin + '/memory#collections'); await click(book);
  await click(page.getByRole('button', { name: '移除收藏夹', exact: true }));
  await page.getByRole('button', { name: '确认移除收藏夹', exact: true }).waitFor();
  assert.ok((await call('GET', '/api/v1/memory/collections')).items.some(c => c.id === collection.id));
  await click(page.getByRole('button', { name: '确认移除收藏夹', exact: true }));
  await page.getByRole('dialog').waitFor({ state: 'detached' });
  assert.equal((await call('GET', '/api/v1/memory/notes')).items.find(n => n.id === note.id).body, note.body);
  assert.equal((await call('GET', `/api/v1/entries/${entry.id}`)).id, entry.id);
  mark('Removing a collection still requires confirmation and preserves its underlying notes and anime.');
  assert.deepEqual(report.errors, []);
  report.status = 'PASS';
} catch (error) {
  report.status = 'FAIL'; report.failure = error.stack;
  await capture('failure').catch(() => {});
  throw error;
} finally {
  await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2) + '\n');
  await browser.close();
}
