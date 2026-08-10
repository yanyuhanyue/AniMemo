import { matchesActivityFilter } from "../lib/journalExperience.js";
import { comparePeriod } from "./dashboardData.js";

const DAY_MS = 24 * 60 * 60 * 1000;

function dateValue(value) {
  const parsed = new Date(value || "").getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}

function matchesServerQuickFilter(record, filter) {
  const tags = (filter?.tags || []).filter(Boolean);
  const keywords = (filter?.title_keywords || []).filter(Boolean);
  if (!tags.length && !keywords.length) return true;
  const predicates = [
    ...tags.map((tag) => (record.tags || []).some((value) => String(value).includes(tag))),
    ...keywords.map((keyword) => `${record.title || ""} ${record.japaneseTitle || ""}`.toLocaleLowerCase("zh-CN").includes(String(keyword).toLocaleLowerCase("zh-CN"))),
  ];
  return filter?.match_mode === "all" ? predicates.every(Boolean) : predicates.some(Boolean);
}

function matchesNeedsAttention(record) {
  const staleCutoff = Date.now() - 14 * DAY_MS;
  const completedUnrated = record.status === "completed" && !(Number(record.score) > 0);
  const watchingStale = record.status === "watching" && dateValue(record.lastWatchedOn) < staleCutoff;
  const noPoster = !record.poster || /poster-01\.webp$/.test(record.poster);
  return completedUnrated || watchingStale || !record.externalIdentities?.length || noPoster;
}

function compareNullableNumbers(a, b, direction) {
  const leftMissing = a === null || a === undefined || a === "";
  const rightMissing = b === null || b === undefined || b === "";
  const left = Number(a);
  const right = Number(b);
  if ((leftMissing || !Number.isFinite(left)) && (rightMissing || !Number.isFinite(right))) return 0;
  if (leftMissing || !Number.isFinite(left)) return 1;
  if (rightMissing || !Number.isFinite(right)) return -1;
  return direction * (left - right);
}

function compareIdsDescending(a, b) {
  const left = Number(a?.id);
  const right = Number(b?.id);
  if (Number.isFinite(left) && Number.isFinite(right)) return right - left;
  return String(b?.id || "").localeCompare(String(a?.id || ""), "zh-CN", { numeric: true });
}

function hasDashboardPriority(record) {
  return Number(record?.score) > 0 || record?.status === "completed";
}

export function matchesDashboardQuery(record, query = {}) {
  if (!record) return false;
  const search = String(query.search || "").trim().toLocaleLowerCase("zh-CN");
  const haystack = `${record.title || ""} ${record.japaneseTitle || ""} ${record.studio || ""} ${record.review || ""}`
    .toLocaleLowerCase("zh-CN");
  if (search && !haystack.includes(search)) return false;
  if (query.status && query.status !== "all" && record.status !== query.status) return false;
  if (query.visibility && query.visibility !== "all" && record.visibility !== query.visibility) return false;
  if (query.tag && query.tag !== "all" && !(record.tags || []).some((tag) => String(tag).includes(query.tag))) return false;
  if (query.year && query.year !== "all" && !String(record.period || "").startsWith(query.year)) return false;
  if (query.activity === "needs-attention") {
    if (!matchesNeedsAttention(record)) return false;
  } else if (!matchesActivityFilter(record, query.activity)) return false;
  if (!matchesServerQuickFilter(record, query.quickFilter)) return false;
  return true;
}

export function compareDashboardRecords(a, b, query = {}) {
  if (query.priority !== false) {
    const priorityDifference = Number(hasDashboardPriority(b)) - Number(hasDashboardPriority(a));
    if (priorityDifference) return priorityDifference;
  }

  let difference = 0;
  if (query.sort === "score-desc") difference = compareNullableNumbers(a.score, b.score, -1);
  else if (query.sort === "score-asc") difference = compareNullableNumbers(a.score, b.score, 1);
  else if (query.sort === "date-asc") difference = comparePeriod(a, b);
  else if (query.sort === "updated-desc") difference = String(b.updatedAt || "").localeCompare(String(a.updatedAt || ""));
  else if (query.sort === "updated-asc") difference = String(a.updatedAt || "").localeCompare(String(b.updatedAt || ""));
  else difference = comparePeriod(b, a);
  return difference || compareIdsDescending(a, b);
}

export function sortDashboardRecords(records, query = {}) {
  return [...records].sort((a, b) => compareDashboardRecords(a, b, query));
}

export function mutationCountDelta({ type, previousRecord, nextRecord, query = {} }) {
  const previousMatches = type !== "create" && matchesDashboardQuery(previousRecord, query);
  const nextMatches = type !== "delete" && matchesDashboardQuery(nextRecord, query);
  return Number(nextMatches) - Number(previousMatches);
}

export function reconcileDashboardMutations(records, mutations, query = {}) {
  const nextRecords = [...records];
  let countDelta = 0;
  let removedVisibleRecord = false;

  for (const mutation of mutations) {
    const previousMatches = mutation.type !== "create" && matchesDashboardQuery(mutation.previousRecord, query);
    const nextMatches = mutation.type !== "delete" && matchesDashboardQuery(mutation.nextRecord, query);
    const ids = new Set([mutation.previousRecord?.id, mutation.nextRecord?.id].filter((id) => id != null).map(String));
    const previousLength = nextRecords.length;
    for (let index = nextRecords.length - 1; index >= 0; index -= 1) {
      if (ids.has(String(nextRecords[index]?.id))) nextRecords.splice(index, 1);
    }
    if (nextMatches && mutation.nextRecord) nextRecords.push(mutation.nextRecord);
    countDelta += Number(nextMatches) - Number(previousMatches);
    if (previousMatches && (!nextMatches || nextRecords.length < previousLength)) removedVisibleRecord = true;
  }

  return {
    records: sortDashboardRecords(nextRecords, query),
    countDelta,
    removedVisibleRecord,
  };
}
