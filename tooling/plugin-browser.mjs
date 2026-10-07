import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

// Optional real-browser stage for the isolated plugin instance smoke. Browser
// tooling belongs in .local and is not a production dependency.
export async function pluginBrowser({ root, origin, credentials, packageFile }) {
  const { chromium } = await import(pathToFileURL(process.env.ANIMEMO_PLAYWRIGHT || path.join(root, '.local/tools/browser/node_modules/playwright/index.mjs')));
  const browser = await chromium.launch({ headless: true, ...(process.env.ANIMEMO_BROWSER ? { executablePath: process.env.ANIMEMO_BROWSER } : {}) });
  const directory = path.join(root, '.local/output/browser'); await mkdir(directory, { recursive: true });
  const errors = [], checks = []; let stage = 'login';
  const admin = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const user = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  for (const p of [admin, user]) { p.setDefaultTimeout(20000); p.on('pageerror', e => errors.push(e.message)); }
  try {
    await admin.goto(origin + '/admin');
    await admin.getByRole('button', { name: '已有账号', exact: true }).click();
    await admin.getByLabel('邮箱地址').fill(credentials.email); await admin.getByLabel(/^密码/).fill(credentials.password);
    await admin.getByRole('button', { name: '进入我的手账' }).click(); await admin.getByRole('button', { name: '插件', exact: true }).click();
    stage = 'install'; await admin.getByLabel('选择插件包').setInputFiles(packageFile);
    await admin.getByRole('button', { name: '已审核，安装此版本' }).click();
    await admin.getByRole('button', { name: '启用此版本', exact: true }).click();
    await admin.getByText('当前启用 · watch-history-text', { exact: true }).waitFor();
    await admin.screenshot({ path: path.join(directory, 'plugins-admin.png'), fullPage: true }); checks.push('administrator reviews, installs and enables immutable package through UI');
    stage = 'register'; await user.goto(origin);
    await user.getByLabel('怎么称呼你').fill('插件验收'); await user.getByLabel('邮箱地址').fill(`plugin-browser-${randomUUID()}@example.test`); await user.getByLabel(/^密码/).fill(`synthetic-${randomUUID()}`);
    await user.getByRole('button', { name: '创建我的手账' }).click(); await user.getByRole('button', { name: '导入与备份', exact: true }).click();
    stage = 'convert'; await user.getByLabel('导入插件', { exact: true }).selectOption('watch-history-text');
    await user.getByLabel('选择 UTF-8 文本').setInputFiles({ name: '2026观看记录.txt', mimeType: 'text/plain', buffer: Buffer.from('10月1日\n首刷 夏目友人帐 第1-3集\n10月2日\n二刷 夏目友人帐 第1集 -- 重温\n') });
    await user.getByRole('button', { name: '解析并生成预览' }).click();
    await user.getByRole('button', { name: '确认导入 1 部', exact: true }).waitFor();
    await user.getByText('核对观看记录（最多显示 100 条）', { exact: true }).click(); await user.getByText('2026-10-02 · 第 1–1 话 · 第 2 刷', { exact: true }).waitFor();
    assert.equal((await (await user.request.get(origin + '/api/v1/entries')).json()).total, 0);
    await user.screenshot({ path: path.join(directory, 'plugin-import-preview.png'), fullPage: true }); checks.push('TXT parsing, dates/episodes/rewatch preview and no writes before confirmation');
    stage = 'resume'; await user.reload(); await user.getByRole('button', { name: '导入与备份', exact: true }).click();
    await user.getByRole('button', { name: '确认导入 1 部', exact: true }).click(); await user.getByText('已导入 1 部番剧，关闭窗口后即可查看。', { exact: true }).waitFor();
    const entries = await (await user.request.get(origin + '/api/v1/entries')).json(); assert.equal(entries.total, 1); assert.equal(entries.items[0].watched_episodes, 3); assert.equal(entries.items[0].visibility, 'private');
    const history = await (await user.request.get(origin + '/api/v1/history/page')).json(); assert.equal(history.total, 2); assert.ok(history.items.some(r => r.rewatch === 2 && r.note === '重温'));
    checks.push('preview survives reload; confirmed private import retains two watch records');
    stage = 'disabled'; await admin.getByRole('button', { name: '停用', exact: true }).click(); await admin.getByText('当前版本已停用 · watch-history-text', { exact: true }).waitFor();
    await user.reload(); await user.getByRole('button', { name: '导入与备份', exact: true }).click(); await user.waitForLoadState('networkidle'); assert.equal(await user.getByLabel('导入插件', { exact: true }).count(), 0);
    await admin.getByRole('button', { name: '启用此版本', exact: true }).click(); await admin.getByText('当前启用 · watch-history-text', { exact: true }).waitFor(); checks.push('disable removes converter; re-enable restores availability');
    stage = 'mobile'; await user.setViewportSize({ width: 390, height: 844 }); await user.reload(); await user.getByRole('button', { name: '导入与备份', exact: true }).click(); await user.getByLabel('导入插件', { exact: true }).selectOption('watch-history-text');
    assert.ok(await user.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)); await user.screenshot({ path: path.join(directory, 'plugin-import-mobile.png'), fullPage: true });
    assert.deepEqual(errors, []); checks.push('mobile import layout; zero page errors');
    await writeFile(path.join(directory, 'plugin-report.json'), JSON.stringify({ status: 'PASS', checks, errors }, null, 2) + '\n');
  } catch (error) {
    await admin.screenshot({ path: path.join(directory, 'plugin-admin-failure.png'), fullPage: true }); await user.screenshot({ path: path.join(directory, 'plugin-user-failure.png'), fullPage: true });
    await writeFile(path.join(directory, 'plugin-report.json'), JSON.stringify({ status: 'FAIL', stage, error: error.message, errors }, null, 2) + '\n'); throw error;
  } finally { await browser.close(); }
}
