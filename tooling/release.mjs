import { createReadStream, existsSync } from 'node:fs';
import { mkdir, readFile, writeFile, stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { runProcess } from './process.mjs';
import { sourceVersion } from './version.mjs';
import { archiveImageIdentity,resolveArchivedImage } from './image-identity.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repository = 'yanyuhanyue/AniMemo';
const workflow = `${repository}/.github/workflows/release.yml`;
const docker = (args, options) => runProcess('docker', args, { cwd: root, ...options });
export async function fileHash(file) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}
export function validateRelease(manifest, { development = false } = {}) {
  if (manifest.schema !== 'animemo.release/v1' || manifest.repository !== repository || !/^\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?$/.test(manifest.version) || manifest.tag !== `v${manifest.version}` || !/^sha256:[a-f0-9]{64}$/.test(manifest.image) || manifest.platform !== 'linux/amd64' || !/^[a-f0-9]{64}$/.test(manifest.archive?.sha256) || !Number.isSafeInteger(manifest.archive?.bytes) || manifest.archive.bytes <= 0 || manifest.archive.name !== 'animemo-image.tar') throw new Error('Invalid or unsupported release manifest.');
  if (!/^[a-f0-9]{40}(?:-dirty)?$/.test(manifest.revision)) throw new Error('Release source revision is invalid.');
  if(manifest.image_config!==undefined&&!/^sha256:[a-f0-9]{64}$/.test(manifest.image_config))throw new Error('Release image configuration digest is invalid.');
  if (!development && (manifest.development !== false || !/^[a-f0-9]{40}$/.test(manifest.revision))) throw new Error('Development artifacts cannot be installed as verified releases.');
  return manifest;
}
export async function inspectRelease(directory, options = {}) {
  const manifest = validateRelease(JSON.parse(await readFile(path.resolve(directory, 'release.json'), 'utf8')), options);
  const file = path.resolve(directory, manifest.archive.name);
  if ((await stat(file)).size !== manifest.archive.bytes || await fileHash(file) !== manifest.archive.sha256) throw new Error('Release archive checksum or size mismatch.');
  return manifest;
}
export async function buildRelease({ output, image = 'animemo-release:candidate', development = false } = {}) {
  if (!output || image.startsWith('-')) throw new Error('Supply a new --output directory and a valid --image tag.');
  const info = await sourceVersion();
  if (!development && info.dirty) throw new Error('A release must be built from a clean committed source tree. Use --development for an explicitly untrusted local candidate.');
  await mkdir(path.resolve(output), { mode: 0o755 });
  const args = ['build', '--platform', 'linux/amd64', '-f', 'deploy/Dockerfile', '-t', image, '--build-arg', `ANIMEMO_VERSION=${info.version}`, '--build-arg', `ANIMEMO_REVISION=${info.revision}`];
  const ca = process.env.ANIMEMO_BUILD_CA || process.env.CODEX_PROXY_CERT;
  if (ca) args.push('--secret', `id=build_ca,src=${path.resolve(ca)}`);
  console.log('Building the candidate once; the saved image is used for checks and distribution.');
  await docker([...args, '.'], { timeout: 1200000 });
  const id = await docker(['image', 'inspect', image, '--format', '{{.Id}}'], { capture: true });
  const runtime = JSON.parse(await docker(['run', '--rm', '--network', 'none', id, 'version'], { capture: true }));
  if (runtime.version !== info.version || runtime.revision !== info.revision) throw new Error('Built image version does not match the source.');
  const archive = path.resolve(output, 'animemo-image.tar');
  await docker(['image', 'save', id], { output: archive, timeout: 600000 });
  const identity=await archiveImageIdentity(archive);
  const manifest = { schema: 'animemo.release/v1', repository, version: info.version, tag: `v${info.version}`, revision: info.revision, development: development || info.dirty, platform: 'linux/amd64', image: identity.config_digest, image_config:identity.config_digest, postgres_major: 17, migrations: runtime.migrations, archive: { name: 'animemo-image.tar', bytes: (await stat(archive)).size, sha256: await fileHash(archive) } };
  validateRelease(manifest, { development });
  await writeFile(path.resolve(output, 'release.json'), JSON.stringify(manifest, null, 2) + '\n', { flag: 'wx' });
  return manifest;
}
export async function verifyRelease(directory, { trustedRoot } = {}) {
  // Identity policy is code-owned, never supplied by the downloaded manifest.
  const manifest = await inspectRelease(directory);
  const localGh = path.join(root, '.local/tools/gh', process.platform === 'win32' ? 'gh.exe' : 'gh');
  const gh = process.env.ANIMEMO_GH || (existsSync(localGh) ? localGh : 'gh');
  const bundle = path.resolve(directory, 'attestations.jsonl');
  const policy = ['--hostname', 'github.com', '--repo', repository, '--signer-workflow', workflow, '--source-ref', `refs/tags/${manifest.tag}`, '--source-digest', manifest.revision, '--deny-self-hosted-runners', '--format', 'json'];
  if (existsSync(bundle)) policy.push('--bundle', bundle);
  if (trustedRoot) policy.push('--custom-trusted-root', path.resolve(trustedRoot));
  for (const file of ['release.json', 'animemo-image.tar']) {
    await runProcess(gh, ['attestation', 'verify', path.resolve(directory, file), ...policy], { cwd: root, capture: true, timeout: 120000 });
  }
  return manifest;
}
export async function loadVerifiedRelease(directory, options = {}) {
  const manifest = await verifyRelease(directory, options);
  return loadImage(directory,manifest);
}
export async function loadDevelopmentRelease(directory){
  const manifest=await inspectRelease(directory,{development:true});
  if(manifest.development!==true)throw new Error('Only explicitly marked development candidates may use --development.');
  return loadImage(directory,manifest);
}
async function loadImage(directory,manifest){
  const archive=path.resolve(directory,'animemo-image.tar');
  const identity=await archiveImageIdentity(archive,manifest.image);
  if(manifest.image_config&&manifest.image_config!==identity.config_digest)throw new Error('Release image configuration differs from the archive.');
  await docker(['image', 'load'], { input: path.resolve(directory, 'animemo-image.tar'), timeout: 600000 });
  const {actual}=await resolveArchivedImage(archive,manifest.image);
  const labels = actual.Config.Labels || {};
  if (`${actual.Os}/${actual.Architecture}` !== manifest.platform || labels['org.opencontainers.image.version'] !== manifest.version || labels['org.opencontainers.image.revision'] !== manifest.revision || labels['org.opencontainers.image.source'] !== `https://github.com/${repository}`) throw new Error('Loaded image metadata differs from the verified release.');
  return {...manifest,local_image:actual.Id};
}
async function main() {
  const [action, ...args] = process.argv.slice(2), options = {};
  for (let n = 0; n < args.length; n++) {
    if (args[n] === '--development') options.development = true;
    else if (['--output', '--image', '--directory', '--trusted-root'].includes(args[n]) && args[n + 1] && !args[n + 1].startsWith('--')) options[args[n++].slice(2)] = args[n];
    else throw new Error('Unknown or incomplete release option.');
  }
  let result;
  if (action === 'build') result = await buildRelease(options);
  else if (action === 'inspect' && options.directory) result = { trust_verified: false, manifest: await inspectRelease(options.directory, options) };
  else if(action==='load'&&options.directory&&options.development)result={trust_verified:false,manifest:await loadDevelopmentRelease(options.directory)};
  else if (['verify', 'load'].includes(action) && options.directory) result = { trust_verified: true, manifest: await (action === 'verify' ? verifyRelease : loadVerifiedRelease)(options.directory, { trustedRoot: options['trusted-root'] }) };
  else throw new Error('Use release build --output NEW_DIR, inspect/verify/load --directory DIR.');
  console.log(JSON.stringify(result, null, 2));
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch(error => { console.error(error.message); process.exitCode = 1; });
