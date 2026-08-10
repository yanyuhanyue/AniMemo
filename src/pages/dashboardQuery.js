export const DASHBOARD_PAGE_SIZE = 48;

const SORT_ORDERING = {
  "date-desc": "-airing_period",
  "date-asc": "airing_period",
  "score-desc": "-personal_score",
  "score-asc": "personal_score",
  "updated-desc": "-updated_at",
  "updated-asc": "updated_at",
};

export function buildDashboardQueryParams(query, { page = 1, includeFacets = false } = {}) {
  const params = new URLSearchParams();
  params.set("page", String(page));
  params.set("page_size", String(DASHBOARD_PAGE_SIZE));
  const search = String(query?.search || "").trim();
  if (search) params.set("search", search);
  if (query?.status && query.status !== "all") params.set("status", query.status);
  if (query?.visibility && query.visibility !== "all") params.set("visibility", query.visibility);
  if (query?.tag && query.tag !== "all") params.set("tag", query.tag);
  if (query?.year && query.year !== "all") params.set("year", query.year);
  if (query?.activity && query.activity !== "all") params.set("activity", query.activity);
  if (query?.priority !== false) params.set("priority", "1");
  const ordering = SORT_ORDERING[query?.sort] || SORT_ORDERING["date-desc"];
  params.set("ordering", ordering);
  const quick = query?.quickFilter;
  if (quick && String(quick.id) !== "all") {
    (quick.tags || []).filter(Boolean).forEach((tag) => params.append("quick_tags", tag));
    (quick.title_keywords || []).filter(Boolean).forEach((keyword) => params.append("quick_title_keywords", keyword));
    if (quick.match_mode) params.set("quick_match_mode", quick.match_mode);
  }
  if (includeFacets) params.set("include_facets", "1");
  return params;
}

export function getDashboardNextPage(payload) {
  if (!payload?.next) return null;
  try {
    const page = new URL(payload.next, "http://localhost").searchParams.get("page");
    const parsed = Number(page);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
  } catch {
    return null;
  }
}

export function appendUniqueDashboardRecords(current, incoming) {
  const seen = new Set(current.map((item) => String(item?.id)));
  return [...current, ...incoming.filter((item) => {
    const key = String(item?.id);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  })];
}
