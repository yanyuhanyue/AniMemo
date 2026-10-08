import { randomBytes } from 'node:crypto';
import { createServer } from 'node:net';
import { mkdir, writeFile } from 'node:fs/promises';
import { inspectRelease } from './release.mjs';
import { resolveArchivedImage } from './image-identity.mjs';
import path from 'node:path';
import { installInstance, doctorInstance, removeTestInstance } from './instance.mjs';

// Call only after release load has verified publisher identity and loaded bytes.
const manifest = await inspectRelease(process.argv[2]);
const {actual}=await resolveArchivedImage(path.resolve(process.argv[2],'animemo-image.tar'),manifest.image);
const name = `probe-${randomBytes(6).toString('hex')}`;
const server = createServer();
await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
const port = server.address().port;
await new Promise(resolve => server.close(resolve));
const report = { status: 'RUNNING', image: manifest.image, version: manifest.version, revision: manifest.revision };
try {
  await installInstance({ name, image: actual.Id, port });
  const result = await doctorInstance({ name });
  if (result.status !== 'PASS') throw new Error('Downloaded release did not become healthy.');
  report.status = 'PASS';
} catch (error) { report.status = 'FAIL'; report.error = error.message; throw error; }
finally {
  try { await removeTestInstance(name); report.cleaned_up = true; }
  catch { report.cleaned_up = false; report.status = 'FAIL'; }
  await mkdir('.local/output', { recursive: true });
  await writeFile('.local/output/release-smoke.json', JSON.stringify(report, null, 2) + '\n');
  if (!report.cleaned_up) throw new Error('Release probe cleanup failed; inspect release-smoke.json.');
}
