import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { createServer } from 'node:net';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { composeArguments, dockerEnvironment } from './docker.mjs';

async function loopbackPort() {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const port = server.address().port;
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  return port;
}

export async function smokeContainers({ root, env, run }) {
  const started = performance.now();
  const port = await loopbackPort();
  const origin = `http://127.0.0.1:${port}`;
  const project = `animemo-next-smoke-${randomBytes(6).toString('hex')}`;
  const environment = dockerEnvironment({
    ...env,
    POSTGRES_PASSWORD: randomBytes(24).toString('hex'),
    PUBLIC_ORIGIN: origin,
    ANIMEMO_SMOKE_PORT: String(port),
  });
  const arguments_ = composeArguments(environment, project, ['deploy/compose.smoke.yaml']);
  const abort = new AbortController();
  const cancel = () => abort.abort(new Error('Container smoke cancelled'));
  const compose = (...args) => run('docker', [...arguments_, ...args], { env: environment, signal: abort.signal, timeout: 600_000 });
  const reportPath = path.join(root, '.local/output/container-smoke.json');
  const report = { status: 'RUNNING', startedAt: new Date().toISOString(), project, checks: [] };
  await writeFile(reportPath, JSON.stringify(report, null, 2) + '\n');
  let cookie = '';
  async function request(method, route, body, status = 200, contentType = 'application/json') {
    const response = await fetch(origin + route, {
      method, redirect: 'error',
      headers: { Origin: origin, Cookie: cookie, ...(body ? { 'Content-Type': contentType } : {}) },
      body: body ? contentType === 'application/json' ? JSON.stringify(body) : body : undefined,
      signal: AbortSignal.any([abort.signal, AbortSignal.timeout(10_000)]),
    });
    assert.equal(response.status, status, `${method} ${route}`);
    const session = response.headers.getSetCookie().find(value => value.startsWith('animemo_session='));
    if (session) cookie = session.split(';')[0];
    if (status === 204) return null;
    if (response.headers.get('content-type')?.startsWith('image/') && route.includes('/cover/')) {
      assert.equal(response.headers.get('cache-control'), 'private, no-store');
      return Buffer.from(await response.arrayBuffer());
    }
    return response.headers.get('content-type')?.includes('application/json') ? response.json() : response.text();
  }
  process.once('SIGINT', cancel);
  process.once('SIGTERM', cancel);
  let failure;
  try {
    await compose('up', '--build', '-d', '--wait', '--wait-timeout', '90');
    assert.equal((await request('GET', '/api/ready')).status, 'ready');
    const html = await request('GET', '/');
    assert.match(html, /<div id="root">/);
    const assets = [...html.matchAll(/(?:src|href)="(\/[^" ]+)"/g)].map(match => match[1]);
    assert.ok(assets.some(asset => asset.endsWith('.js')), 'Built frontend JavaScript is referenced');
    for (const asset of assets) assert.ok((await request('GET', asset)).length > 0, `Readable image resource: ${asset}`);
    report.checks.push('readiness and built frontend assets');
    await request('GET', '/api/v1/auth/me', null, 401);
    const credentials = { email: `smoke-${randomUUID()}@example.test`, password: randomBytes(24).toString('hex') };
    const user = await request('POST', '/api/v1/auth/register', { ...credentials, display_name: '容器验收' }, 201);
    await request('POST', '/api/v1/auth/logout', null, 204);
    await request('GET', '/api/v1/auth/me', null, 401);
    assert.equal((await request('POST', '/api/v1/auth/login', credentials)).id, user.id);
    report.checks.push('registration, logout and login');
    const entry = await request('POST', '/api/v1/entries', { title: '容器里的番剧记忆', total_episodes: 12, tags: ['验收'] }, 201);
    const entryPath = `/api/v1/entries/${entry.id}`;
    const cover = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMISNnyHwAE2AJoEeqx4gAAAABJRU5ErkJggg==', 'base64');
    const withCover = await request('PUT', entryPath + '/cover?version=1', cover, 200, 'image/png');
    assert.equal(withCover.version, 2);
    const coverPath = `${entryPath}/cover/${withCover.cover_revision}`;
    assert.deepEqual(await request('GET', coverPath), cover);
    const watch = { watched_on: '2026-10-07', episode_from: 1, episode_to: 3, note: '重建后仍应保留', request_id: randomUUID() };
    const saved = await request('POST', entryPath + '/history', watch, 201);
    assert.equal(saved.entry.watched_episodes, 3);
    report.checks.push('create entry, private cover and watch record');
    const importJob = await request('POST', '/api/v1/imports?format=csv', Buffer.from('title,total_episodes\n容器重建后导入,12\n'), 202, 'application/octet-stream');

    // Recreate both containers while preserving this test's dedicated database volume.
    await compose('up', '-d', '--force-recreate', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '90');
    assert.equal((await request('GET', '/api/v1/auth/me')).id, user.id);
    assert.deepEqual(await request('GET', entryPath), saved.entry);
    assert.deepEqual(await request('GET', coverPath), cover);
    const { items: history } = await request('GET', entryPath + '/history');
    assert.equal(history.length, 1);
    assert.equal(history[0].id, saved.record.id);
    assert.equal((await request('POST', entryPath + '/history', watch, 201)).record.id, saved.record.id);
    const exported = await request('GET', '/api/v1/export');
    assert.equal(exported.entries.length, 1);
    assert.equal(exported.entries[0].cover_revision, withCover.cover_revision);
    assert.equal(exported.history.length, 1);
    assert.equal(exported.history[0].id, saved.record.id);
    await request('POST', '/api/v1/auth/logout', null, 204);
    await request('GET', coverPath, null, 401);
    assert.equal((await request('POST', '/api/v1/auth/login', credentials)).id, user.id);
    assert.deepEqual(await request('GET', coverPath), cover);
    const withoutCover = await request('DELETE', `${entryPath}/cover?version=${saved.entry.version}`);
    assert.equal(withoutCover.cover_revision, null);
    await request('GET', coverPath, null, 404);
    report.checks.push('session, entry, private image bytes, history, idempotency, export and login after recreation');
    report.checks.push('cover inaccessible after logout and removal');
    async function waitForImport(state) {
      for (let attempt = 0; attempt < 50; attempt++) {
        const job = await request('GET', `/api/v1/imports/${importJob.id}`);
        assert.notEqual(job.state, 'failed', job.error);
        if (job.state === state) return job;
        await new Promise(resolve => setTimeout(resolve, 200));
      }
      throw new Error(`Import did not reach ${state}`);
    }
    assert.equal((await waitForImport('ready')).preview.ready, 1);
    // Cover removal above changed the journal after preview. Cancel the stale
    // task and verify a fresh durable task through the running worker.
    await request('POST', `/api/v1/imports/${importJob.id}`, { action: 'cancel' });
    const fresh = await request('POST', '/api/v1/imports?format=csv', Buffer.from('title,total_episodes\n容器重建后导入,12\n'), 202, 'application/octet-stream');
    importJob.id = fresh.id;
    await waitForImport('ready');
    await request('POST', `/api/v1/imports/${importJob.id}`, { action: 'apply' });
    assert.equal((await waitForImport('done')).created, 1);
    assert.equal((await request('GET', '/api/v1/entries')).total, 2);
    report.checks.push('import preview survives recreation; worker applies confirmed CSV atomically');
  } catch (error) {
    failure = error;
  } finally {
    try {
      // Never address the developer's Compose project or its database volume.
      await run('docker', [...arguments_, 'down', '--volumes', '--remove-orphans', '--rmi', 'local'], { env: environment, timeout: 120_000 });
      report.checks.push('isolated containers, network and volume removed');
    } catch (error) {
      failure = new Error(`${failure ? `${failure.message}; ` : ''}cleanup failed for ${project}: ${error.message}`);
    }
    process.removeListener('SIGINT', cancel);
    process.removeListener('SIGTERM', cancel);
    report.status = failure ? 'FAIL' : 'PASS';
    report.elapsedSeconds = Number(((performance.now() - started) / 1000).toFixed(1));
    await writeFile(reportPath, JSON.stringify(report, null, 2) + '\n');
  }
  if (failure) throw failure;
  console.log(`Container business and persistence checks passed. Report: ${reportPath}`);
}
