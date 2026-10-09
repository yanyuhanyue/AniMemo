import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
const hash = value => createHash('sha256').update(value).digest('hex');

// Shared source convention for local packaging and the bundled inventory.
export async function buildPluginPackage(manifestFile, module) {
  const manifest = JSON.parse(await readFile(manifestFile, 'utf8'));
  manifest.module_sha256 = hash(module);
  const result = { manifest, module: module.toString('base64') };
  const presentation = manifest.notes_theme?.presentation;
  if (presentation) {
    const directory = path.dirname(manifestFile);
    for (const [field, file] of Object.entries({ list: 'list.html', card: 'card.html', reader: 'reader.html', css: 'theme.css' })) {
      if (!presentation[field]) presentation[field] = await readFile(path.join(directory, file), 'utf8');
    }
    result.assets = {};
    for (const asset of presentation.assets || []) {
      if (!/^[a-z][a-z0-9.-]{0,63}$/.test(asset.name)) throw new Error('Theme asset names must be plain filenames.');
      const bytes = await readFile(path.join(directory, 'assets', asset.name));
      asset.sha256 = hash(bytes);
      result.assets[asset.name] = bytes.toString('base64');
    }
  }
  return result;
}
