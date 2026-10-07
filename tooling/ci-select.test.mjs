import test from 'node:test';
import assert from 'node:assert/strict';
import { selectChecks } from './ci-select.mjs';

test('frontend-only changes do not select database integration', () => {
  assert.deepEqual(selectChecks(['web/src/journal/Journal.tsx', 'package-lock.json']), { web: true, api: false });
});
test('schema migrations select API integration', () => {
  assert.deepEqual(selectChecks(['server/internal/database/migrations/002.sql']), { web: false, api: true });
});
test('API contract and tooling changes check both consumers', () => {
  for (const path of ['contracts/openapi.json', 'tooling/ci-select.mjs', '.github/workflows/ci.yml', 'deploy/Dockerfile', 'unexpected/new-file']) {
    assert.deepEqual(selectChecks([path]), { web: true, api: true }, path);
  }
});
test('documentation-only changes have no product work', () => {
  assert.deepEqual(selectChecks(['README.md', 'docs/architecture.md']), { web: false, api: false });
});
