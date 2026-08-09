const REFRESH_FIELD_MAP = {
  japanese_title: "japaneseTitle",
  airing_period: "period",
  studio: "studio",
  episodes: "episodes",
  poster_url: "posterUrl",
};

export function bangumiIdentityFromResult(item) {
  const externalId = item?.external_id ?? item?.id;
  if (externalId === null || externalId === undefined || String(externalId).trim() === "") return null;
  return { provider: "bangumi", external_id: String(externalId).trim() };
}

export function replaceProviderIdentity(identities, identity) {
  const current = Array.isArray(identities) ? identities : [];
  if (!identity?.provider) return current;
  return [...current.filter((item) => item?.provider !== identity.provider), identity];
}

export function refreshRecordPatch(changedFields, draft = {}) {
  const patch = {};
  Object.entries(changedFields || {}).forEach(([apiField, values]) => {
    const recordField = REFRESH_FIELD_MAP[apiField];
    if (recordField) patch[recordField] = values?.provider ?? "";
  });
  if (Object.hasOwn(patch, "posterUrl") && !hasCustomPoster(draft)) {
    patch.poster = patch.posterUrl || draft.poster;
    patch.posterSource = patch.posterUrl ? "default_url" : draft.posterSource;
  }
  return patch;
}

export function hasCustomPoster(record = {}) {
  return Boolean(
    record.customPosterUrl
    || record.posterFile
    || record.posterSource === "upload"
    || record.posterSource === "trusted_url"
  ) && !record.clearCustomPoster;
}

export const REFRESH_FIELD_LABELS = {
  japanese_title: "日文名",
  airing_period: "放送季度",
  studio: "制作公司",
  episodes: "话数",
  poster_url: "默认海报",
};
