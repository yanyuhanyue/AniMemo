const LOOPBACK_HOST = "127.0.0.1";

export function resolveFixedLoopbackOrigin(rawValue, expectedPort, variableName) {
  if (!Number.isInteger(expectedPort) || expectedPort < 1 || expectedPort > 65535) {
    throw new TypeError("expectedPort must be a valid TCP port");
  }

  const expectedOrigin = `http://${LOOPBACK_HOST}:${expectedPort}`;
  if (rawValue === undefined || rawValue === "") return expectedOrigin;

  let candidate;
  try {
    candidate = new URL(rawValue);
  } catch {
    throw new Error(`${variableName} must be exactly ${expectedOrigin}`);
  }

  const isExpectedOrigin = candidate.protocol === "http:"
    && candidate.hostname === LOOPBACK_HOST
    && candidate.port === String(expectedPort)
    && candidate.username === ""
    && candidate.password === ""
    && candidate.pathname === "/"
    && candidate.search === ""
    && candidate.hash === "";

  if (!isExpectedOrigin) {
    throw new Error(`${variableName} must be exactly ${expectedOrigin}`);
  }

  // Reconstruct from trusted constants so environment input never reaches a network sink.
  return expectedOrigin;
}
