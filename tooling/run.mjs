import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const local = path.join(root, '.local');
const win = process.platform === 'win32';
for (const name of ['tmp', 'cache/npm', 'cache/go-build', 'cache/go-mod', 'cache/go', 'logs', 'output', 'bin', 'tools', 'data']) {
  mkdirSync(path.join(local, name), { recursive: true });
}
const configPath = path.join(local, 'config.json');
const config = existsSync(configPath) ? JSON.parse(readFileSync(configPath, 'utf8').replace(/^\uFEFF/, '')) : {};
const env = {
  ...process.env,
  NPM_CONFIG_CACHE: path.join(local, 'cache/npm'),
  GOCACHE: path.join(local, 'cache/go-build'),
  GOMODCACHE: path.join(local, 'cache/go-mod'),
  GOPATH: path.join(local, 'cache/go'),
  GOENV: 'off', GOTOOLCHAIN: 'local', GOTELEMETRY: 'off',
  TMP: path.join(local, 'tmp'), TEMP: path.join(local, 'tmp'), TMPDIR: path.join(local, 'tmp'),
  DOCKER_CONFIG: path.join(local, 'cache/docker'),
};
const go = process.env.ANIMEMO_GO || config.go || 'go';
const executable = path.join(local, 'bin', win ? 'animemo.exe' : 'animemo');

function launch(command, args, options = {}) {
  return spawn(command, args, { cwd: root, env, stdio: 'inherit', windowsHide: true, ...options });
}
function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = launch(command, args, options);
    child.on('error', reject);
    child.on('exit', (code, signal) => {
      if (code === 0) resolve();
      else reject(new Error(`${path.basename(command)} failed (${signal || code})`));
    });
  });
}
function node(relative, ...args) { return run(process.execPath, [relative, ...args]); }
function goRun(...args) { return run(go, ['-C', 'server', ...args]); }

function databaseURL() {
  if (process.env.DATABASE_URL) return process.env.DATABASE_URL;
  if (!config.databasePassword) {
    throw new Error('Run npm run db:start, or provide DATABASE_URL. Local PostgreSQL stays inside .local.');
  }
  return `postgres://animemo:${encodeURIComponent(config.databasePassword)}@127.0.0.1:55432/animemo?sslmode=disable`;
}

function pgProgram(name) {
  const directory = process.env.ANIMEMO_PG_BIN || path.join(local, 'tools', 'pgsql', 'bin');
  const program = path.join(directory, name + (win ? '.exe' : ''));
  if (!existsSync(program)) throw new Error(`PostgreSQL not found: ${program}. Set ANIMEMO_PG_BIN; see README.md.`);
  return program;
}

async function dbStart() {
  const data = path.join(local, 'data', 'postgres');
  if (!config.databasePassword) {
    config.databasePassword = randomBytes(24).toString('hex');
    writeFileSync(configPath, JSON.stringify(config, null, 2) + '\n', { mode: 0o600 });
  }
  if (!process.env.ANIMEMO_PG_BIN && !existsSync(path.join(local, 'tools', 'pgsql', 'bin', win ? 'pg_ctl.exe' : 'pg_ctl'))) {
    await compose('up', '-d', '--wait', 'db');
    console.log('PostgreSQL container ready on 127.0.0.1:55432; data: .local/data/compose-postgres');
    return;
  }
  const pgEnv = { ...env, PGPASSWORD: config.databasePassword };
  if (!existsSync(path.join(data, 'PG_VERSION'))) {
    const passwordFile = path.join(local, 'tmp', 'postgres-init-password');
    writeFileSync(passwordFile, config.databasePassword + '\n', { mode: 0o600 });
    try {
      await run(pgProgram('initdb'), ['-D', data, '-U', 'animemo', '--encoding=UTF8', '--locale=C', '--auth=scram-sha-256', `--pwfile=${passwordFile}`], { env: pgEnv });
    } finally { rmSync(passwordFile, { force: true }); }
  }
  const running = await new Promise((resolve, reject) => {
    const check = launch(pgProgram('pg_ctl'), ['-D', data, 'status'], { stdio: 'ignore' });
    check.on('error', reject); check.on('exit', code => resolve(code === 0));
  });
  if (!running) {
    await run(pgProgram('pg_ctl'), ['-D', data, '-l', path.join(local, 'logs', 'postgres.log'), '-o', '-h 127.0.0.1 -p 55432', '-w', 'start'], { env: pgEnv });
  }
  const exists = await new Promise((resolve, reject) => {
    let output = '';
    const child = launch(pgProgram('psql'), ['-h', '127.0.0.1', '-p', '55432', '-U', 'animemo', '-d', 'postgres', '-Atc', "SELECT 1 FROM pg_database WHERE datname='animemo'"], { env: pgEnv, stdio: ['ignore', 'pipe', 'inherit'] });
    child.stdout.on('data', data => output += data);
    child.on('error', reject);
    child.on('exit', code => code === 0 ? resolve(output.trim() === '1') : reject(new Error('Cannot inspect local database')));
  });
  if (!exists) await run(pgProgram('createdb'), ['-h', '127.0.0.1', '-p', '55432', '-U', 'animemo', '-T', 'template0', '-E', 'UTF8', 'animemo'], { env: pgEnv });
  console.log('PostgreSQL ready on 127.0.0.1:55432; data: .local/data/postgres');
}

function compose(...args) {
  if (!config.databasePassword) throw new Error('Run npm run db:start first.');
  return run('docker', ['compose', '-p', 'animemo-next', '-f', 'deploy/compose.yaml', ...args], {
    env: { ...env, POSTGRES_PASSWORD: config.databasePassword, PUBLIC_ORIGIN: process.env.PUBLIC_ORIGIN || 'http://127.0.0.1:18081' },
  });
}

async function dbStop() {
  if (existsSync(path.join(local, 'data', 'postgres', 'PG_VERSION'))) {
    await run(pgProgram('pg_ctl'), ['-D', path.join(local, 'data', 'postgres'), '-m', 'fast', '-w', 'stop']);
  } else { await compose('stop', 'db'); }
}

async function contracts(check = false) {
  const target = check ? '.local/output/api-check.d.ts' : 'web/src/api/schema.d.ts';
  await node('node_modules/openapi-typescript/bin/cli.js', 'contracts/openapi.json', '-o', target);
  if (check && readFileSync(path.join(root, target), 'utf8') !== readFileSync(path.join(root, 'web/src/api/schema.d.ts'), 'utf8')) {
    throw new Error('Generated API types changed. Run npm run contracts and review the result.');
  }
}

async function check() {
  await checkWeb();
  await checkAPI();
}

async function checkWeb() {
  await contracts(true);
  await node('node_modules/typescript/bin/tsc', '--noEmit');
}

async function checkAPI() {
  await goRun('vet', './...');
  await node('tooling/check-layout.mjs');
}

async function build() {
  await node('node_modules/typescript/bin/tsc', '--noEmit');
  await node('node_modules/vite/bin/vite.js', 'build', '--config', 'web/vite.config.ts');
  await goRun('build', '-trimpath', '-o', executable, './cmd/animemo');
}

async function dev() {
  env.DATABASE_URL = databaseURL();
  env.PUBLIC_ORIGIN = process.env.PUBLIC_ORIGIN || 'http://127.0.0.1:5177';
  env.LISTEN_ADDR = process.env.LISTEN_ADDR || '127.0.0.1:18081';
  env.ANIMEMO_API_PROXY = `http://${env.LISTEN_ADDR.replace('0.0.0.0', '127.0.0.1')}`;
  await goRun('build', '-o', executable, './cmd/animemo');
  const children = [launch(executable, ['serve']), launch(process.execPath, ['node_modules/vite/bin/vite.js', '--config', 'web/vite.config.ts'])];
  let closing = false;
  function close() { if (closing) return; closing = true; for (const child of children) child.kill('SIGTERM'); }
  process.on('SIGINT', close); process.on('SIGTERM', close);
  await Promise.all(children.map(child => new Promise((resolve, reject) => {
    child.on('error', error => { close(); reject(error); });
    child.on('exit', code => { const expected = closing; close(); expected || code === 0 ? resolve() : reject(new Error('Development process exited unexpectedly.')); });
  })));
}

const commands = {
  'db-start': dbStart,
  'db-stop': dbStop,
  containers: () => compose('up', '--build', '-d', '--wait'),
  contracts: () => contracts(), check, 'check-web': checkWeb, 'check-api': checkAPI, build, dev,
  test: async () => { await goRun('test', './...'); await node('--test', 'tooling/ci-select.test.mjs', 'web/tests/session.test.mjs'); },
  'test-api': async () => { env.TEST_DATABASE_URL = process.env.TEST_DATABASE_URL || databaseURL(); await goRun('test', '-count=1', '-tags=integration', './...'); },
};

const started = performance.now();
try {
  const command = commands[process.argv[2]];
  if (!command) throw new Error(`Use one of: ${Object.keys(commands).join(', ')}`);
  await command();
  console.log(`\n${process.argv[2]} completed in ${((performance.now() - started) / 1000).toFixed(1)}s`);
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
