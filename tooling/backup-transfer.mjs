import { mkdir, readdir, rm, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { runProcess } from './process.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const localAge = path.join(root, '.local/tools/age', process.platform === 'win32' ? 'age.exe' : 'age');
const age = () => process.env.ANIMEMO_AGE || (existsSync(localAge) ? localAge : 'age');
const files = ['manifest.json', 'database.dump', 'secrets.json', 'media.zip'];

// Encrypt each fixed backup member with age's authenticated file format. No
// custom cipher, passwords in argv, tar extraction or user-controlled paths.
export async function exportBackup({ backup, output, recipientsFile }, validate) {
  if (!backup || !output || !recipientsFile) throw new Error('Supply --backup DIR --output NEW_DIR --recipients-file FILE.');
  const { manifest } = await validate(backup);
  await runProcess(age(), ['--version'], { capture: true, timeout: 10000 });
  await mkdir(path.resolve(output), { mode: 0o700 });
  try {
    for (const name of ['manifest.json', ...Object.keys(manifest.files)]) {
      await runProcess(age(), ['--encrypt', '--recipients-file', path.resolve(recipientsFile), path.resolve(backup, name)], { output: path.resolve(output, `${name}.age`), timeout: 600000 });
    }
    return { encrypted_backup: path.resolve(output), format: 'age/v1', files: ['manifest.json', ...Object.keys(manifest.files)].map(n => `${n}.age`) };
  } catch (error) { await rm(path.resolve(output), { recursive: true, force: true }); throw error; }
}

export async function openBackup({ archive, output, identityFile }, validate) {
  if (!archive || !output || !identityFile) throw new Error('Supply --archive DIR --output NEW_PRIVATE_DIR --identity-file FILE.');
  const members = (await readdir(path.resolve(archive))).sort();
  const required = files.slice(0, 3).map(n => `${n}.age`);
  if (required.some(n => !members.includes(n)) || members.some(n => !files.some(f => `${f}.age` === n))) throw new Error('Invalid encrypted backup members.');
  await mkdir(path.resolve(output), { mode: 0o700 });
  try {
    for (const member of members) {
      if (!(await stat(path.resolve(archive, member))).isFile()) throw new Error('Encrypted backup member is not a file.');
      await runProcess(age(), ['--decrypt', '--identity', path.resolve(identityFile), path.resolve(archive, member)], { output: path.resolve(output, member.slice(0, -4)), timeout: 600000 });
    }
    await validate(output);
    return { backup: path.resolve(output), encrypted: false, notice: 'Private plaintext working copy; keep on protected storage and remove after recovery is complete.' };
  } catch (error) { await rm(path.resolve(output), { recursive: true, force: true }); throw error; }
}
