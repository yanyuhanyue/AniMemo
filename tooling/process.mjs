import { spawn } from 'node:child_process';
import { createReadStream, createWriteStream } from 'node:fs';
import { pipeline } from 'node:stream/promises';

// Keep credentials in files/environment, never shell interpolation. stdout is
// bounded for diagnostics; streaming archives are not buffered in memory.
export async function runProcess(program, args, { cwd, env = process.env, input, output, capture = false, timeout = 120000 } = {}) {
  const child = spawn(program, args, { cwd, env, windowsHide: true, stdio: [input ? 'pipe' : 'ignore', output || capture ? 'pipe' : 'ignore', 'pipe'] });
  let stdout = '', stderr = '', timedOut = false;
  child.stderr.on('data', b => { stderr = (stderr + b).slice(-8192); });
  if (capture) child.stdout.on('data', b => { stdout += b; if (stdout.length > 2 ** 20) child.kill('SIGKILL'); });
  const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, timeout);
  const completion = new Promise((resolve, reject) => {
    child.on('error', reject);
    child.on('close', code => code === 0 ? resolve() : reject(new Error(`${program} ${timedOut ? 'timed out' : `failed (${code})`}: ${stderr.trim()}`)));
  });
  try {
    await Promise.all([completion, ...(input ? [pipeline(createReadStream(input), child.stdin)] : []), ...(output ? [pipeline(child.stdout, createWriteStream(output, { flags: 'wx', mode: 0o600 }))] : [])]);
    return stdout.trim();
  } catch (error) { child.kill('SIGKILL'); await completion.catch(() => {}); throw error; }
  finally { clearTimeout(timer); }
}
