import { useSyncExternalStore } from "react";

const revisions = new Map();
const subscriptions = new Set();

function normalizeDomains(domains) {
  return (Array.isArray(domains) ? domains : [domains]).filter(Boolean).map(String);
}

function snapshotFor(domains) {
  return normalizeDomains(domains).map((domain) => `${domain}:${revisions.get(domain) || 0}`).join("|");
}

export function getServerStateRevision(domain) {
  return revisions.get(String(domain)) || 0;
}

export function subscribeServerState(listener, domains = null) {
  const watched = domains == null ? null : new Set(normalizeDomains(domains));
  const subscription = { listener, watched };
  subscriptions.add(subscription);
  return () => subscriptions.delete(subscription);
}

export function invalidateServerState(domains, entityId = null) {
  const changed = normalizeDomains(domains);
  changed.forEach((domain) => revisions.set(domain, getServerStateRevision(domain) + 1));
  const event = { domains: changed, entityId };
  subscriptions.forEach(({ listener, watched }) => {
    if (!watched || changed.some((domain) => watched.has(domain))) listener(event);
  });
}

export function useServerStateRevision(domains) {
  const normalized = normalizeDomains(domains);
  const subscribe = (listener) => subscribeServerState(listener, normalized);
  const getSnapshot = () => snapshotFor(normalized);
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

function pathWithoutQuery(url = "") {
  return String(url).split("?", 1)[0].replace(/^\/+/, "");
}

export function invalidateServerStateForRequest(config = {}) {
  const method = String(config.method || "get").toLowerCase();
  if (["get", "head", "options"].includes(method)) return;
  const path = pathWithoutQuery(config.url);
  const entryMatch = path.match(/^entries\/(\d+)/)
    || path.match(/^external-sync\/providers\/[^/]+\/entries\/(\d+)/);
  const entityId = entryMatch ? Number(entryMatch[1]) : null;
  if (path.includes("watch-history")) {
    invalidateServerState(["watch_history", "journal_entries", "analytics"], entityId);
  } else if (path.startsWith("entries/")) {
    invalidateServerState(["journal_entries", "analytics"], entityId);
  } else if (path.startsWith("external-sync/")) {
    invalidateServerState(["external_sync", "journal_entries", "analytics"], entityId);
  } else if (/^external-accounts\/[^/]+\/import-apply\/$/.test(path)) {
    invalidateServerState(["external_accounts", "journal_entries", "analytics"]);
  } else if (path.startsWith("external-accounts/")) {
    invalidateServerState("external_accounts");
  } else if (path.startsWith("settings/") || path.startsWith("public-journal/")) {
    invalidateServerState(["settings", "showcase"]);
  } else if (path.startsWith("filters/")) {
    invalidateServerState("filters");
  } else if (path === "import/" || path.startsWith("auth/")) {
    invalidateServerState(["journal_entries", "settings", "analytics"]);
  }
}
