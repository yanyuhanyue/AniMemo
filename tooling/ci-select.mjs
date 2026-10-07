import { readFileSync, appendFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// Keep selection explicit. Unknown paths broaden verification instead of skipping it.
export function selectChecks(paths) {
  let web = false, api = false;
  for (const path of paths) {
    if (/^(docs\/|README\.md$|LICENSE$|NOTICE$)/.test(path)) continue;
    if (/^(web\/|package(-lock)?\.json$|tsconfig\.json$|\.npmrc$)/.test(path)) web = true;
    else if (path.startsWith('server/internal/apicontract/')) { web = true; api = true; }
    else if (path.startsWith('server/')) api = true;
    else { web = true; api = true; }
  }
  return { web, api };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const paths = readFileSync(process.argv[2], 'utf8').split('\0').filter(Boolean);
  const checks = selectChecks(paths);
  const output = Object.entries(checks).map(([key, value]) => `${key}=${value}`).join('\n') + '\n';
  if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, output);
  process.stdout.write(output);
}
