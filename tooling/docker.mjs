import path from 'node:path';

export function dockerEnvironment(environment) {
  const result = { ...environment };
  const certificate = environment.ANIMEMO_BUILD_CA || environment.CODEX_PROXY_CERT;
  if (certificate) result.ANIMEMO_BUILD_CA = path.resolve(certificate);
  return result;
}

export function composeArguments(environment, project = 'animemo-next', overrides = []) {
  const files = ['deploy/compose.yaml'];
  if (environment.ANIMEMO_BUILD_CA) files.push('deploy/compose.proxy.yaml');
  files.push(...overrides);
  return ['compose', '-p', project, ...files.flatMap(file => ['-f', file])];
}
