import { open, readFile, rename } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';

// Private local state, not a release authority. Configurations contain secrets:
// never print this journal or include it in source / diagnostic bundles.
export async function writeOperation(file, value) {
  const temporary = `${file}.${randomUUID()}.tmp`;
  const handle = await open(temporary, 'wx', 0o600);
  try { await handle.writeFile(JSON.stringify(value) + '\n'); await handle.sync(); }
  finally { await handle.close(); }
  await rename(temporary, file);
}
export async function readOperation(file) {
  try {
    const value = JSON.parse(await readFile(file, 'utf8'));
    if (value.schema !== 'animemo.operation/v1' || !['install','restore','backup','configure','update','rollback'].includes(value.kind) || !value.id || !value.prior || !value.phase) throw new Error('Invalid operation journal.');
    return value;
  } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}
export function operationReceipt(operation) {
  return { id: operation.id, kind: operation.kind, phase: operation.phase, started_at: operation.started_at, updated_at: operation.updated_at, backup: operation.backup ?? null, rescue: operation.rescue ?? null };
}
export async function advanceOperation(file, operation, phase, updates = {}) {
  const next = { ...operation, ...updates, phase, updated_at: new Date().toISOString() };
  await writeOperation(file, next);
  return next;
}
