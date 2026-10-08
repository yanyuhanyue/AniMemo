import { readFile,writeFile,mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';

const [binary,wasm,manifestFile,directory]=process.argv.slice(2);
if(!directory)throw new Error('Use bundle-extensions CORE WASM MANIFEST OUTPUT_DIRECTORY');
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
const module=await readFile(wasm),manifest=JSON.parse(await readFile(manifestFile,'utf8'));
manifest.module_sha256=hash(module);
const file=manifest.slug+'.animemo-plugin';
const bytes=JSON.stringify({manifest,module:module.toString('base64')})+'\n';
await mkdir(directory,{recursive:true});await writeFile(path.join(directory,file),bytes);
await writeFile(path.join(directory,'bundled-extensions.json'),JSON.stringify({schema:'animemo.bundled/v1',core_sha256:hash(await readFile(binary)),packages:[{file,sha256:hash(bytes)}]},null,2)+'\n');
