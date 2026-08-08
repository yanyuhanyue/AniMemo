import { statusTagColors, tagColors } from "../data/catalogData.js";
import { isCustomTagColor, tagColorKey } from "../lib/tagPresets.js";

export function TagChip({ tag, color, onClick, active = false }) {
  const fixedStatusColor = statusTagColors[tag];
  const customColor = !fixedStatusColor && isCustomTagColor(color) ? color : null;
  const resolvedColor = fixedStatusColor || tagColorKey(tag, color, tagColors);
  const className = `tag-chip tag-${customColor ? "custom" : resolvedColor}${active ? " is-active" : ""}`;
  const style = customColor ? { backgroundColor: customColor } : undefined;
  if (onClick) {
    return <button type="button" className={className} style={style} onClick={onClick}>{tag}</button>;
  }
  return <span className={className} style={style}>{tag}</span>;
}
