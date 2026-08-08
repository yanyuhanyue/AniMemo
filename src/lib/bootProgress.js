export const BOOT_PROGRESS_STAGES = Object.freeze({
  mounted: 10,
  fontsReady: 25,
  dataReady: 55,
  imagesReady: 85,
  layoutReady: 95,
  complete: 100,
});

export function normalizeBootProgress(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  return Math.max(0, Math.min(100, Math.round(numeric)));
}

export function nextMonotonicProgress(currentValue, nextValue) {
  const current = normalizeBootProgress(currentValue);
  if (current >= BOOT_PROGRESS_STAGES.complete) return BOOT_PROGRESS_STAGES.complete;
  return Math.max(current, normalizeBootProgress(nextValue));
}
