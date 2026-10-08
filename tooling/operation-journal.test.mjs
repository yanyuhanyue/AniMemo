import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, stat, readFile } from 'node:fs/promises';
import path from 'node:path';
import { readOperation, advanceOperation, operationReceipt } from './operation-journal.mjs';

test('operation survives process boundary; public receipt excludes credentials', async () => {
  await mkdir('.local/tmp', { recursive: true });
  const directory = await mkdtemp('.local/tmp/operation-');
  const file = path.join(directory, 'operation.json');
  try {
    assert.equal(await readOperation(file), null);
    let operation = await advanceOperation(file, { schema: 'animemo.operation/v1', id: 'test', kind: 'update', started_at: new Date().toISOString(), prior: { password: 'private-credential', database: 'old' } }, 'prepared');
    operation = await advanceOperation(file, operation, 'switching', { next: { password: 'private-credential', database: 'candidate' }, backup: 'snapshot' });
    const restored = await readOperation(file);
    assert.equal(restored.next.database, 'candidate');
    assert.equal(restored.prior.database, 'old');
    assert.ok(!JSON.stringify(operationReceipt(restored)).includes('private-credential'));
    if (process.platform !== 'win32') assert.equal((await stat(file)).mode & 0o777, 0o600);
    assert.equal(JSON.parse(await readFile(file, 'utf8')).phase, 'switching');
  } finally { await rm(directory, { recursive: true, force: true }); }
});
