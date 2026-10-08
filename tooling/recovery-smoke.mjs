import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { createServer } from 'node:net';
import { readFile, writeFile } from 'node:fs/promises';
import { installInstance, backupInstance, updateInstance, rollbackInstance, recoverInstance, removeTestInstance } from './instance.mjs';
import { readOperation, advanceOperation } from './operation-journal.mjs';

const image = process.env.ANIMEMO_CANDIDATE_IMAGE;
if (!image) throw new Error('Set ANIMEMO_CANDIDATE_IMAGE to the existing candidate.');
const names = [0, 1, 2].map(() => `probe-${randomBytes(6).toString('hex')}`), created = [];
const report = { status: 'RUNNING', checks: [] };
const config = async name => JSON.parse(await readFile(`.local/instances/${name}/config.json`, 'utf8'));
const journal = name => `.local/instances/${name}/operation.json`;
async function port() { const s = createServer(); await new Promise(r => s.listen(0, '127.0.0.1', r)); const p = s.address().port; await new Promise(r => s.close(r)); return p; }
function client(origin) {
  let cookie = '';
  return async (method, url, body, want = 200) => {
    const r = await fetch(origin + url, { method, headers: { Origin: origin, Cookie: cookie, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(10000) });
    if (r.headers.get('set-cookie')) cookie = r.headers.get('set-cookie').split(';')[0];
    const text = await r.text(); assert.equal(r.status, want, `${method} ${url}: ${text.slice(0, 100)}`); return text ? JSON.parse(text) : null;
  };
}
const mark = value => { report.checks.push(value); console.log(`Recovery: ${value}`); };
try {
  created.push(names[0]); const a = await installInstance({ name: names[0], image, port: await port() });
  let call = client(a.origin); const credentials = { email: `${randomUUID()}@example.test`, password: randomUUID() };
  await call('POST', '/api/v1/setup', { ...credentials, display_name: '恢复验收', token: (await config(names[0])).setupToken }, 201);
  await call('POST', '/api/v1/entries', { title: '原记录' }, 201);
  const backup = await backupInstance(names[0]);
  created.push(names[1]); const b = await installInstance({ name: names[1], backup, port: await port() });
  const before = await config(names[1]);
  await advanceOperation(journal(names[1]), await readOperation(journal(names[1])), 'restoring');
  await recoverInstance(names[1]);
  assert.notEqual((await config(names[1])).database, before.database);
  const restored = client(b.origin); await restored('POST', '/api/v1/auth/login', credentials);
  assert.equal((await restored('GET', '/api/v1/entries')).total, 1);
  await restored('POST', '/api/v1/entries', { title: '已启动后的新记录' }, 201);
  const started = await config(names[1]);
  await advanceOperation(journal(names[1]), await readOperation(journal(names[1])), 'starting');
  await recoverInstance(names[1]);
  assert.equal((await config(names[1])).database, started.database);
  assert.equal((await restored('GET', '/api/v1/entries')).total, 2);
  mark('interrupted restore retries on a fresh database; starting-phase recovery retains new writes');

  await updateInstance(names[0], image); call = client(a.origin); await call('POST', '/api/v1/auth/login', credentials);
  await call('POST', '/api/v1/entries', { title: '更新后记录' }, 201);
  await rollbackInstance(names[0], true); call = client(a.origin); await call('POST', '/api/v1/auth/login', credentials);
  await call('POST', '/api/v1/entries', { title: '回滚后新记录' }, 201);
  await advanceOperation(journal(names[0]), await readOperation(journal(names[0])), 'switching');
  const recovered = await recoverInstance(names[0]); assert.ok(recovered.operation.rescue);
  call = client(a.origin); await call('POST', '/api/v1/auth/login', credentials);
  const titles = (await call('GET', '/api/v1/entries')).items.map(e => e.title);
  assert.ok(titles.includes('更新后记录')); assert.ok(!titles.includes('回滚后新记录'));
  created.push(names[2]); const c = await installInstance({ name: names[2], backup: recovered.operation.rescue, port: await port() });
  const rescue = client(c.origin); await rescue('POST', '/api/v1/auth/login', credentials);
  const rescued = (await rescue('GET', '/api/v1/entries')).items.map(e => e.title);
  assert.ok(rescued.includes('回滚后新记录'));
  mark('interrupted rollback returns to prior database while new rollback-side writes remain restorable');
  report.status = 'PASS';
} catch (error) { report.status = 'FAIL'; report.error = error.message; throw error; }
finally {
  report.cleanup = [];
  for (const name of created.reverse()) {
    try { await removeTestInstance(name); report.cleanup.push({ name, removed: true }); }
    catch (error) { report.status = 'FAIL'; report.cleanup.push({ name, removed: false, error: error.message }); }
  }
  await writeFile('.local/output/recovery-smoke.json', JSON.stringify(report, null, 2) + '\n');
  if (report.cleanup.some(c => !c.removed)) throw new Error('Recovery probe cleanup incomplete.');
}
