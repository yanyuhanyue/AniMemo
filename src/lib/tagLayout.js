const FULL_WIDTH_CHARACTER = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\p{Extended_Pictographic}]/u;

export function getTagText(tag) {
  return String(tag?.label ?? tag ?? "").trim();
}

export function getTagVisualUnits(tag) {
  return Array.from(getTagText(tag).normalize("NFC")).reduce((total, character) => {
    if (/\s/u.test(character)) return total + 0.5;
    return total + (FULL_WIDTH_CHARACTER.test(character) ? 2 : 1);
  }, 0);
}

export function isOversizedTag(tag, maxVisualUnits = 16) {
  return getTagVisualUnits(tag) > maxVisualUnits;
}

export function splitTagsIntoRows(tags, {
  maxItems = 3,
  maxVisualUnits = 16,
} = {}) {
  const rows = [];
  let currentRow = [];
  let currentUnits = 0;

  for (const tag of tags) {
    const visualUnits = getTagVisualUnits(tag);
    const exceedsItemLimit = currentRow.length >= maxItems;
    const exceedsWidthLimit = currentRow.length > 0
      && currentUnits + visualUnits > maxVisualUnits;

    if (exceedsItemLimit || exceedsWidthLimit) {
      rows.push(currentRow);
      currentRow = [];
      currentUnits = 0;
    }

    currentRow.push(tag);
    currentUnits += visualUnits;
  }

  if (currentRow.length > 0) rows.push(currentRow);
  return rows;
}
