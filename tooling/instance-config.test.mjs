import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:net';
import { configurationPlan, checkPort, validOrigin, validBind } from './instance-config.mjs';

test('configuration preview preserves HTTPS origin, follows local port and never exposes secrets', () => {
  const c = { port: 18082, origin: 'http://127.0.0.1:18082', password: 'do-not-print', secretKey: 'do-not-print' };
  const p = configurationPlan(c, { port: 19000 });
  assert.equal(p.next.origin, 'http://127.0.0.1:19000');
  assert.equal(c.port, 18082);
  assert.ok(!JSON.stringify(p.preview).includes('do-not-print'));
  const external = configurationPlan({ ...c, origin: 'https://anime.example.test' }, { port: 19001, bind: '0.0.0.0' });
  assert.equal(external.next.origin, 'https://anime.example.test');
  assert.equal(external.preview.external_listener, true);
  for (const origin of ['http://anime.example.test', 'https://anime.example.test/path', 'https://name:secret@example.test']) assert.throws(() => validOrigin(origin));
  assert.throws(() => validBind('example.test'));
});
test('occupied ports are rejected before an instance is modified', async () => {
  const s = createServer();
  await new Promise(resolve => s.listen(0, '127.0.0.1', resolve));
  try { await assert.rejects(() => checkPort('127.0.0.1', s.address().port), /unavailable/); }
  finally { await new Promise(resolve => s.close(resolve)); }
});
