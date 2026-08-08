import { Icon } from "../Icon.jsx";

const seasonConfig = {
  spring: { icon: "seedling", label: "春季" },
  summer: { icon: "sun", label: "夏季" },
  autumn: { icon: "leaf", label: "秋季" },
  winter: { icon: "snowflake", label: "冬季" },
};

export function getSeasonFromPeriod(period = "") {
  const rawMonth = String(period).split("-")[1];
  const quarter = /^Q([1-4])$/i.exec(rawMonth || "");
  const month = quarter ? (Number(quarter[1]) - 1) * 3 + 1 : Number(rawMonth);
  if ([3, 4, 5].includes(month)) return "spring";
  if ([6, 7, 8].includes(month)) return "summer";
  if ([9, 10, 11].includes(month)) return "autumn";
  return "winter";
}

export function SeasonBadge({
  period,
  variant = "detailed",
  showSeasonName = true,
  className = "",
}) {
  const season = getSeasonFromPeriod(period);
  const config = seasonConfig[season];
  const [year, rawMonth] = String(period).split("-");
  const quarter = /^Q([1-4])$/i.exec(rawMonth || "");
  const month = quarter ? String((Number(quarter[1]) - 1) * 3 + 1) : rawMonth;
  const periodLabel = showSeasonName
    ? `${year || "--"}年${month || "--"}月 · ${config.label}`
    : period;

  return (
    <span className={`season-badge season-badge--${variant} season-badge--${season} ${className}`.trim()}>
      <span className="season-badge__icon-rotor" aria-hidden="true">
        <span className="season-badge__icon"><Icon name={config.icon} /></span>
      </span>
      <span className="season-badge__label">{periodLabel}</span>
    </span>
  );
}
