import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { validateRelease, inspectRelease, fileHash, verifyRelease } from './release.mjs';

test('release policy rejects development builds, arbitrary sources and unsafe archive names', () => {
  const m = { schema: 'animemo.release/v1', repository: 'yanyuhanyue/AniMemo', version: '1.1.0-rc.1', tag: 'v1.1.0-rc.1', revision: 'a'.repeat(40), development: false, image: `sha256:${'b'.repeat(64)}`, platform: 'linux/amd64', archive: { name: 'animemo-image.tar', bytes: 1, sha256: 'c'.repeat(64) } };
  assert.equal(validateRelease(m), m);
  for (const changes of [{ repository: 'untrusted/repo' }, { revision: 'a'.repeat(40) + '-dirty' }, { development: true }, { tag: 'v9.0.0' }, { platform: 'windows/amd64' }, { archive: { ...m.archive, name: '../outside.tar' } }]) assert.throws(() => validateRelease({ ...m, ...changes }));
});
test('tampered artifacts fail before invoking any identity or Docker tools', async () => {
  await mkdir('.local/tmp', { recursive: true });
  const dir = await mkdtemp('.local/tmp/release-test-');
  try {
    const archive = path.join(dir, 'animemo-image.tar'); await writeFile(archive, 'test archive');
    const m = { schema: 'animemo.release/v1', repository: 'yanyuhanyue/AniMemo', version: '1.1.0-rc.1', tag: 'v1.1.0-rc.1', revision: 'a'.repeat(40), development: false, image: `sha256:${'b'.repeat(64)}`, platform: 'linux/amd64', archive: { name: 'animemo-image.tar', bytes: 12, sha256: await fileHash(archive) } };
    await writeFile(path.join(dir, 'release.json'), JSON.stringify(m));
    assert.equal((await inspectRelease(dir)).version, m.version);
    await writeFile(archive, 'evil archive');
    await assert.rejects(() => verifyRelease(dir), /checksum or size/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
