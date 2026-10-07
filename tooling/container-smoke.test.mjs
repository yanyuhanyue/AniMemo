import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm } from 'node:fs/promises';
import path from 'node:path';
import { smokeContainers } from './container-smoke.mjs';

async function fixture(t) {
  const temporary = path.resolve('.local/tmp');
  await mkdir(temporary, { recursive: true });
  const root = await mkdtemp(path.join(temporary, 'smoke-runner-test-'));
  await mkdir(path.join(root, '.local/output'), { recursive: true });
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

test('a failed build cleans only its isolated project and records failure', async t => {
  const root = await fixture(t);
  const calls = [];
  await assert.rejects(smokeContainers({
    root, env: { DOCKER_CONFIG: '/managed/docker', POSTGRES_PASSWORD: 'existing-development-password' },
    run: async (command, args, options) => {
      calls.push({ command, args, options });
      if (args.includes('up')) throw new Error('synthetic build failure');
    },
  }), /synthetic build failure/);
  assert.equal(calls.length, 2);
  const project = calls[0].args[2];
  assert.match(project, /^animemo-next-smoke-[a-f0-9]{12}$/);
  assert.equal(calls[1].args[2], project);
  assert.ok(calls[1].args.includes('down'));
  assert.ok(calls[1].args.includes('--volumes'));
  assert.equal(calls[0].options.env.DOCKER_CONFIG, '/managed/docker');
  assert.notEqual(calls[0].options.env.POSTGRES_PASSWORD, 'existing-development-password');
  const report = JSON.parse(await readFile(path.join(root, '.local/output/container-smoke.json')));
  assert.equal(report.status, 'FAIL');
  assert.ok(report.checks.includes('isolated containers, network and volume removed'));
});

test('cancellation still runs cleanup without the aborted signal', async t => {
  const root = await fixture(t);
  let cleaned = false;
  await assert.rejects(smokeContainers({
    root, env: {},
    run: async (command, args, options) => {
      if (args.includes('up')) {
        process.emit('SIGTERM');
        assert.equal(options.signal.aborted, true);
        throw options.signal.reason;
      }
      assert.equal(options.signal, undefined);
      cleaned = true;
    },
  }), /cancelled/);
  assert.equal(cleaned, true);
});

test('cleanup failure retains the first error and names the project for recovery', async t => {
  const root = await fixture(t);
  await assert.rejects(smokeContainers({
    root, env: {},
    run: async (command, args) => { throw new Error(args.includes('up') ? 'build failed' : 'daemon unavailable'); },
  }), /build failed; cleanup failed for animemo-next-smoke-[a-f0-9]+: daemon unavailable/);
  const report = JSON.parse(await readFile(path.join(root, '.local/output/container-smoke.json')));
  assert.equal(report.status, 'FAIL');
  assert.deepEqual(report.checks, []);
});
