import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir, mkdtemp, rm, cp, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { createServer } from 'node:net';
import path from 'node:path';
import { configureInstance, configureExternal, doctorInstance, recoverInstance, startInstance, restorePlan, validateBackup } from './instance.mjs';
import { readOperation, advanceOperation } from './operation-journal.mjs';
import { exportBackup, openBackup } from './backup-transfer.mjs';
import { runProcess } from './process.mjs';

export async function checkConfiguration(name, mark) {
  const file = `.local/instances/${name}/config.json`, operationFile = `.local/instances/${name}/operation.json`;
  const before = await readFile(file, 'utf8'), c = JSON.parse(before);
  assert.equal((await doctorInstance({ name })).status, 'PASS');
  const s = createServer(); await new Promise(resolve => s.listen(0, '127.0.0.1', resolve));
  const port = s.address().port;
  try {
    const preview = await configureInstance(name, { port });
    assert.equal(preview.applied, false); assert.equal(await readFile(file, 'utf8'), before);
    assert.ok(!JSON.stringify(preview).includes(c.secretKey));
    await assert.rejects(() => configureInstance(name, { port }, true), /unavailable/);
    assert.equal(await readFile(file, 'utf8'), before);
  } finally { await new Promise(resolve => s.close(resolve)); }
  await configureInstance(name, { port }, true);
  assert.equal((await fetch(`http://127.0.0.1:${port}/api/ready`)).status, 200);
  // The journal records the old config before switching. Recover must restore
  // its original listener even when the previous CLI process no longer exists.
  const op = await readOperation(operationFile);
  await advanceOperation(operationFile, op, 'switching');
  await assert.rejects(() => startInstance(name), /recover first/);
  await recoverInstance(name);
  assert.equal((await fetch(c.origin + '/api/ready')).status, 200);
  assert.equal(JSON.parse(await readFile(file, 'utf8')).port, c.port);
  mark('doctor, read-only configuration preview, occupied-port rejection, live listener change and interrupted configuration recovery');

  await mkdir('.local/tmp', { recursive: true });
  const dir = await mkdtemp('.local/tmp/config-failure-');
  try {
    // Syntactically valid settings that the application rejects on startup.
    const env = path.join(dir, 'invalid.env');
    await writeFile(env, 'R2_ENDPOINT=https://invalid.example.test\n', { mode: 0o600 });
    await assert.rejects(() => configureExternal(name, env), /previous configuration restored/);
    assert.equal((await doctorInstance({ name })).status, 'PASS');
    assert.equal(JSON.parse(await readFile(file, 'utf8')).integrations?.R2_ENDPOINT ?? '', '');
  } finally { await rm(dir, { recursive: true, force: true }); }
  mark('failed runtime configuration restores prior settings and healthy app/worker');
}

export async function checkBackupTransfer(backup, target, source, mark) {
  const plan = await restorePlan({ backup, name: target });
  assert.equal(plan.compatible, true); assert.equal(plan.writes_resources, false);
  assert.ok(plan.migrations.length > 0); assert.ok(Array.isArray(plan.plugins));
  assert.ok(!JSON.stringify(plan).includes(source.secretKey));
  const occupied = await restorePlan({ backup, name: source.name }); assert.equal(occupied.compatible, false);
  const operationFile = `.local/instances/${source.name}/operation.json`;
  await advanceOperation(operationFile, await readOperation(operationFile), 'backed_up');
  await recoverInstance(source.name);
  mark('v3 migration, source, media and extension inventory; read-only restore plan and interrupted backup recovery');

  const keygen = process.env.ANIMEMO_AGE_KEYGEN || path.resolve('.local/tools/age', process.platform === 'win32' ? 'age-keygen.exe' : 'age-keygen');
  if (!existsSync(keygen)) throw new Error('Install age and age-keygen into .local/tools/age before lifecycle validation.');
  const dir = await mkdtemp('.local/tmp/encrypted-backup-');
  try {
    const key = path.join(dir, 'identity.txt'), wrong = path.join(dir, 'wrong.txt'), recipients = path.join(dir, 'recipients.txt');
    await runProcess(keygen, ['-o', key]); await runProcess(keygen, ['-o', wrong]);
    await writeFile(recipients, await runProcess(keygen, ['-y', key], { capture: true }));
    const encrypted = path.join(dir, 'encrypted');
    await exportBackup({ backup, output: encrypted, recipientsFile: recipients }, validateBackup);
    const wrongOutput = path.join(dir, 'wrong-output');
    await assert.rejects(() => openBackup({ archive: encrypted, output: wrongOutput, identityFile: wrong }, validateBackup));
    assert.equal(existsSync(wrongOutput), false);
    const tampered = path.join(dir, 'tampered'); await cp(encrypted, tampered, { recursive: true });
    const cipher = path.join(tampered, 'database.dump.age'), data = await readFile(cipher); data[data.length - 1] ^= 1; await writeFile(cipher, data);
    await assert.rejects(() => openBackup({ archive: tampered, output: wrongOutput, identityFile: key }, validateBackup));
    assert.equal(existsSync(wrongOutput), false);
    const plain = path.resolve('.local/output/backups', `decrypted-${path.basename(dir)}`);
    const opened = await openBackup({ archive: encrypted, output: plain, identityFile: key }, validateBackup);
    assert.deepEqual(await readFile(path.join(plain, 'secrets.json')), await readFile(path.join(backup, 'secrets.json')));
    if (process.platform !== 'win32') assert.equal((await stat(plain)).mode & 0o777, 0o700);
    mark('real age encrypted transfer round-trip; wrong identity and modified ciphertext rejected with partial plaintext removed');
    return opened.backup;
  } finally { await rm(dir, { recursive: true, force: true }); }
}
