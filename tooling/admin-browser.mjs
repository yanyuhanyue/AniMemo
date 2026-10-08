// Run against a synthetic acceptance instance. Browser dependencies stay in .local.
import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

const root = process.cwd();
const { origin, email, password } = JSON.parse(await readFile(process.env.ANIMEMO_REVIEW_ACCESS || '.local/output/stage3-review-access.json', 'utf8'));
const { chromium } = await import(pathToFileURL(process.env.ANIMEMO_PLAYWRIGHT || path.join(root, '.local/tools/browser/node_modules/playwright/index.mjs')));
const { default: AxeBuilder } = await import(pathToFileURL(path.join(root, '.local/tools/browser/node_modules/@axe-core/playwright/dist/index.mjs')));
const output = '.local/output/browser/admin-redesign';
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.ANIMEMO_BROWSER || '/usr/bin/chromium', args: ['--no-sandbox'] });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
const page = await context.newPage();
page.setDefaultTimeout(15000);
const report = { status: 'RUNNING', checks: [], layouts: [], page_errors: [], accessibility: [] };
page.on('pageerror', error => report.page_errors.push(error.message));
const tabs = [['控制台概览', '00-overview'], ['用户与公开审核', '01-users'], ['插件', '02-plugins'], ['健康与维护', '03-health'], ['站点设置', '04-site'], ['资源与专栏审核', '05-resources'], ['标签预设', '06-presets'], ['操作审计', '07-audit'], ['成就管理', '08-achievements']];
const nav = label => page.getByRole('navigation', { name: '管理导航' }).getByRole('button', { name: label, exact: true });
const mark = text => { report.checks.push(text); console.log(text); };
async function idle() { await page.waitForLoadState('networkidle'); }
async function fits(label) { assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `horizontal overflow: ${label}`); }
const presetName = `界面验收-${randomUUID().slice(0, 8)}`;
let originalPlugin;
try {
  await page.goto(origin + '/admin');
  await page.getByRole('button', { name: '已有账号', exact: true }).click();
  await page.getByLabel('邮箱地址').fill(email); await page.getByLabel(/^密码/).fill(password);
  await page.getByRole('button', { name: '进入我的手账' }).click();
  await page.getByRole('navigation', { name: '管理导航' }).waitFor();
  for (const width of [1440, 390, 768, 320]) {
    await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
    for (const [label, file] of tabs) {
      await nav(label).click(); await idle();
      await page.evaluate(async () => { await document.fonts.ready; document.activeElement?.blur(); window.scrollTo(0, 0); });
      await fits(`${width} ${label}`);
      report.layouts.push({ label, width, fits: true });
      if (width === 1440 || width === 390) {
        await page.screenshot({ path: `${output}/${file}-${width}.png`, fullPage: true, animations: 'disabled' });
        const axe = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
        report.accessibility.push({ label, width, violations: axe.violations });
        assert.equal(axe.violations.length, 0, `${label}: ${axe.violations.map(v => v.id).join(', ')}`);
      }
    }
    mark(`${width}px: all ${tabs.length} pages render without horizontal page overflow`);
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await nav('用户与公开审核').click(); await page.getByLabel('搜索用户').fill('no-user-ui-review-match');
  await page.getByRole('heading', { name: '没有符合条件的用户' }).waitFor();
  await page.getByLabel('搜索用户').fill('');
  await page.getByRole('button', { name: /^管理账号 / }).first().click();
  const dialog = page.getByRole('dialog'); await dialog.waitFor();
  assert.equal(await page.getByRole('button', { name: '确认执行', exact: true }).isEnabled(), false);
  await page.getByLabel('管理操作', { exact: true }).selectOption('disable');
  assert.equal(await page.getByRole('button', { name: '确认执行', exact: true }).isEnabled(), true);
  await page.screenshot({ path: `${output}/08-user-confirmation.png` });
  await page.keyboard.press('Escape'); await dialog.waitFor({ state: 'hidden' });
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')?.startsWith('管理账号 ')), true);
  mark('user search, explicit action selection, Escape cancellation and focus restoration');
  await nav('健康与维护').click(); await page.reload();
  assert.equal(await nav('健康与维护').getAttribute('aria-current'), 'page');
  await nav('插件').click(); await page.goBack();
  assert.equal(await nav('健康与维护').getAttribute('aria-current'), 'page');
  await page.getByRole('link', { name: '跳到管理内容' }).focus(); await page.keyboard.press('Enter');
  assert.equal(await nav('健康与维护').getAttribute('aria-current'), 'page');
  assert.equal(await page.locator('#admin-content').evaluate(e => document.activeElement === e), true);
  await page.getByRole('button', { name: '清理过期数据', exact: true }).click();
  await dialog.waitFor(); await page.getByRole('button', { name: '取消', exact: true }).click(); await dialog.waitFor({ state: 'hidden' });
  mark('hash survives reload/back; skip link keeps current page; maintenance cancellation');
  await nav('资源与专栏审核').click(); await page.getByLabel('资源类型', { exact: true }).selectOption('entry'); await idle();
  await page.screenshot({ path: `${output}/05-resources-entries-1440.png`, fullPage: true });
  await page.getByRole('button', { name: '检查内容', exact: true }).first().click();
  await dialog.waitFor(); await page.keyboard.press('Escape'); await dialog.waitFor({ state: 'hidden' });
  await page.getByRole('button', { name: /^管理内容 / }).first().click();
  await page.getByLabel('管理操作', { exact: true }).selectOption('trash');
  assert.equal(await page.getByLabel('操作说明').getAttribute('required'), '');
  await page.getByRole('button', { name: '取消', exact: true }).click(); await dialog.waitFor({ state: 'hidden' });
  mark('real resource preview; destructive action requires reason and can be cancelled');
  await nav('标签预设').click();
  await page.getByLabel('预设名称').fill(presetName); await page.getByLabel('预设颜色').fill('#8062b8');
  await page.getByRole('button', { name: '保存标签预设', exact: true }).click();
  await page.getByRole('button', { name: `修改 ${presetName}`, exact: true }).click();
  await page.getByLabel('预设颜色').fill('#e0a7c2');
  await page.getByRole('button', { name: '保存标签预设', exact: true }).click(); await idle();
  const presets = await (await page.request.get(origin + '/api/v1/presets')).json();
  assert.equal(presets.items.find(p => p.name === presetName)?.color, '#e0a7c2');
  await page.getByRole('button', { name: `删除 ${presetName}`, exact: true }).click();
  await page.getByRole('button', { name: `修改 ${presetName}`, exact: true }).waitFor({ state: 'hidden' });
  mark('synthetic preset create, edit and delete through UI, verified against real API');
  await nav('站点设置').click();
  const siteBefore = await (await page.request.get(origin + '/api/v1/site')).json();
  await page.getByRole('button', { name: '保存站点设置', exact: true }).click();
  await page.getByText('设置已保存', { exact: true }).waitFor();
  const siteAfter = await (await page.request.get(origin + '/api/v1/site')).json();
  for (const key of ['name', 'description', 'registration_open']) assert.equal(siteBefore[key], siteAfter[key]);
  assert.ok(siteAfter.version > siteBefore.version);
  mark('site form saves actual values without altering the existing configuration');
  await nav('插件').click();
  const inventory = await (await page.request.get(origin + '/api/v1/admin/plugins')).json();
  originalPlugin = inventory.items.find(p => p.manifest.slug === 'watch-history-text' && p.active);
  assert.ok(originalPlugin, 'synthetic instance needs the official TXT extension');
  const card = page.locator('.admin-plugin-card').filter({ hasText: 'watch-history-text' });
  await card.getByRole('button', { name: originalPlugin.enabled ? '停用' : '启用此版本', exact: true }).click();
  await card.getByText(originalPlugin.enabled ? '已停用' : '已启用', { exact: true }).waitFor();
  await card.getByRole('button', { name: originalPlugin.enabled ? '启用此版本' : '停用', exact: true }).click();
  await card.getByText(originalPlugin.enabled ? '已启用' : '已停用', { exact: true }).waitFor();
  await page.getByLabel('选择插件包').setInputFiles(process.env.ANIMEMO_PLUGIN_PACKAGE || '.local/output/watch-history-text.animemo-plugin');
  await dialog.waitFor(); await page.getByRole('button', { name: '已审核，安装此版本', exact: true }).waitFor();
  await page.getByRole('button', { name: '取消', exact: true }).click(); await dialog.waitFor({ state: 'hidden' });
  mark('official extension toggles and restores its state; local package review cancels without installing');
  await page.goto(origin + '/'); await page.getByRole('button', { name: '记下看过的番', exact: true }).waitFor(); await idle();
  await page.screenshot({ path: `${output}/journal-unchanged-1440.png`, fullPage: true });
  await fits('public journal desktop');
  await page.setViewportSize({ width: 390, height: 844 }); await fits('public journal mobile');
  mark('journal layout remains separate at desktop and mobile sizes');
  assert.deepEqual(report.page_errors, []);
  report.status = 'PASS';
} catch (error) {
  report.status = 'FAIL'; report.error = error.message;
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
  throw error;
} finally {
  // Clean only this run's synthetic preset; restore the extension state if a check failed.
  const response = await page.request.get(origin + '/api/v1/presets');
  if (response.ok()) {
    const preset = (await response.json()).items.find(item => item.name === presetName);
    if (preset) await page.request.delete(origin + '/api/v1/admin/presets', { headers: { Origin: origin }, data: preset });
  }
  if (originalPlugin) {
    const inventory = await (await page.request.get(origin + '/api/v1/admin/plugins')).json();
    const current = inventory.items.find(item => item.manifest.slug === originalPlugin.manifest.slug && item.active);
    if (current && current.enabled !== originalPlugin.enabled) {
      const restored = await page.request.post(origin + `/api/v1/admin/plugins/${current.manifest.slug}`, { headers: { Origin: origin }, data: { action: originalPlugin.enabled ? 'activate' : 'disable', version: current.manifest.version, revision: current.revision } });
      assert.ok(restored.ok(), 'restore original extension state');
    }
  }
  report.finished_at = new Date().toISOString();
  await writeFile(`${output}/verification.json`, JSON.stringify(report, null, 2) + '\n');
  await browser.close();
}
