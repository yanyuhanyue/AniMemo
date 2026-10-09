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
  if (file.includes(`${path.sep}journal${path.sep}`) && /"animemo\.local\/server\/internal\/(accounts|api|plugins)"/.test(source)) violations.push(`${file}: journal imports a transport or authentication module`);
}
for (const file of [...files('server/pkg'), ...files('server/examples')]) {
  if (file.endsWith('.go') && /"animemo\.local\/server\/internal\//.test(readFileSync(file, 'utf8'))) violations.push(`${file}: plugin SDK/consumer imports host internals`);
}
for (const file of files('server/examples/watch-history-text')) {
  if (file.endsWith('.go') && /"animemo\.local\/server\/pkg\/(pluginproto|themeproto)"/.test(readFileSync(file, 'utf8'))) violations.push(`${file}: converter depends on host/theme package types`);
}
for (const file of files('web/src')) {
  if (!file.includes(`${path.sep}components${path.sep}ui${path.sep}`) && /from ['"](?:@base-ui\/|motion\/|gsap)/.test(readFileSync(file,'utf8'))) violations.push(`${file}: feature imports a UI engine directly`);
  if (/\.(exe|zip|log|png)$/.test(file)) violations.push(`${file}: generated artifact in source tree`);
}
if (violations.length) { console.error(violations.join('\n')); process.exitCode = 1; }
else console.log('Module dependencies and source layout checked.');
