export function formatFeaturedPeriod(period = "") {
  const [year, month] = String(period).split("-");
  const monthNumber = Number(month);

  if (!year || !month || !Number.isFinite(monthNumber) || monthNumber < 1 || monthNumber > 12) {
    return "日期待定";
  }

  return `${year}年${monthNumber}月`;
}

export function FeaturedDateBadge({ period }) {
  return (
    <time className="featured-date-badge" dateTime={period || undefined}>
      {formatFeaturedPeriod(period)}
    </time>
  );
}
