import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';

const [manifestFile, moduleFile, output] = process.argv.slice(2);
if (!manifestFile || !moduleFile || !output) throw new Error('Use node tooling/package-plugin.mjs MANIFEST WASM OUTPUT.animemo-plugin');
const manifest = JSON.parse(await readFile(manifestFile, 'utf8'));
const module = await readFile(moduleFile);
if (module.length > 8 * 1024 * 1024) throw new Error('Module exceeds 8 MiB.');
manifest.module_sha256 = createHash('sha256').update(module).digest('hex');
await mkdir(path.dirname(output), { recursive: true });
await writeFile(output, JSON.stringify({ manifest, module: module.toString('base64') }) + '\n');
console.log(`Packaged ${manifest.slug}@${manifest.version}: ${output}`);
