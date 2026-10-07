import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createServer } from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { installInstance, backupInstance, updateInstance, rollbackInstance, instanceStatus, removeTestInstance } from './instance.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
async function port() { const s = createServer(); await new Promise((resolve, reject) => { s.once('error', reject); s.listen(0, '127.0.0.1', resolve); }); const p = s.address().port; await new Promise(resolve => s.close(resolve)); return p; }
function client(origin) { let cookie = ''; return async (method, url, body, want = 200, raw = false) => {
  const r = await fetch(origin + url, { method, headers: { Origin: origin, Cookie: cookie, 'Content-Type': raw ? 'application/octet-stream' : 'application/json' }, body: body === undefined ? undefined : raw ? body : JSON.stringify(body), signal: AbortSignal.timeout(15000) });
  if (r.headers.get('set-cookie')) cookie = r.headers.get('set-cookie').split(';')[0]; const out = await r.json(); assert.equal(r.status, want, `${method} ${url}: ${JSON.stringify(out).slice(0,250)}`); return out;
}; }
const config = async name => JSON.parse(await readFile(path.join(root, '.local/instances', name, 'config.json'), 'utf8'));

export async function pluginSmoke({ image, previousImage = image, incompatibleImage, browser = false, packageFile = path.join(root, '.local/output/watch-history-text.animemo-plugin') }) {
  if (!image) throw new Error('Set ANIMEMO_CANDIDATE_IMAGE to the locally built plugin-enabled image.');
  const names = [0,1].map(() => `probe-${randomBytes(6).toString('hex')}`), created = [];
  const report = { status: 'RUNNING', started_at: new Date().toISOString(), checks: [] };
  const mark = message => { report.checks.push(message); console.log(`Plugin check: ${message}`); };
  const source = JSON.parse(await readFile(packageFile, 'utf8'));
  try {
    created.push(names[0]); const a = await installInstance({ name: names[0], image: previousImage, port: await port() }); const original = await config(names[0]);
    const credentials = { email: `plugin-probe-${randomUUID()}@example.test`, password: `probe-${randomUUID()}` };
    let call = client(a.origin); await call('POST', '/api/v1/setup', { ...credentials, display_name: '插件验收管理', token: original.setupToken }, 201);
    if (previousImage !== image) { await updateInstance(names[0], image); call = client(a.origin); await call('POST', '/api/v1/auth/login', credentials); mark('upgrade pre-plugin database through read-only preflight and additive migration'); }
    if (browser) { const { pluginBrowser } = await import('./plugin-browser.mjs'); await pluginBrowser({ root, origin: a.origin, credentials, packageFile }); mark('real browser install, activate, TXT preview, confirmation, disable and mobile layout'); }
    else { await call('POST', '/api/v1/admin/plugins', source, 201); const installed = (await call('GET', '/api/v1/admin/plugins')).items[0]; await call('POST', '/api/v1/admin/plugins/watch-history-text', { action: 'activate', version: '1.0.0', revision: installed.revision }); }
    const before = (await call('GET', '/api/v1/plugins')).items[0]; assert.equal(before.manifest.version, '1.0.0');
    const backup = await backupInstance(names[0]); created.push(names[1]); const b = await installInstance({ name: names[1], backup, port: await port() }); const restored = client(b.origin); await restored('POST', '/api/v1/auth/login', credentials);
    const cloned = (await restored('GET', '/api/v1/plugins')).items[0]; assert.equal(cloned.digest, before.digest); assert.equal(cloned.enabled, true);
    const job = await restored('POST', '/api/v1/plugins/watch-history-text/imports?filename=2026.txt', '10月3日\n首刷 恢复后的插件 共12集\n', 202, true);
    for (let n = 0; n < 60; n++) { const state = await restored('GET', `/api/v1/imports/${job.id}`); if (state.state === 'ready') { assert.equal(state.preview.records, 1); break; } assert.notEqual(state.state, 'failed', state.error); assert.ok(n < 59); await new Promise(resolve => setTimeout(resolve, 150)); }
    mark('database snapshot restores immutable WASM bytes, digest and activation; restored module executes');
    if (incompatibleImage) {
      const old = await config(names[0]); await assert.rejects(() => updateInstance(names[0], incompatibleImage), /original image and database are running again/);
      const state = await instanceStatus(names[0]); assert.equal(state.image, old.image); assert.equal(state.database, old.database); assert.ok(state.services.some(s => s.service === 'app' && s.health === 'healthy'));
      assert.equal((await call('GET', '/api/v1/plugins')).items[0].digest, before.digest); mark('candidate with incompatible host API rejected before migration; original plugin and application recover');
    }
    await updateInstance(names[0], image); call = client(a.origin); await call('POST', '/api/v1/auth/login', credentials);
    assert.equal((await call('GET', '/api/v1/plugins')).items[0].digest, before.digest);
    const newer = structuredClone(source); newer.manifest.version = '1.1.0'; await call('POST', '/api/v1/admin/plugins', newer, 201);
    const active = (await call('GET', '/api/v1/plugins')).items[0]; await call('POST', '/api/v1/admin/plugins/watch-history-text', { action: 'activate', version: '1.1.0', revision: active.revision });
    assert.equal((await call('GET', '/api/v1/plugins')).items[0].manifest.version, '1.1.0');
    await rollbackInstance(names[0], true); call = client(a.origin); await call('POST', '/api/v1/auth/login', credentials);
    const rolled = (await call('GET', '/api/v1/plugins')).items[0]; assert.equal(rolled.digest, before.digest); assert.equal(rolled.manifest.version, '1.0.0'); assert.equal((await call('GET', '/api/v1/admin/plugins')).items.length, 1);
    mark('host update preserves plugin; instance rollback restores previous package inventory and activation together'); report.status = 'PASS';
  } catch (error) { report.status = 'FAIL'; report.error = error.message; throw error; }
  finally {
    report.cleanup = []; for (const name of created.reverse()) { try { await removeTestInstance(name); report.cleanup.push({ name, removed: true }); } catch (error) { report.cleanup.push({ name, removed: false, error: error.message }); } }
    if (report.cleanup.some(c => !c.removed)) report.status = 'FAIL'; report.finished_at = new Date().toISOString(); await mkdir(path.join(root, '.local/output'), { recursive: true }); await writeFile(path.join(root, '.local/output/plugin-smoke.json'), JSON.stringify(report, null, 2) + '\n');
    if (report.cleanup.some(c => !c.removed)) throw new Error('Plugin test cleanup incomplete; see .local/output/plugin-smoke.json.');
  }
  return report;
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) pluginSmoke({ image: process.env.ANIMEMO_CANDIDATE_IMAGE, previousImage: process.env.ANIMEMO_PREVIOUS_IMAGE || process.env.ANIMEMO_CANDIDATE_IMAGE, incompatibleImage: process.env.ANIMEMO_INCOMPATIBLE_IMAGE, browser: process.env.ANIMEMO_PLUGIN_BROWSER === '1' }).then(r => console.log(`Plugin operations ${r.status}`)).catch(e => { console.error(e.message); process.exitCode = 1; });
