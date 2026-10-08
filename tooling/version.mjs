import { readFile } from 'node:fs/promises';
import { runProcess } from './process.mjs';

export async function sourceVersion() {
  const { version } = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  if (!/^\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?$/.test(version)) throw new Error('Invalid product version.');
  const contract = JSON.parse(await readFile(new URL('../contracts/openapi.json', import.meta.url), 'utf8'));
  if (contract.info?.version !== version) throw new Error('OpenAPI and product versions differ. Update contracts/openapi.json and run npm run contracts before building.');
  let revision = 'unknown', dirty = true;
  try {
    revision = await runProcess('git', ['rev-parse', 'HEAD'], { capture: true });
    dirty = !!await runProcess('git', ['status', '--porcelain', '--untracked-files=normal'], { capture: true });
  } catch { /* Source archives remain explicitly unverified development builds. */ }
  return { version, revision: revision + (dirty ? '-dirty' : ''), dirty, source: 'https://github.com/yanyuhanyue/AniMemo' };
}
export const versionFlags = info => `-X animemo.local/server/internal/buildinfo.Version=${info.version} -X animemo.local/server/internal/buildinfo.Revision=${info.revision}`;
