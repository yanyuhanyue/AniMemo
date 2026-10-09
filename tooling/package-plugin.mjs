import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { buildPluginPackage } from './plugin-package.mjs';
import path from 'node:path';

const [manifestFile, moduleFile, output] = process.argv.slice(2);
if (!manifestFile || !moduleFile || !output) throw new Error('Use node tooling/package-plugin.mjs MANIFEST WASM_OR_DASH OUTPUT.animemo-plugin');
const module = moduleFile === '-' ? Buffer.alloc(0) : await readFile(moduleFile);
if (module.length > 8 * 1024 * 1024) throw new Error('Module exceeds 8 MiB.');
const pack = await buildPluginPackage(manifestFile, module);
const { manifest } = pack;
await mkdir(path.dirname(output), { recursive: true });
await writeFile(output, JSON.stringify(pack) + '\n');
console.log(`Packaged ${manifest.slug}@${manifest.version}: ${output}`);
