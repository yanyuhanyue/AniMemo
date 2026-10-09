import assert from 'node:assert/strict';
import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createServer } from 'node:net';
import { execFileSync } from 'node:child_process';
import { installInstance, backupInstance, updateInstance, recoverInstance, instanceStatus, removeTestInstance } from './instance.mjs';
import { readOperation, advanceOperation } from './operation-journal.mjs';

const image = process.env.ANIMEMO_CANDIDATE_IMAGE;
const previousImage = process.env.ANIMEMO_PREVIOUS_IMAGE;
if (!image) throw new Error('Set ANIMEMO_CANDIDATE_IMAGE to a locally built stage 3 image.');
const names = [0, 1, 2].map(() => `probe-${randomBytes(6).toString('hex')}`), created = [];
const report = { status: 'RUNNING', started_at: new Date().toISOString(), image, previous_image: previousImage, checks: [], cleanup: [] };
const mark = name => { report.checks.push(name); console.log(`Stage 3: ${name}`); };
const docker = args => execFileSync('docker', args, { encoding: 'utf8', stdio: ['ignore','pipe','pipe'], timeout: 30000 });
const config = async name => JSON.parse(await readFile(`.local/instances/${name}/config.json`, 'utf8'));
async function port() { const s = createServer(); await new Promise(r => s.listen(0, '127.0.0.1', r)); const p = s.address().port; await new Promise(r => s.close(r)); return p; }
function client(origin) { let cookie = ''; return async (method, url, body, want = 200, raw = false, binary = false) => {
  const r = await fetch(origin + url, { method, headers: { Origin: origin, Cookie: cookie, 'Content-Type': raw ? 'application/octet-stream' : 'application/json' }, body: body === undefined ? undefined : raw ? body : JSON.stringify(body), signal: AbortSignal.timeout(15000) });
  if (r.headers.get('set-cookie')) cookie = r.headers.get('set-cookie').split(';')[0];
  if (binary) { assert.equal(r.status, want, `${method} ${url}`); assert.equal(r.headers.get('cache-control'), 'private, no-store'); return { bytes: Buffer.from(await r.arrayBuffer()), type: r.headers.get('content-type') }; }
  const text = await r.text(); assert.equal(r.status, want, `${method} ${url}: ${text.slice(0, 200)}`); return text ? JSON.parse(text) : null;
}; }
async function poll(check) { for (let n = 0; n < 80; n++) { const out = await check(); if (out) return out; await new Promise(r => setTimeout(r, 250)); } throw new Error('Timed out'); }
async function jobState(call, id, state) { return poll(async () => { const j = await call('GET', `/api/v1/imports/${id}`); assert.notEqual(j.state, 'failed', j.error); return j.state === state && j; }); }
try {
  if (previousImage && previousImage !== image) {
    created.push(names[2]);
    const prior = await installInstance({ name: names[2], image: previousImage, port: await port() });
    const priorConfig = await config(names[2]);
    const credentials = { email: `${randomUUID()}@example.test`, password: `upgrade-${randomUUID()}` };
    let use = client(prior.origin);
    await use('POST', '/api/v1/setup', { ...credentials, display_name: '跨版本扩展验收', token: priorConfig.setupToken }, 201);
    const old = (await use('GET', '/api/v1/admin/plugins')).items.find(p => p.manifest.slug === 'watch-history-text' && p.active);
    await use('POST', '/api/v1/admin/plugins/watch-history-text', { action: 'activate', version: old.manifest.version, revision: old.revision });
    await updateInstance(names[2], image);
    use = client(prior.origin); await use('POST', '/api/v1/auth/login', credentials);
    const inventory = (await use('GET', '/api/v1/admin/plugins')).items;
    const retained = inventory.find(p => p.manifest.slug === old.manifest.slug && p.active);
    assert.equal(retained.manifest.version, old.manifest.version);
    assert.equal(retained.digest, old.digest); assert.equal(retained.enabled, true); assert.equal(retained.health, 'ready');
    const added = inventory.find(p => p.manifest.slug === 'notes-gallery');
    assert.ok(added, 'old converter identity must not block the new bundled theme');
    assert.equal(added.publisher_id, 'ANIMEMO_FIRST_PARTY'); assert.equal(added.enabled, false);
    const job = await use('POST', '/api/v1/plugins/watch-history-text/imports?filename=2026.txt', '10月3日\n首刷 升级后仍使用原转换器 共12集\n', 202, true);
    await jobState(use, job.id, 'ready'); await use('POST', `/api/v1/imports/${job.id}`, { action: 'apply' }); await jobState(use, job.id, 'done');
    assert.equal((await use('GET', '/api/v1/entries')).total, 1);
    mark('cross-version update preserves the enabled immutable converter and installs the new official theme without switching versions');
    await removeTestInstance(names[2]); created.pop();
    report.cleanup.push({ name: names[2], removed: true });
  }
  created.push(names[0]); const a = await installInstance({ name: names[0], image, port: await port() }); let c = await config(names[0]);
  let call = client(a.origin); const credentials = { email: `${randomUUID()}@example.test`, password: `stage3-${randomUUID()}` };
  await call('POST', '/api/v1/setup', { ...credentials, display_name: '阶段三验收', token: c.setupToken }, 201);
  let plugin = (await call('GET', '/api/v1/admin/plugins')).items.find(p => p.manifest.slug === 'watch-history-text');
  assert.equal(plugin.publisher_id, 'ANIMEMO_FIRST_PARTY'); assert.equal(plugin.distribution, 'bundled'); assert.equal(plugin.enabled, false);
  const activate = async (use, p) => use('POST', `/api/v1/admin/plugins/${p.manifest.slug}`, { action: 'activate', version: p.manifest.version, revision: p.revision });
  await activate(call, plugin);
  const converted = await call('POST', '/api/v1/plugins/watch-history-text/imports?filename=2026.txt', '10月3日\n首刷 进程隔离验收 共12集\n', 202, true);
  await jobState(call, converted.id, 'ready'); await call('POST', `/api/v1/imports/${converted.id}`, { action: 'apply' }); await jobState(call, converted.id, 'done');
  assert.equal((await call('GET', '/api/v1/entries')).total, 1);
  const theme = (await call('GET', '/api/v1/admin/plugins')).items.find(p => p.manifest.slug === 'notes-hanami');
  assert.equal(theme.publisher_id, 'ANIMEMO_FIRST_PARTY'); assert.equal(theme.enabled, false);
  await activate(call, theme);
  const themeOptions = await call('GET', '/api/v1/themes');
  await call('PUT', '/api/v1/themes/selection', { slug: theme.manifest.slug, revision: themeOptions.items[0].revision });
  assert.equal((await call('GET', '/api/v1/themes')).selected_slug, 'notes-hanami');
  const gallery = (await call('GET', '/api/v1/admin/plugins')).items.find(p => p.manifest.slug === 'notes-gallery');
  assert.equal(gallery.publisher_id, 'ANIMEMO_FIRST_PARTY'); assert.equal(gallery.enabled, false);
  const assetPath = (p, asset) => `/api/v1/themes/${p.manifest.slug}/${p.manifest.version}/assets/${asset.name}`;
  const firstAsset = gallery.manifest.notes_theme.presentation.assets[0];
  await call('GET', assetPath(gallery, firstAsset), undefined, 404);
  const checkAssets = async use => {
    for (const asset of gallery.manifest.notes_theme.presentation.assets) {
      const actual = await use('GET', assetPath(gallery, asset), undefined, 200, false, true);
      assert.equal(actual.type, asset.content_type);
      assert.equal(createHash('sha256').update(actual.bytes).digest('hex'), asset.sha256);
    }
  };
  await activate(call, gallery); await checkAssets(call);
  const available = (await call('GET', '/api/v1/themes')).items.find(p => p.manifest.slug === 'notes-gallery');
  await call('PUT', '/api/v1/themes/selection', { slug: available.manifest.slug, revision: available.revision });
  mark('bundled converter and both theme formats bound to Core; import, template selection and authenticated asset bytes work');
  docker(['stop', `${c.project}-worker-1`]);
  const queued = await call('POST', '/api/v1/imports?format=csv', 'title\nWorker停止期间的记录\n', 202, true);
  await new Promise(r => setTimeout(r, 1500)); assert.equal((await call('GET', `/api/v1/imports/${queued.id}`)).state, 'validating');
  assert.equal((await call('GET', '/api/v1/entries')).total, 1);
  docker(['start', `${c.project}-worker-1`]); await jobState(call, queued.id, 'ready');
  await call('POST', `/api/v1/imports/${queued.id}`, { action: 'apply' }); await jobState(call, queued.id, 'done');
  await poll(async () => { const r = await call('GET', '/api/v1/admin/runtime'); return r.pending === 0 && r.running === 0; });
  mark('web does not execute queued work; worker restart resumes durable jobs');
  const backup = await backupInstance(names[0]); created.push(names[1]); const b = await installInstance({ name: names[1], backup, port: await port() }); const restored = client(b.origin);
  await restored('POST', '/api/v1/auth/login', credentials);
  const clone = (await restored('GET', '/api/v1/admin/plugins')).items.find(p => p.manifest.slug === 'watch-history-text'); assert.equal(clone.enabled, false); assert.equal(clone.health, 'review_required'); assert.equal((await restored('GET', '/api/v1/entries')).total, 2);
  await activate(restored, clone);
  const restoredTheme = (await restored('GET', '/api/v1/admin/plugins')).items.find(p => p.manifest.slug === 'notes-hanami');
  assert.equal(restoredTheme.enabled, false);
  await activate(restored, restoredTheme);
  assert.equal((await restored('GET', '/api/v1/themes')).selected_slug, '', 'restoration clears personal activation as well as package activation');
  const restoredGallery = (await restored('GET', '/api/v1/admin/plugins')).items.find(p => p.manifest.slug === 'notes-gallery');
  assert.equal(restoredGallery.enabled, false); assert.equal(restoredGallery.digest, gallery.digest);
  await restored('GET', assetPath(gallery, firstAsset), undefined, 404);
  await activate(restored, restoredGallery); await checkAssets(restored);
  mark('clone restore preserves core data, template, image, font and license bytes; explicit reactivation required');
  await updateInstance(names[0], image); call = client(a.origin); await call('POST', '/api/v1/auth/login', credentials); c = await config(names[0]);
  assert.equal((await call('GET', '/api/v1/themes')).selected_slug, 'notes-gallery', 'normal update preserves personal appearance');
  await checkAssets(call);
  const operationFile = `.local/instances/${names[0]}/operation.json`, completed = await readOperation(operationFile);
  assert.equal(completed.phase, 'completed'); await call('POST', '/api/v1/entries', { title: '中断后仍可救援的新写入' }, 201);
  // Persist the exact state an interruption leaves after switching databases.
  await advanceOperation(operationFile, completed, 'switching'); docker(['stop', `${c.project}-app-1`, `${c.project}-worker-1`]);
  const recovery = await recoverInstance(names[0]); assert.equal(recovery.operation.phase, 'recovered'); assert.ok(recovery.operation.rescue);
  call = client(a.origin); await call('POST', '/api/v1/auth/login', credentials); assert.equal((await call('GET', '/api/v1/entries')).total, 2);
  mark('persisted interrupted update recovers original database and captures new writes');
  c = await config(names[0]);
  docker(['exec', `${c.project}-db-1`, 'psql', '-U', 'animemo', '-d', c.database, '-v', 'ON_ERROR_STOP=1', '-c', "UPDATE plugin_releases SET module=decode('00','hex') WHERE slug='watch-history-text'"]);
  docker(['restart', `${c.project}-app-1`]);
  await poll(async () => { try { return (await fetch(a.origin + '/api/ready')).ok; } catch { return false; } });
  const quarantined = (await call('GET', '/api/v1/admin/plugins')).items.find(p => p.manifest.slug === 'watch-history-text'); assert.equal(quarantined.health, 'quarantined'); assert.equal(quarantined.enabled, false);
  await call('POST', '/api/v1/admin/plugins/watch-history-text', { action: 'activate', version: quarantined.manifest.version, revision: quarantined.revision }, 400);
  assert.equal((await call('GET', '/api/v1/entries')).total, 2);
  mark('damaged optional extension quarantined; core stays available and integrity blocks activation');
  report.status = 'PASS';
} catch (e) { report.status = 'FAIL'; report.error = e.message; throw e; }
finally {
  for (const name of created.reverse()) { try { await removeTestInstance(name); report.cleanup.push({ name, removed: true }); } catch (e) { report.cleanup.push({ name, removed: false, error: e.message }); report.status = 'FAIL'; } }
  report.finished_at = new Date().toISOString(); await mkdir('.local/output', { recursive: true }); await writeFile('.local/output/stage3-smoke.json', JSON.stringify(report, null, 2) + '\n');
  if (report.cleanup.some(item => !item.removed)) throw new Error('Cleanup incomplete; inspect stage3-smoke report.');
}
