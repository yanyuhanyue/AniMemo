export const TAG_COLOR_OPTIONS = [
  ["pink", "粉色", "#fce7f3", "#be185d"],
  ["rose", "玫红", "#ffe4e6", "#e11d48"],
  ["blue", "蓝色", "#eff6ff", "#2563eb"],
  ["emerald", "翡翠", "#ecfdf5", "#059669"],
  ["amber", "琥珀", "#fffbeb", "#d97706"],
  ["orange", "橙色", "#fff7ed", "#ea580c"],
  ["indigo", "靛蓝", "#eef2ff", "#4f46e5"],
  ["violet", "堇紫", "#f5f3ff", "#7c3aed"],
  ["fuchsia", "洋红", "#fdf4ff", "#a21caf"],
  ["yellow", "黄色", "#fef9c3", "#854d0e"],
  ["purple", "紫色", "#f3e8ff", "#7e22ce"],
  ["cyan", "青色", "#ecfeff", "#0e7490"],
  ["lime", "青柠", "#f7fee7", "#4d7c0f"],
  ["sky", "天蓝", "#f0f9ff", "#0369a1"],
  ["slate", "灰色", "#f8fafc", "#475569"],
].map(([value, label, background, color]) => ({ value, label, background, color }));

export const FALLBACK_TAG_PRESETS = [
  ["真百", "pink"], ["轻百", "rose"], ["日常", "blue"], ["治愈", "emerald"], ["萌系", "amber"],
  ["搞笑", "orange"], ["校园", "lime"], ["恋爱", "fuchsia"], ["原创", "cyan"], ["奇幻", "indigo"],
  ["异世界", "violet"], ["战斗", "slate"], ["冒险", "slate"], ["剧场版", "sky"], ["泡面番", "yellow"],
  ["魔法少女", "rose"], ["音乐", "slate"], ["悬疑", "slate"], ["职场", "slate"], ["游戏改", "slate"],
].map(([name, color], index) => ({ id: `fallback-${name}`, name, color, sort_order: (index + 1) * 10 }));

const COLOR_KEYS = new Set(TAG_COLOR_OPTIONS.map((option) => option.value));
const FALLBACK_COLOR_MAP = Object.fromEntries(FALLBACK_TAG_PRESETS.map((preset) => [preset.name, preset.color]));
const GENERATED_COLOR_KEYS = TAG_COLOR_OPTIONS
  .map((option) => option.value)
  .filter((value) => value !== "slate");

export function normalizeTagPresets(payload, fallback = FALLBACK_TAG_PRESETS) {
  const source = Array.isArray(payload?.results) ? payload.results : Array.isArray(payload) ? payload : null;
  if (!source) return fallback;
  return source
    .map((item, index) => ({
      id: item?.id ?? `preset-${index}`,
      name: String(item?.name || "").trim(),
      color: COLOR_KEYS.has(item?.color) ? item.color : "slate",
      sort_order: Number(item?.sort_order) || 0,
    }))
    .filter((item) => item.name);
}

export function buildPresetColorMap(presets = FALLBACK_TAG_PRESETS) {
  return Object.fromEntries(normalizeTagPresets(presets).map((preset) => [preset.name, preset.color]));
}

export function generatedTagColorKey(tag) {
  const normalized = String(tag || "").normalize("NFKC").trim().toLocaleLowerCase();
  let hash = 2166136261;
  for (const character of normalized) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return GENERATED_COLOR_KEYS[(hash >>> 0) % GENERATED_COLOR_KEYS.length];
}

export function isCustomTagColor(value) {
  const normalized = String(value || "").trim().toLowerCase();
  return /^#[0-9a-f]{3,8}$/.test(normalized);
}

export function tagColorKey(tag, value, presetColors = FALLBACK_COLOR_MAP) {
  const normalized = String(value || "").trim().toLowerCase();
  if (COLOR_KEYS.has(normalized)) return normalized;
  return presetColors[tag] || FALLBACK_COLOR_MAP[tag] || generatedTagColorKey(tag);
}

export function resolveTagColors(tags, savedColors = {}, presetColors = FALLBACK_COLOR_MAP) {
  return Object.fromEntries((tags || []).map((tag) => {
    const saved = savedColors?.[tag];
    return [tag, isCustomTagColor(saved) ? saved : tagColorKey(tag, saved, presetColors)];
  }));
}

export function tagColorStyle(tag, value, presetColors) {
  const key = tagColorKey(tag, value, presetColors);
  const option = TAG_COLOR_OPTIONS.find((item) => item.value === key) || TAG_COLOR_OPTIONS.at(-1);
  return {
    "--edit-tag-background": option.background,
    "--edit-tag-color": option.color,
  };
}
