import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

const violations = [];
function files(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const name = path.join(directory, entry.name);
    return entry.isDirectory() ? files(name) : [name];
  });
}
for (const file of files('server')) {
  if (!file.endsWith('.go')) continue;
  const source = readFileSync(file, 'utf8');
  if (/(?:animemo\.local\/server\/(?:tooling|release|deploy)|exec\.Command.*(?:docker|git|npm))/.test(source)) violations.push(`${file}: application runtime depends on delivery tooling`);
  if (file.includes(`${path.sep}journal${path.sep}`) && /"animemo\.local\/server\/internal\/(accounts|api)"/.test(source)) violations.push(`${file}: journal imports a transport or authentication module`);
}
for (const file of files('web/src')) {
  if (/\.(exe|zip|log|png)$/.test(file)) violations.push(`${file}: generated artifact in source tree`);
}
if (violations.length) { console.error(violations.join('\n')); process.exitCode = 1; }
else console.log('Module dependencies and source layout checked.');
