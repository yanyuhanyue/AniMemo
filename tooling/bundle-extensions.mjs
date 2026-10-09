import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { buildPluginPackage } from './plugin-package.mjs';
const [binary, wasm, manifestFile, directory, ...themeFiles] = process.argv.slice(2);
if (!directory) throw new Error('Use bundle-extensions CORE WASM MANIFEST OUTPUT_DIRECTORY [THEME_MANIFEST ...]');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
await mkdir(directory, { recursive: true });
const packages = [];
for (const [file, module] of [[manifestFile, await readFile(wasm)], ...themeFiles.map(file => [file, Buffer.alloc(0)])]) {
  const pack = await buildPluginPackage(file, module);
  const name = pack.manifest.slug + '.animemo-plugin';
  const bytes = JSON.stringify(pack) + '\n';
  await writeFile(path.join(directory, name), bytes);
  packages.push({ file: name, sha256: hash(bytes) });
}
await writeFile(path.join(directory, 'bundled-extensions.json'), JSON.stringify({ schema: 'animemo.bundled/v1', core_sha256: hash(await readFile(binary)), packages }, null, 2) + '\n');
