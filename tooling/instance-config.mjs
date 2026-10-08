import { isIP, createServer } from 'node:net';

export function validName(name) {
  if (!/^[a-z][a-z0-9-]{0,31}$/.test(name || '')) throw new Error('Instance name must contain 1–32 lowercase letters, digits or hyphens, beginning with a letter.');
  return name;
}
export function validPort(port) {
  const n = Number(port);
  if (!Number.isInteger(n) || n < 1024 || n > 65535) throw new Error('Port must be 1024–65535.');
  return n;
}
export function validOrigin(origin) {
  const u = new URL(origin);
  if (u.origin !== origin || u.username || u.password || !['http:', 'https:'].includes(u.protocol) || (u.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(u.hostname))) throw new Error('Origin must be an exact HTTPS origin, or loopback HTTP for development.');
  return origin;
}
export function validBind(bind = '127.0.0.1') {
  if (!isIP(bind)) throw new Error('Bind address must be a literal IPv4 or IPv6 address.');
  return bind;
}
export function publicConfig(c) {
  return { bind: c.bind || '127.0.0.1', port: c.port, origin: c.origin };
}
export function configurationPlan(c, changes) {
  const next = { ...c, bind: validBind(changes.bind ?? c.bind), port: validPort(changes.port ?? c.port) };
  const loopbackOrigin = c.origin === `http://127.0.0.1:${c.port}`;
  next.origin = validOrigin(changes.origin ?? (loopbackOrigin ? `http://127.0.0.1:${next.port}` : c.origin));
  const before = publicConfig(c), after = publicConfig(next);
  return { next, preview: { before, after, changed: JSON.stringify(before) !== JSON.stringify(after), external_listener: !['127.0.0.1', '::1'].includes(next.bind), restart_required: true, manages_dns_tls: false } };
}
export async function checkPort(bind, port) {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once('error', () => reject(new Error('Listen address or port is unavailable; choose another address/port.')));
    server.listen({ host: validBind(bind), port: validPort(port), exclusive: true }, resolve);
  });
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
}
